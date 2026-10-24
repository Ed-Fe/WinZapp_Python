"""Rotas de grupos: as funcoes puras que montam o que o cliente le."""
from types import SimpleNamespace as N

import grupos


def pessoa(usuario, erro=0, convite="", expira=0, lid="", fone=""):
    return N(JID=N(User=usuario), LID=N(User=lid), PhoneNumber=N(User=fone),
             Error=erro, AddRequest=N(Code=convite, Expiration=expira))


def test_usuario_de_aceita_as_formas_de_id():
    assert grupos.usuario_de("5511999@c.us") == "5511999"
    assert grupos.usuario_de("5511999:12@s.whatsapp.net") == "5511999"
    assert grupos.usuario_de("5511999") == "5511999"


def test_quem_voltou_na_lista_foi_aceito_e_quem_nao_voltou_foi_recusado():
    saida = grupos.resultado_de_participantes(
        ["111@c.us", "222@c.us"], [pessoa("111")])
    assert saida == {"111@c.us": {"code": 200}, "222@c.us": {"code": 403}}


def test_o_erro_do_participante_vira_o_codigo():
    saida = grupos.resultado_de_participantes(
        ["111@c.us", "222@c.us"], [pessoa("111", erro=409), pessoa("222", erro=421)])
    assert saida["111@c.us"]["code"] == 409 and saida["222@c.us"]["code"] == 421


def test_recusado_com_convite_leva_o_codigo_do_convite():
    saida = grupos.resultado_de_participantes(
        ["111@c.us"], [pessoa("111", erro=403, convite="ABC", expira=99)])
    assert saida["111@c.us"] == {"code": 403, "invite_code": "ABC", "invite_code_exp": 99}


def test_a_pessoa_e_achada_pelo_lid_ou_pelo_numero():
    assert grupos.resultado_de_participantes(
        ["333@lid"], [pessoa("", lid="333")])["333@lid"]["code"] == 200
    assert grupos.resultado_de_participantes(
        ["444@c.us"], [pessoa("", fone="444")])["444@c.us"]["code"] == 200


def test_erros_de_entrada_por_convite():
    assert grupos.pedido_de_entrada_pendente(Exception("membership approval required"))
    assert not grupos.pedido_de_entrada_pendente(Exception("expired"))
    assert grupos.ja_participa(Exception("already a participant"))


def test_grupos_em_comum_pelo_jid_lid_ou_numero():
    g1 = N(JID="g1@g.us", Participants=[pessoa("111"), pessoa("222")])
    g2 = N(JID="g2@g.us", Participants=[pessoa("", lid="333")])
    g3 = N(JID="g3@g.us", Participants=[pessoa("999")])
    assert grupos.usuarios_em_comum([g1, g2, g3], {"111", "333"}) == ["g1@g.us", "g2@g.us"]
    assert grupos.usuarios_em_comum([g1], set()) == []
