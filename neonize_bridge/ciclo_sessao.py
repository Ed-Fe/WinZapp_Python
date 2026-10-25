"""O ciclo de vida de uma sessao: quem esta valendo, em que estado ela esta e
o que cada evento do neonize muda. Logica pura, sem neonize e sem asyncio,
testada em test_ciclo_sessao.py.

Por que existe uma "geracao". O `connect()` do neonize bloqueia a thread ate o
contexto do Go ser cancelado por `stop()` (neonize/client.py: o `Neonize()` do
Go "so retorna quando o contexto e cancelado via Stop"; `disconnect()` so fecha
o socket). Fechar a sessao sem `stop()` deixava a thread viva e o
`start-session` seguinte voltava cedo, com o estado preso em CLOSED. Agora cada
`abrir()` abre uma geracao nova e `encerrar()` a invalida: uma thread antiga
que acorda depois (fim do `connect()`, evento atrasado) ve que nao e mais a
vigente e nao mexe no estado da nova.
"""
import threading

CLOSED = "CLOSED"
INITIALIZING = "INITIALIZING"
QRCODE = "QRCODE"
CONNECTED = "CONNECTED"
# Conta desvinculada (o celular tirou o aparelho). E o `notLogged` do WPPConnect,
# que o cliente (connection_state.UNLINKED_STATES) le como "desvinculada" pela
# consulta de estado; CLOSED o faria reiniciar a sessao e pedir um QR sem aviso.
NOT_LOGGED = "notLogged"

# Estados em que a sessao foi encerrada: uma thread ainda viva ali esta so
# terminando, e um novo start-session pode substitui-la.
ENCERRADOS = (CLOSED, NOT_LOGGED)

# Quanto o start-session espera a thread antiga terminar depois do stop().
PRAZO_DA_THREAD_ANTIGA = 10.0

# Decisoes de `decidir_inicio`.
INICIAR = "iniciar"
REINICIAR = "reiniciar"
IGNORAR = "ignorar"


def decidir_inicio(thread_viva, status, telefone) -> str:
    """O que um start-session faz: INICIAR (nao ha thread), REINICIAR (ha uma
    thread que precisa parar antes: ela esta terminando depois de um close ou
    logout, ou e uma tentativa de pareamento parada que o telefone recomeca) ou
    IGNORAR (ja esta de pe)."""
    if not thread_viva:
        return INICIAR
    if status in ENCERRADOS or (telefone and status == QRCODE):
        return REINICIAR
    return IGNORAR


# Eventos da thread/neonize que mexem no estado.
CONECTOU = "conectou"
QR = "qr"
CAIU = "caiu"                       # DisconnectedEv, KeepAliveTimeoutEv, falha passageira
VOLTOU = "voltou"                   # KeepAliveRestoredEv
SAIU = "saiu"                       # LoggedOutEv: o celular removeu o aparelho
FALHA_TERMINAL = "falha_terminal"   # banido, versao antiga, outro aparelho tomou a conexao
FIM_DA_THREAD = "fim_da_thread"     # o connect() voltou


def proximo_estado(status, degradada, evento):
    """(novo status, degradada) apos `evento`. `degradada` lembra que fomos nos
    que tiramos a sessao de CONNECTED (queda ou keep-alive), para o aviso de
    "voltou" so restaurar o que a queda tirou."""
    if evento == CONECTOU:
        return CONNECTED, False
    if evento == QR:
        return QRCODE, False
    if evento == CAIU:
        if status == CONNECTED:
            return INITIALIZING, True
        return status, degradada
    if evento == VOLTOU:
        if status == INITIALIZING and degradada:
            return CONNECTED, False
        return status, degradada
    if evento == SAIU:
        return NOT_LOGGED, False
    if evento in (FALHA_TERMINAL, FIM_DA_THREAD):
        return (status, degradada) if status in ENCERRADOS else (CLOSED, False)
    return status, degradada


