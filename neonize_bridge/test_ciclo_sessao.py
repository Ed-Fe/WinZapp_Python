"""O ciclo de vida da sessao: a maquina de estados pura e a Sessao de verdade
com um cliente de mentira (sem neonize, sem WhatsApp). Nao testado contra uma
conta real: o comportamento do Stop/Disconnect do Go vem da leitura do neonize."""
import threading
import time

import pytest

import ciclo_sessao as cs
import server
from test_um_manipulador_por_evento import registros


# ------------------------------------------------------------ maquina pura
@pytest.mark.parametrize("viva,status,telefone,esperado", [
    (False, "CLOSED", None, cs.INICIAR),
    (False, "CONNECTED", None, cs.INICIAR),            # thread morta: sobe outra
    (True, "CLOSED", None, cs.REINICIAR),              # close-session ainda terminando
    (True, "notLogged", None, cs.REINICIAR),
    (True, "QRCODE", "5511999990000", cs.REINICIAR),   # pareamento parado, recomeca
    (True, "QRCODE", None, cs.IGNORAR),
    (True, "CONNECTED", None, cs.IGNORAR),
    (True, "CONNECTED", "5511999990000", cs.IGNORAR),
    (True, "INITIALIZING", None, cs.IGNORAR),
])
def test_decidir_inicio(viva, status, telefone, esperado):
    assert cs.decidir_inicio(viva, status, telefone) == esperado


def test_queda_so_tira_de_connected_e_a_volta_so_devolve_o_que_a_queda_tirou():
    assert cs.proximo_estado("CONNECTED", False, cs.CAIU) == ("INITIALIZING", True)
    assert cs.proximo_estado("QRCODE", False, cs.CAIU) == ("QRCODE", False)
    assert cs.proximo_estado("INITIALIZING", True, cs.VOLTOU) == ("CONNECTED", False)
    # INITIALIZING do arranque (nunca esteve conectada): VOLTOU nao inventa CONNECTED
    assert cs.proximo_estado("INITIALIZING", False, cs.VOLTOU) == ("INITIALIZING", False)
    assert cs.proximo_estado("INITIALIZING", True, cs.CONECTOU) == ("CONNECTED", False)


def test_falha_terminal_e_fim_da_thread_fecham_mas_nao_desfazem_o_desvinculo():
    for evento in (cs.FALHA_TERMINAL, cs.FIM_DA_THREAD):
        assert cs.proximo_estado("INITIALIZING", False, evento)[0] == "CLOSED"
        assert cs.proximo_estado("CONNECTED", False, evento)[0] == "CLOSED"
        assert cs.proximo_estado("notLogged", False, evento)[0] == "notLogged"
    assert cs.proximo_estado("CONNECTED", False, cs.SAIU)[0] == "notLogged"


def test_evento_de_geracao_antiga_nao_mexe_no_estado():
    c = cs.Ciclo()
    antiga = c.abrir()
    assert c.status == "INITIALIZING"
    c.encerrar()
    nova = c.abrir()
    assert c.aplicar(antiga, cs.FIM_DA_THREAD) is None      # a thread antiga acordando
    assert c.aplicar(antiga, cs.CONECTOU) is None
    assert c.status == "INITIALIZING"
    assert c.aplicar(nova, cs.CONECTOU) == ("INITIALIZING", "CONNECTED")


def test_encerrar_sem_status_mantem_a_conta_desvinculada():
    c = cs.Ciclo()
    g = c.abrir()
    c.aplicar(g, cs.SAIU)
    c.encerrar()                    # o close-session que o cliente manda depois
    assert c.status == "notLogged"
    c.encerrar("CLOSED")
    assert c.status == "CLOSED"


@pytest.mark.parametrize("motivo,esperado", [
    (2, cs.SAIU), (4, cs.SAIU), (5, cs.SAIU),
    (3, cs.FALHA_TERMINAL), (6, cs.FALHA_TERMINAL), (7, cs.FALHA_TERMINAL),
    (1, cs.CAIU), (8, cs.CAIU), (10, cs.CAIU), (0, cs.CAIU)])
def test_classificar_falha(motivo, esperado):
    assert cs.classificar_falha(motivo) == esperado


