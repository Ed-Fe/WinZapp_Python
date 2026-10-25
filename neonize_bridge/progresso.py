"""Progresso real de envio e download de midia: a logica pura (sem neonize,
sem rede) que `transferencias.py` liga ao fork da DLL.

O WinZapp escuta `media-upload-progress` ({uploadId, progress 0..1, session})
e `media-download-progress` ({progressId, progress 0..1, session}). A ponte nao
os emitia: a barra ficava parada ate o envio terminar. Aqui ficam as contas
(percentual -> fracao, tamanho do arquivo -> fracao), o limitador (so emite
quando o percentual inteiro avanca e no maximo a cada ~250 ms; o 100% sempre
passa) e o `Acompanhante`, que sonda uma leitura numa thread propria.

Testado em test_progresso.py.
"""
import logging
import threading
import time

log = logging.getLogger("ponte")

#: segundos entre duas leituras do progresso
INTERVALO_LEITURA = 0.3
#: segundos minimos entre dois eventos (o 100% ignora)
INTERVALO_MINIMO = 0.25
#: enquanto o arquivo cresce, nunca passa disto: so o fim da transferencia diz 100%
TETO_EM_ANDAMENTO = 0.99
_TAMANHO_MAXIMO_DO_ID = 200


def fracao_de_percentual(percentual):
    """0..100 (o que TransferProgress devolve) -> 0.0..1.0; None quando nao ha
    leitura (-1 = chave desconhecida, None, texto, NaN)."""
    try:
        valor = float(percentual)
    except (TypeError, ValueError):
        return None
    if valor != valor or valor < 0:
        return None
    return min(valor, 100.0) / 100.0


def fracao_de_arquivo(atual, esperado):
    """Bytes ja gravados / bytes esperados -> 0.0..TETO_EM_ANDAMENTO, ou None
    quando nao da para saber (tamanho esperado desconhecido, arquivo ainda
    inexistente)."""
    try:
        atual, esperado = int(atual), int(esperado)
    except (TypeError, ValueError):
        return None
    if esperado <= 0 or atual < 0:
        return None
    return min(atual / esperado, TETO_EM_ANDAMENTO)


def id_de_progresso(corpo):
    """O `progressId` que o cliente manda no corpo do get-media, ou '' (sem
    corpo JSON, outro tipo, vazio)."""
    if not isinstance(corpo, dict):
        return ""
    valor = corpo.get("progressId")
    if not isinstance(valor, str):
        return ""
    return valor.strip()[:_TAMANHO_MAXIMO_DO_ID]


def evento_upload(sessao, upload_id, fracao):
    return {"uploadId": upload_id, "progress": round(fracao, 4), "session": sessao}


def evento_download(sessao, progress_id, fracao):
    return {"progressId": progress_id, "progress": round(fracao, 4), "session": sessao}


class Limitador:
    """Decide se uma leitura vira evento: o percentual inteiro tem de ter
    avancado e, fora o 100%, tem de ter passado `minimo` segundos desde o
    ultimo evento. Nunca recua."""

    def __init__(self, minimo=INTERVALO_MINIMO):
        self.minimo = minimo
        self.pct = -1
        self.quando = None

    @property
    def terminou(self):
        return self.pct >= 100

    def decidir(self, fracao, agora):
        if fracao is None:
            return False
        pct = int(round(min(max(fracao, 0.0), 1.0) * 100))
        if pct <= self.pct:
            return False
        if pct < 100 and self.quando is not None and agora - self.quando < self.minimo:
            return False
        self.pct, self.quando = pct, agora
        return True


class Acompanhante:
    """Sonda `ler()` (devolve fracao 0..1 ou None) a cada `intervalo` numa
    thread e chama `emitir(fracao)` pelo `Limitador`. Usar como contexto em volta
    da transferencia; `concluir()` (so no sucesso) emite o 100%."""

    def __init__(self, ler, emitir, *, intervalo=INTERVALO_LEITURA,
                 minimo=INTERVALO_MINIMO, relogio=time.monotonic):
        self._ler, self._emitir = ler, emitir
        self._intervalo, self._relogio = intervalo, relogio
        self._limitador = Limitador(minimo)
        self._trava = threading.Lock()
        self._parar = threading.Event()
        self._thread = None

    def _emitir_se_couber(self, fracao):
        with self._trava:
            if not self._limitador.decidir(fracao, self._relogio()):
                return
            try:
                self._emitir(fracao)
            except Exception:
                log.exception("progresso: emitir falhou")

    def passo(self):
        """Uma leitura. Nunca levanta: uma leitura ruim so pula o evento."""
        try:
            fracao = self._ler()
        except Exception:
            return
        self._emitir_se_couber(fracao)

    def _laco(self):
        while not self._parar.wait(self._intervalo):
            self.passo()

    def iniciar(self):
        self._thread = threading.Thread(target=self._laco, name="progresso", daemon=True)
        self._thread.start()
        return self

    def parar(self):
        self._parar.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def concluir(self):
        """A transferencia acabou bem: para a sonda e garante o 100%."""
        self.parar()
        self._emitir_se_couber(1.0)

    def __enter__(self):
        return self.iniciar()

    def __exit__(self, *_):
        self.parar()
        return False
