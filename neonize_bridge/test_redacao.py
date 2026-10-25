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