def test_avisos_de_logout_mandam_o_status_find_antes_do_session_logged():
    avisos = cs.avisos_de_logout("conta1")
    assert [a[1] for a in avisos] == ["status-find", "session-logged"]
    assert avisos[0][0] == 0 and avisos[1][0] > 0
    assert avisos[0][2]["status"] in ("notLogged", "disconnectedMobile")
    assert avisos[1][2]["status"] is False


def test_host_device_so_diz_o_numero_conectado_e_nunca_um_vazio_incerto():
    http, corpo = cs.resposta_host_device("CONNECTED", "5511@s.whatsapp.net", "9@lid")
    assert http == 200 and corpo["response"]["phoneNumber"] == {"_serialized": "5511@s.whatsapp.net"}
    # ponte recem-iniciada: o 200 com numero vazio era lido como "desvinculada"
    for status, eu in (("INITIALIZING", ""), ("CONNECTED", ""), ("CLOSED", "5511@s.whatsapp.net"),
                       ("INITIALIZING", "5511@s.whatsapp.net")):
        assert cs.resposta_host_device(status, eu, "")[0] == 503
    # desvinculada de verdade: 200 sem numero, como o WPPConnect
    for status in ("notLogged", "QRCODE"):
        assert cs.resposta_host_device(status, "", "") == (
            200, {"status": "success", "response": {}})


def test_status_apagavel_so_o_meu_no_mural():
    assert cs.status_apagavel({"fromMe": True, "to": "status@broadcast"})
    assert not cs.status_apagavel({"fromMe": False, "to": "status@broadcast"})
    assert not cs.status_apagavel({"fromMe": True, "to": "5511@c.us"})
    assert not cs.status_apagavel(None)


def test_codigo_expirado_nao_leva_telefone_e_e_o_nome_que_o_cliente_fala():
    aviso = cs.aviso_de_codigo_expirado("conta1")
    assert aviso["name"] == "LinkCodeExpired" and aviso["rateLimited"] is False
    assert cs.VIDA_DO_CODIGO > 0


# ------------------------------------------------- Sessao com cliente falso
class ClienteFalso:
    """O Neonize() do Go so volta no stop(); disconnect() so fecha o socket."""

    def __init__(self):
        self.parado = threading.Event()
        self.chamadas = []
        self.is_connected = True

    def disconnect(self):
        self.chamadas.append("disconnect")

    def stop(self):
        self.chamadas.append("stop")
        self.parado.set()

    def logout(self):
        self.chamadas.append("logout")


@pytest.fixture
def sessao(tmp_path, monkeypatch):
    s = server.Sessao("conta1", str(tmp_path / "conta1"), None, None)
    s.eventos = []
    s.emitir = lambda evento, dados: s.eventos.append((evento, dados))
    s.clientes = []
    s.geracoes = []

    def rodar_falso(self, telefone, geracao):
        cli = self.cliente = ClienteFalso()
        self.clientes.append(cli)
        self.geracoes.append(geracao)
        try:
            cli.parado.wait(10)
        finally:
            self._aplicar(geracao, cs.FIM_DA_THREAD)

    monkeypatch.setattr(server.Sessao, "_rodar", rodar_falso)
    return s


def _espera(condicao, tempo=3):
    fim = time.monotonic() + tempo
    while time.monotonic() < fim:
        if condicao():
            return True
        time.sleep(0.01)
    return False


def test_close_e_start_na_mesma_sessao_sobem_uma_thread_nova(sessao):
    """O defeito 1: o parar() so fazia disconnect(); o connect() do neonize nao
    voltava, a thread seguia viva e o start-session seguinte saia cedo, com o
    estado preso em CLOSED."""
    assert sessao.iniciar() is True
    primeira = sessao.thread
    assert _espera(lambda: sessao.clientes)
    sessao.ciclo.aplicar(sessao.geracoes[0], cs.CONECTOU)
    assert sessao.status == "CONNECTED"

    sessao.parar()
    assert sessao.status == "CLOSED"
    assert sessao.clientes[0].chamadas == ["disconnect", "stop"]

    assert sessao.iniciar() is True                 # o start-session do cliente
    assert sessao.thread is not primeira
    assert not primeira.is_alive()
    assert _espera(lambda: len(sessao.clientes) == 2)
    assert sessao.status == "INITIALIZING"          # nao ficou CLOSED
    sessao.parar()
    sessao.thread.join(3)


