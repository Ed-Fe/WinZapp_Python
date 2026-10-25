"""O erro de codigo de pareamento no formato que o cliente le."""
import pytest

import pareamento


def test_erro_comum_leva_nome_mensagem_e_nao_e_quota():
    info = pareamento.erro_do_codigo(RuntimeError("connection reset"))
    assert info == {"name": "LinkCodeError", "message": "RuntimeError: connection reset",
                    "rateLimited": False}


@pytest.mark.parametrize("texto", [
    "iq error: rate-overlimit", "IQErrorRateOverlimit", "server said 429", "RATE-OVERLIMIT"])
def test_recusa_por_quota_e_reconhecida(texto):
    assert pareamento.erro_do_codigo(Exception(texto))["rateLimited"] is True


def test_numero_que_so_contem_429_nao_e_quota():
    assert pareamento.erro_do_codigo(Exception("id 14290001"))["rateLimited"] is False


def test_telefone_nunca_vai_na_mensagem():
    info = pareamento.erro_do_codigo(Exception("invalid number 5511999998888 for user"))
    assert "5511999998888" not in info["message"] and "<n>" in info["message"]


def test_mensagem_vazia_fica_so_com_o_nome_e_o_tamanho_tem_limite():
    assert pareamento.erro_do_codigo(ValueError())["message"] == "ValueError"
    assert len(pareamento.erro_do_codigo(Exception("x" * 5000))["message"]) <= pareamento.LIMITE_DA_MENSAGEM
