"""redigir: o log nunca mostra o JID, o telefone ou o nome da sessao inteiros."""
from redacao import redigir


def test_jid_de_pessoa_guarda_so_os_dois_ultimos_digitos_e_o_servidor():
    assert redigir("5511999998888@s.whatsapp.net") == "...88@s.whatsapp.net"


def test_jid_de_grupo_e_lid_guardam_o_tipo():
    assert redigir("120363025246125486@g.us") == "...86@g.us"
    assert redigir("98765432109876@lid") == "...76@lid"


def test_nome_de_sessao_sem_arroba():
    assert redigir("minha-conta") == "...ta"


def test_valor_curto_ou_vazio_nao_quebra():
    assert redigir("a") == "...a"
    assert redigir("") == "?"
    assert redigir(None) == "?"


def test_o_resultado_nunca_contem_o_valor_original():
    for original in ("5511999998888@s.whatsapp.net", "120363025246125486@g.us",
                     "conta-do-joao"):
        assert original not in redigir(original)
        assert original.partition("@")[0][:-2] not in redigir(original)


# ---------------------------------------------------------- filtro do log
import io
import logging

from redacao import FiltroDeRedacao, mascarar_texto


def test_mascarar_texto_esconde_jid_e_numero_longo():
    assert mascarar_texto("falhou com 5511999998888@s.whatsapp.net em 120363025246125486@g.us") \
        == "falhou com ...88@s.whatsapp.net em ...86@g.us"
    assert mascarar_texto("numero 5511999998888 invalido") == "numero ...88 invalido"
    assert mascarar_texto("erro 404 em 3 tentativas") == "erro 404 em 3 tentativas"


def _log_com_filtro():
    saida = io.StringIO()
    manipulador = logging.StreamHandler(saida)
    manipulador.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    manipulador.addFilter(FiltroDeRedacao())
    registro = logging.getLogger("teste_redacao")
    registro.handlers = [manipulador]
    registro.propagate = False
    registro.setLevel(logging.INFO)
    return registro, saida


def test_o_filtro_mascara_a_mensagem_formatada_e_o_traceback():
    registro, saida = _log_com_filtro()
    try:
        raise ValueError("rejeitado: 5511999998888@s.whatsapp.net")
    except ValueError:
        registro.exception("enviando para %s", "5511999998888@s.whatsapp.net")
    texto = saida.getvalue()
    assert "5511999998888" not in texto
    assert "...88@s.whatsapp.net" in texto