def test_start_com_a_sessao_de_pe_nao_cria_outra_thread(sessao):
    assert sessao.iniciar() is True
    thread = sessao.thread
    assert _espera(lambda: sessao.clientes)
    assert sessao.iniciar() is False
    assert sessao.thread is thread
    sessao.parar()
    thread.join(3)


def test_fim_atrasado_da_thread_antiga_nao_fecha_a_sessao_nova(sessao):
    sessao.iniciar()
    assert _espera(lambda: sessao.clientes)
    antiga = sessao.geracoes[0]
    sessao.parar()
    sessao.thread.join(3)
    sessao.iniciar()
    assert _espera(lambda: len(sessao.clientes) == 2)
    sessao.ciclo.aplicar(sessao.geracoes[1], cs.CONECTOU)
    assert sessao._aplicar(antiga, cs.FIM_DA_THREAD) is None
    assert sessao.status == "CONNECTED"
    sessao.parar()
    sessao.thread.join(3)


def test_thread_que_nao_termina_nao_ganha_uma_irma(sessao, monkeypatch):
    monkeypatch.setattr(cs, "PRAZO_DA_THREAD_ANTIGA", 0.2)
    sessao.iniciar()
    assert _espera(lambda: sessao.clientes)
    cliente = sessao.clientes[0]
    cliente.stop = lambda: cliente.chamadas.append("stop")      # o Go nao volta
    sessao.parar()
    assert sessao.iniciar() is False
    assert len(sessao.clientes) == 1 and sessao.status == "CLOSED"
    cliente.parado.set()
    sessao.thread.join(3)


def test_parar_sem_cliente_nem_thread_nao_quebra(sessao):
    sessao.parar()
    assert sessao.status == "CLOSED"


def test_sair_limpa_a_identidade(sessao):
    sessao.iniciar()
    assert _espera(lambda: sessao.clientes)
    sessao.eu, sessao.lid = "5511@s.whatsapp.net", "9@lid"
    sessao.sair()
    assert sessao.eu == "" and sessao.lid == "" and sessao.status == "CLOSED"
    assert sessao.clientes[0].chamadas[0] == "logout"
    sessao.thread.join(3)


# ------------------------------------------------------- eventos do neonize
def _conectada(sessao):
    sessao.iniciar()
    assert _espera(lambda: sessao.clientes)
    g = sessao.geracoes[0]
    sessao.eu, sessao.lid = "5511@s.whatsapp.net", "9@lid"
    sessao.ciclo.aplicar(g, cs.CONECTOU)
    return g, sessao.clientes[0]


def test_logout_pelo_celular_avisa_na_ordem_limpa_a_identidade_e_para_o_cliente(
        sessao, monkeypatch):
    monkeypatch.setattr(cs, "ATRASO_DO_SESSION_LOGGED", 0.05)
    desvinculadas = []
    sessao.ao_desvincular = lambda: desvinculadas.append(1)
    g, cli = _conectada(sessao)

    sessao._ao_ser_desvinculada(g, cli)
    assert sessao.status == "notLogged"     # a consulta de estado do cliente o le como desvinculo
    assert sessao.eu == "" and sessao.lid == ""
    assert desvinculadas == [1]
    assert _espera(lambda: "stop" in cli.chamadas)
    assert _espera(lambda: len(sessao.eventos) == 2)
    assert [e[0] for e in sessao.eventos] == ["status-find", "session-logged"]
    assert sessao.eventos[1][1]["status"] is False

    sessao._ao_ser_desvinculada(g, cli)     # ConnectFailure e LoggedOut do mesmo logout
    time.sleep(0.15)
    assert len(sessao.eventos) == 2 and desvinculadas == [1]
    sessao.parar()                          # o close-session que o cliente manda depois
    assert sessao.status == "notLogged"
    sessao.thread.join(3)


