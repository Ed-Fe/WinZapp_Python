"""Rotas de comunidades: as funcoes puras que montam o que o cliente le."""
from types import SimpleNamespace as N

import comunidades


def jid(usuario, servidor="s.whatsapp.net"):
    return N(User=usuario, Server=servidor)


def pessoa(usuario, admin=False, super_admin=False, lid="", fone="", erro=0):
    return N(JID=jid(usuario), LID=jid(lid, "lid"), PhoneNumber=jid(fone),
             IsAdmin=admin, IsSuperAdmin=super_admin, Error=erro,
             AddRequest=N(Code="", Expiration=0))


def grupo(usuario, nome="", pai="", pais=False, padrao=False, topico="", gente=()):
    return N(JID=jid(usuario, "g.us"), GroupName=N(Name=nome), GroupTopic=N(Topic=topico),
             GroupParent=N(IsParent=pais), GroupIsDefaultSub=N(IsDefaultSubGroup=padrao),
             GroupLinkedParent=N(LinkedParentJID=jid(pai, "g.us")),
             Participants=list(gente))


def alvo(usuario, nome, padrao=False):
    return N(JID=jid(usuario, "g.us"), GroupName=N(Name=nome),
             GroupIsDefaultSub=N(IsDefaultSubGroup=padrao))


def test_so_o_grupo_pai_e_comunidade():
    assert comunidades.e_comunidade(grupo("1", pais=True))
    assert not comunidades.e_comunidade(grupo("2", pai="1"))


def test_administradores_sao_so_os_marcados_e_o_numero_vem_primeiro():
    g = grupo("1", pais=True, gente=[
        pessoa("lid1", admin=True, lid="lid1", fone="5511"), pessoa("222", super_admin=True),
        pessoa("333")])
    assert comunidades.administradores_de(g) == [
        {"id": "5511@s.whatsapp.net", "superAdmin": False},
        {"id": "222@s.whatsapp.net", "superAdmin": True}]


def test_sou_administrador_pelo_numero_ou_pelo_lid():
    g = grupo("1", pais=True, gente=[pessoa("lid1", admin=True, lid="lid1"), pessoa("333")])
    assert comunidades.sou_administrador(g, {"lid1"})
    assert not comunidades.sou_administrador(g, {"333"})   # membro, nao admin
    assert not comunidades.sou_administrador(g, {""})
    assert not comunidades.sou_administrador(g, set())


def test_subgrupos_entrados_e_nao_entrados():
    pai = grupo("1", "Comunidade", pais=True)
    avisos = grupo("2", "Avisos", pai="1", padrao=True)
    geral = grupo("3", "Geral", pai="1")
    de_outra = grupo("4", "Outra", pai="9")
    linhas = comunidades.subgrupos_da_comunidade(
        pai, [pai, avisos, geral, de_outra],
        [alvo("2", "Avisos", True), alvo("5", "Nao entrei"), alvo("1", "o proprio pai")])
    assert [(l["id"], l["subject"], l["joined"], l["announcement"]) for l in linhas] == [
        ("2@g.us", "Avisos", True, True), ("3@g.us", "Geral", True, False),
        ("5@g.us", "Nao entrei", False, False)]
    assert linhas[2]["approval"] is False and linhas[2]["requested"] is False


def test_comunidade_no_formato_que_o_cliente_le():
    pai = grupo("1", "Comunidade", pais=True, topico="Sobre",
                gente=[pessoa("lid1", admin=True, lid="lid1")])
    avisos = grupo("2", "Avisos", pai="1", padrao=True)
    saida = comunidades.comunidades_para_wpp([pai, avisos, grupo("7", "Grupo comum")], {"lid1"})
    assert saida == [{
        "id": "1@g.us", "subject": "Comunidade", "description": "Sobre", "iAmAdmin": True,
        "admins": [{"id": "lid1@s.whatsapp.net", "superAdmin": False}],
        "subgroups": [{"id": "2@g.us", "subject": "Avisos", "announcement": True,
                       "general": False, "joined": True, "approval": False}]}]


def test_ids_de_grupos_recusa_o_que_nao_e_grupo():
    assert comunidades.ids_de_grupos(["1@g.us", "12-34@g.us"]) == ["1@g.us", "12-34@g.us"]
    assert comunidades.ids_de_grupos([]) == []
    assert comunidades.ids_de_grupos(["5511@c.us"]) is None
    assert comunidades.ids_de_grupos("1@g.us") is None
    assert comunidades.ids_de_grupos([1]) is None


def test_resultado_de_vinculos():
    assert comunidades.resultado_de_vinculos("linkedGroupJids", ["1@g.us"], [("2@g.us", "x")]) == {
        "linkedGroupJids": ["1@g.us"],
        "failedGroups": [{"groupJid": "2@g.us", "errorText": "x"}]}
    assert comunidades.resultado_de_vinculos("unlinkedGroupJids", [], []) == {
        "unlinkedGroupJids": [], "failedGroups": []}


def test_promocao_aceita_e_recusada():
    assert comunidades.promocao_aceita("111@c.us", [pessoa("111")])
    assert not comunidades.promocao_aceita("111@c.us", [])
    assert not comunidades.promocao_aceita("111@c.us", [pessoa("111", erro=403)])


def test_resultado_de_promocao_tem_success_falso_se_alguem_foi_recusado():
    bom = comunidades.resultado_de_promocao(["111@c.us"], [pessoa("111")])
    assert bom == {"participants": [{"id": "111@c.us", "code": 200}], "success": True}
    mau = comunidades.resultado_de_promocao(["111@c.us", "222@c.us"], [pessoa("111")])
    assert mau["success"] is False and mau["participants"][1] == {"id": "222@c.us", "code": 403}


def test_pessoas_da_comunidade_marcam_quem_e_admin():
    admins = [pessoa("lid1", admin=True, lid="lid1", fone="5511"), pessoa("9", super_admin=True)]
    linhas = comunidades.pessoas_da_comunidade(
        [jid("5511"), jid("9"), jid("333"), jid("")], admins)
    assert linhas == [
        {"id": "5511@s.whatsapp.net", "isAdmin": True, "isSuperAdmin": False},
        {"id": "9@s.whatsapp.net", "isAdmin": True, "isSuperAdmin": True},
        {"id": "333@s.whatsapp.net", "isAdmin": False, "isSuperAdmin": False}]
