"""Progresso de transferencia: as contas puras, o limitador e a sonda (com
relogio e leitura falsos; nenhuma espera real alem de um teste de thread)."""
import threading

import pytest

import progresso as p


@pytest.mark.parametrize("entrada,esperado", [
    (0, 0.0), (10, 0.1), (55, 0.55), (100, 1.0), (250, 1.0), ("42", 0.42),
    (-1, None), (None, None), ("x", None), (float("nan"), None),
])
def test_fracao_de_percentual(entrada, esperado):
    assert p.fracao_de_percentual(entrada) == esperado


@pytest.mark.parametrize("atual,esperado,fracao", [
    (0, 1000, 0.0), (250, 1000, 0.25), (1000, 1000, p.TETO_EM_ANDAMENTO),
    (5000, 1000, p.TETO_EM_ANDAMENTO),          # arquivo maior que o dito: nunca passa do teto
    (10, 0, None), (10, -5, None), (-1, 100, None), (None, 100, None), ("a", 100, None),
])
def test_fracao_de_arquivo(atual, esperado, fracao):
    assert p.fracao_de_arquivo(atual, esperado) == fracao


def test_id_de_progresso_so_aceita_texto_do_corpo():
    assert p.id_de_progresso({"progressId": "  ABC  "}) == "ABC"
    assert p.id_de_progresso({"progressId": 7}) == ""
    assert p.id_de_progresso({}) == ""
    assert p.id_de_progresso(None) == ""
    assert p.id_de_progresso([1]) == ""
    assert len(p.id_de_progresso({"progressId": "x" * 999})) == 200


def test_payloads_do_formato_que_o_cliente_le():
    assert p.evento_upload("conta1", "U1", 0.12345678) == {
        "uploadId": "U1", "progress": 0.1235, "session": "conta1"}
    assert p.evento_download("conta1", "D1", 0.5) == {
        "progressId": "D1", "progress": 0.5, "session": "conta1"}


def test_limitador_so_emite_quando_o_percentual_inteiro_avanca():
    lim = p.Limitador(minimo=0.25)
    assert lim.decidir(0.10, 0.0) is True
    assert lim.decidir(0.10, 1.0) is False        # igual
    assert lim.decidir(0.104, 2.0) is False       # mesmo percentual inteiro
    assert lim.decidir(0.05, 3.0) is False        # nunca recua
    assert lim.decidir(0.20, 4.0) is True


def test_limitador_espera_o_intervalo_minimo_mas_nao_para_o_100():
    lim = p.Limitador(minimo=0.25)
    assert lim.decidir(0.10, 10.0) is True
    assert lim.decidir(0.20, 10.1) is False       # cedo demais
    assert lim.decidir(0.20, 10.3) is True
    assert lim.decidir(1.0, 10.31) is True        # o fim passa por cima do intervalo
    assert lim.terminou
    assert lim.decidir(1.0, 20.0) is False        # e so uma vez


def test_limitador_ignora_leitura_sem_valor():
    assert p.Limitador().decidir(None, 0.0) is False


class Relogio:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def _acompanhante(leituras, recebidos, relogio):
    fila = iter(leituras)

    def ler():
        valor = next(fila)
        if isinstance(valor, Exception):
            raise valor
        return valor
    return p.Acompanhante(ler, recebidos.append, relogio=relogio)


def test_passo_emite_pelo_limitador_e_sobrevive_a_leitura_ruim():
    rel, recebidos = Relogio(), []
    a = _acompanhante([0.1, RuntimeError("dll"), None, 0.1, 0.5], recebidos, rel)
    for _ in range(5):
        rel.t += 1
        a.passo()
    assert recebidos == [0.1, 0.5]


def test_emitir_que_falha_nao_derruba_o_passo():
    def explode(_):
        raise RuntimeError("socket")
    a = p.Acompanhante(lambda: 0.5, explode)
    a.passo()       # nao levanta


def test_concluir_emite_o_100_uma_vez():
    rel, recebidos = Relogio(), []
    a = _acompanhante([0.3], recebidos, rel)
    rel.t = 1
    a.passo()
    a.concluir()
    a.concluir()
    assert recebidos == [0.3, 1.0]


def test_a_thread_sonda_ate_parar():
    lidas = []
    tres = threading.Event()

    def ler():
        lidas.append(1)
        if len(lidas) >= 3:
            tres.set()
        return min(len(lidas) / 10, 0.9)
    recebidos = []
    a = p.Acompanhante(ler, recebidos.append, intervalo=0.01, minimo=0.0)
    with a:
        assert tres.wait(5)
    n = len(lidas)
    threading.Event().wait(0.1)
    assert len(lidas) == n          # parou ao sair do contexto
    assert recebidos[0] == 0.1
    assert a._thread is None


def test_sair_do_contexto_com_erro_nao_emite_o_100():
    recebidos = []
    with pytest.raises(RuntimeError):
        with p.Acompanhante(lambda: None, recebidos.append, intervalo=0.01):
            raise RuntimeError("falhou")
    assert recebidos == []