def test_conectar_guarda_a_identidade_so_para_a_geracao_vigente(sessao, monkeypatch):
    from types import SimpleNamespace as N
    for nome in ("_carregar_nomes", "_conferir_estado", "_conferir_sempre", "_copia_sempre"):
        monkeypatch.setattr(server.Sessao, nome, lambda self: None)
    sessao.iniciar()
    assert _espera(lambda: sessao.clientes)
    g = sessao.geracoes[0]
    c = N(get_me=lambda: N(JID=N(User="5511", Server="s.whatsapp.net"), LID=N(User="9", Server="lid")))

    sessao._ao_conectar(g + 1, c, None)             # evento de uma geracao que nao e a vigente
    assert sessao.eu == "" and sessao.status == "INITIALIZING" and sessao.eventos == []
    sessao._ao_conectar(g, c, None)
    assert sessao.eu == "5511@s.whatsapp.net" and sessao.lid == "9@lid"
    assert sessao.status == "CONNECTED"
    assert sessao.eventos == [("session-logged", {"session": "conta1", "status": True})]
    sessao.parar()
    sessao.thread.join(3)


def test_queda_vira_initializing_e_a_volta_restaura(sessao):
    g, _ = _conectada(sessao)
    sessao._ao_cair(g, cs.CAIU, "desconectado")
    assert sessao.status == "INITIALIZING"
    sessao._ao_cair(g, cs.VOLTOU, "keep-alive restaurado")
    assert sessao.status == "CONNECTED"
    sessao.parar()
    sessao.thread.join(3)
    sessao._ao_cair(g, cs.CAIU, "desconectado")     # evento atrasado da sessao fechada
    assert sessao.status == "CLOSED"


def test_falha_terminal_fecha_a_sessao_e_para_o_cliente(sessao):
    g, cli = _conectada(sessao)
    sessao._ao_falha_terminal(g, cli, "banimento temporario")
    assert sessao.status == "CLOSED"
    assert _espera(lambda: "stop" in cli.chamadas)
    sessao.thread.join(3)


def test_connect_failure_por_motivo(sessao):
    g, cli = _conectada(sessao)
    sessao._ao_falhar(g, cli, 8)                    # falha do servidor: passageira
    assert sessao.status == "INITIALIZING"
    sessao._ao_falhar(g, cli, 6)                    # versao antiga: terminal
    assert sessao.status == "CLOSED"
    sessao.thread.join(3)


def test_connect_failure_de_logout_desvincula(sessao):
    g, cli = _conectada(sessao)
    sessao._ao_falhar(g, cli, 4)
    assert sessao.status == "notLogged"
    sessao.thread.join(3)


def test_cada_evento_do_ciclo_de_vida_e_registrado_uma_vez_no_server():
    achados = registros()
    for tipo in ("ConnectedEv", "LoggedOutEv", "DisconnectedEv", "ConnectFailureEv",
                 "ClientOutdatedEv", "TemporaryBanEv", "StreamReplacedEv", "StreamErrorEv",
                 "KeepAliveTimeoutEv", "KeepAliveRestoredEv"):
        assert len(achados[tipo]) == 1, tipo
        assert achados[tipo][0].startswith("server.py"), tipo


# ------------------------------------------------- codigo de pareamento
def test_codigo_que_vence_sem_parear_avisa_uma_vez_e_nao_gera_outro(sessao):
    sessao.iniciar()
    assert _espera(lambda: sessao.clientes)
    g = sessao.geracoes[0]
    sessao.ciclo.aplicar(g, cs.QR)
    sessao._vigiar_codigo(g, vida=0.1, passo=0.02)
    assert [e[0] for e in sessao.eventos] == ["phoneCodeError"]
    assert sessao.eventos[0][1]["name"] == "LinkCodeExpired"
    sessao.parar()
    sessao.thread.join(3)


def test_codigo_pareado_ou_de_sessao_antiga_nao_avisa_nada(sessao):
    sessao.iniciar()
    assert _espera(lambda: sessao.clientes)
    g = sessao.geracoes[0]
    sessao.ciclo.aplicar(g, cs.QR)
    sessao.ciclo.aplicar(g, cs.CONECTOU)            # pareou antes de vencer
    sessao._vigiar_codigo(g, vida=0.1, passo=0.02)
    sessao.ciclo.aplicar(g, cs.QR)
    sessao.parar()                                  # fechou: a geracao nao e mais a vigente
    sessao._vigiar_codigo(g, vida=0.1, passo=0.02)
    assert sessao.eventos == []
    sessao.thread.join(3)