class Ciclo:
    """Estado e geracao de uma sessao, sob uma trava: o estado so muda por
    quem ainda e a geracao vigente."""

    def __init__(self):
        self._trava = threading.Lock()
        self.status = CLOSED
        self.geracao = 0
        self.degradada = False

    def abrir(self) -> int:
        """Nova geracao, em INITIALIZING; devolve o numero dela."""
        with self._trava:
            self.geracao += 1
            self.status = INITIALIZING
            self.degradada = False
            return self.geracao

    def encerrar(self, status=None):
        """Invalida a geracao atual: o que ela ainda disser e ignorado. Sem
        `status`, vira CLOSED, menos uma conta desvinculada, que continua
        desvinculada (o close-session que o cliente manda depois nao apaga o
        aviso)."""
        with self._trava:
            self.geracao += 1
            if status is None:
                status = NOT_LOGGED if self.status == NOT_LOGGED else CLOSED
            self.status = status
            self.degradada = False

    def vigente(self, geracao) -> bool:
        return geracao == self.geracao

    def aplicar(self, geracao, evento):
        """Aplica `evento` se `geracao` e a vigente. Devolve (antes, depois)
        ou None quando o evento e de uma geracao antiga."""
        with self._trava:
            if geracao != self.geracao:
                return None
            antes = self.status
            self.status, self.degradada = proximo_estado(antes, self.degradada, evento)
            return antes, self.status


# ConnectFailureReason do neonize (Neonize.proto): GENERIC 1, LOGGED_OUT 2,
# TEMP_BANNED 3, MAIN_DEVICE_GONE 4, UNKNOWN_LOGOUT 5, CLIENT_OUTDATED 6,
# BAD_USER_AGENT 7, INTERNAL_SERVER_ERROR 8, EXPERIMENTAL 9, SERVICE_UNAVAILABLE 10.
MOTIVOS_DE_LOGOUT = (2, 4, 5)
MOTIVOS_TERMINAIS = (3, 6, 7)


def classificar_falha(motivo) -> str:
    """O que um ConnectFailureEv significa: SAIU (o aparelho foi removido),
    FALHA_TERMINAL (repetir nao adianta e pode piorar: banimento, versao
    antiga) ou CAIU (falha do servidor, passageira)."""
    if motivo in MOTIVOS_DE_LOGOUT:
        return SAIU
    if motivo in MOTIVOS_TERMINAIS:
        return FALHA_TERMINAL
    return CAIU


# O cliente (websocket_client.on_wpp_status_find) so trata o desvinculo se ainda
# se ve conectado, e o python-socketio roda cada evento em uma thread propria:
# mandados juntos, o session-logged(false) podia limpar o "conectado" antes e o
# status-find ser ignorado. Por isso o status-find vai primeiro e o outro depois.
ATRASO_DO_SESSION_LOGGED = 1.5


def avisos_de_logout(nome):
    """[(atraso em segundos, evento, dados)] que o cliente recebe quando o
    celular remove o aparelho, na ordem em que devem sair."""
    return [(0.0, "status-find", {"session": nome, "status": NOT_LOGGED}),
            (ATRASO_DO_SESSION_LOGGED, "session-logged", {"session": nome, "status": False})]


# Quanto vive um codigo de pareamento por telefone. PALPITE: nao ha como medir
# sem uma conta; 195 s e o intervalo em que o wa-js renova o codigo no
# WPPConnect (docs/traps/pairing-flow.md), entao o codigo nao morre antes disso.
# Avisar um pouco tarde e melhor que avisar que morreu um codigo ainda valido.
VIDA_DO_CODIGO = 195.0


def aviso_de_codigo_expirado(nome) -> dict:
    """phoneCodeError que o cliente fala em voz alta (`LinkCodeExpired`). Sem
    telefone e sem novo codigo: gerar outro sem ninguem pedir gasta a cota por
    numero (docs/traps/pairing-flow.md)."""
    return {"session": nome, "name": "LinkCodeExpired",
            "message": "the pairing code expired", "rateLimited": False}


def resposta_host_device(status, eu, lid):
    """(http, corpo) de GET host-device. So CONNECTED com identidade diz o
    numero. QRCODE e desvinculada dizem, como o WPPConnect, que nao ha numero
    ligado (corpo sem `phoneNumber`): e a unica resposta que o cliente le como
    "desvinculada" e a que ele checa antes de apagar dados locais. Qualquer
    outro estado (ponte recem-iniciada, reconectando, fechada) nao sabe
    ainda: 503, que o cliente le como "nao sei" e nunca como desvinculo."""
    if status == CONNECTED and eu:
        return 200, {"status": "success", "response": {
            "phoneNumber": {"_serialized": eu}, "wid": {"_serialized": eu},
            "lid": {"_serialized": lid}}}
    if status in (QRCODE, NOT_LOGGED):
        return 200, {"status": "success", "response": {}}
    return 503, {"status": "error", "message": "The WhatsApp session is not connected."}


def status_apagavel(mensagem) -> bool:
    """True se a mensagem guardada e um status meu (so esses o status/{id}/delete apaga)."""
    return (isinstance(mensagem, dict) and bool(mensagem.get("fromMe"))
            and mensagem.get("to") == "status@broadcast")
