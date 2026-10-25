"""Nome de sessao e arquivo do corpo: nada que saia de dados-real ou leia o disco."""
import base64

import pytest

import entradas


@pytest.mark.parametrize("nome", ["conta1", "Minha_Conta-2", "a" * 128])
def test_nomes_comuns_de_sessao_sao_aceitos(nome):
    assert entradas.nome_de_sessao_valido(nome)


@pytest.mark.parametrize("nome", ["", "..", "../x", "a/b", "a\\b", "c:", "a b", "a" * 129, None])
def test_nome_que_sai_da_pasta_e_recusado(nome):
    assert not entradas.nome_de_sessao_valido(nome)


def test_bytes_do_corpo_aceita_data_uri_e_base64_puro():
    dados = b"\x89PNG-fake"
    b64 = base64.b64encode(dados).decode()
    assert entradas.bytes_do_corpo({"path": "data:image/png;base64," + b64}) == dados
    assert entradas.bytes_do_corpo({"path": b64}) == dados


@pytest.mark.parametrize("caminho", ["/etc/passwd", "C:\\Users\\x\\secret.key", "../../x.png", "", None])
def test_bytes_do_corpo_nunca_le_um_caminho_do_disco(caminho):
    with pytest.raises(ValueError):
        entradas.bytes_do_corpo({"path": caminho})
