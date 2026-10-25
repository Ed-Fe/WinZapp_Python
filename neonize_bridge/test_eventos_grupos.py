"""Mudancas de grupo ao vivo: funcoes puras com eventos de mentira e os
manipuladores com um `neonize.events` de mentira (sem DLL nem conta)."""
import sys
import types
from types import SimpleNamespace as N

import eventos_grupos as eg


def jid(usuario, servidor="s.whatsapp.net"):
    return N(User=usuario, Server=servidor)


def grupo(**campos):
    base = dict(JID=jid("120", "g.us"), Join=[], Leave=[], Promote=[], Demote=[])
    base.update(campos)
    return N(**base)


def pessoa(j):
    return eg.msgmap.jid_wpp(eg.jid_texto(j))


def test_nome_novo_vira_subject_com_o_id_do_grupo():
    e = grupo(Name=N(Name="  Familia  "))
    assert eg.atualizacoes_do_evento(e, pessoa) == [{"id": "120@g.us", "subject": "Familia"}]


def test_campos_sao_juntos_num_item_so():
    e = grupo(Name=N(Name="X"), Topic=N(Topic="regras", TopicDeleted=False),
              Locked=N(isLocked=True), Announce=N(IsAnnounce=False))
    assert eg.atualizacoes_do_evento(e, pessoa) == [{
        "id": "120@g.us", "subject": "X", "desc": "regras",
        "restrict": True, "announce": False}]


def test_descricao_apagada_vira_vazia():
    e = grupo(Topic=N(Topic="velha", TopicDeleted=True))
    assert eg.atualizacoes_do_evento(e, pessoa) == [{"id": "120@g.us", "desc": ""}]


def test_nome_vazio_nao_vira_subject():
    assert eg.atualizacoes_do_evento(grupo(Name=N(Name="  ")), pessoa) == []


def test_membros_viram_um_item_por_acao_na_ordem():
    e = grupo(Join=[jid("1"), jid("2"), jid("3")], Demote=[jid("4")], Leave=[jid("5")])
    assert eg.atualizacoes_do_evento(e, pessoa) == [
        {"id": "120@g.us", "participants": ["1@c.us", "2@c.us", "3@c.us"], "action": "add"},
        {"id": "120@g.us", "participants": ["5@c.us"], "action": "remove"},
        {"id": "120@g.us", "participants": ["4@c.us"], "action": "demote"}]


def test_evento_de_fora_de_grupo_ou_sem_mudanca_nao_gera_nada():
    assert eg.atualizacoes_do_evento(grupo(JID=jid("5511")), pessoa) == []
    assert eg.atualizacoes_do_evento(grupo(), pessoa) == []
    assert eg.atualizacoes_do_evento(N(), pessoa) == []


def test_entrada_em_grupo_traz_nome_descricao_e_ajustes():
    info = N(JID=jid("120", "g.us"), GroupName=N(Name="Novo"),
             GroupTopic=N(Topic="", TopicDeleted=False),
             GroupLocked=N(isLocked=False), GroupAnnounce=N(IsAnnounce=True))
    assert eg.atualizacoes_de_entrada(N(GroupInfo=info)) == [{
        "id": "120@g.us", "subject": "Novo", "desc": "", "restrict": False, "announce": True}]
    assert eg.atualizacoes_de_entrada(N(GroupInfo=N(JID=jid("5511")))) == []
    assert eg.atualizacoes_de_entrada(N()) == []


def test_tem_usa_hasfield_quando_existe():
    class Proto:
        def HasField(self, campo):
            return campo == "Name"
        Name = N(Name="a")
        Topic = N(Topic="b", TopicDeleted=False)
    assert eg._tem(Proto(), "Name") and not eg._tem(Proto(), "Topic")


# ------------------------------------------------- manipuladores, sem neonize
class _Cli:
    def __init__(self, pares=None):
        self.manipuladores = {}
        self.pares = pares or {}

    def event(self, tipo):
        def liga(f):
            self.manipuladores[tipo] = f
            return f
        return liga

    def get_pn_from_lid(self, j):
        return self.pares[f"{j.User}@{j.Server}"]


class _Sessao:
    nome = "s1"

    def __init__(self):
        self.enviados = []

    def emitir(self, evento, dados):
        self.enviados.append((evento, dados))


def _ligar(monkeypatch, pares=None):
    eventos = types.ModuleType("neonize.events")
    eventos.GroupInfoEv = type("GroupInfoEv", (), {})
    eventos.JoinedGroupEv = type("JoinedGroupEv", (), {})
    pacote = types.ModuleType("neonize")
    pacote.events = eventos
    monkeypatch.setitem(sys.modules, "neonize", pacote)
    monkeypatch.setitem(sys.modules, "neonize.events", eventos)
    cli, s = _Cli(pares), _Sessao()
    eg.registrar_eventos(cli, s)
    return cli, s, eventos


def test_grupo_mudou_emite_groups_update_da_sessao(monkeypatch):
    cli, s, ev = _ligar(monkeypatch)
    cli.manipuladores[ev.GroupInfoEv](cli, grupo(Name=N(Name="Novo nome")))
    assert s.enviados == [("groups.update", {
        "session": "s1", "data": [{"id": "120@g.us", "subject": "Novo nome"}]})]


def test_membro_lid_vira_numero_quando_se_sabe_e_fica_lid_quando_nao(monkeypatch):
    cli, s, ev = _ligar(monkeypatch, {"99@lid": jid("5511")})
    cli.manipuladores[ev.GroupInfoEv](
        cli, grupo(Join=[jid("99", "lid"), jid("77", "lid")]))
    # 77@lid nao tem par: get_pn_from_lid levanta KeyError e o LID fica
    assert s.enviados[0][1]["data"] == [{
        "id": "120@g.us", "participants": ["5511@c.us", "77@lid"], "action": "add"}]


def test_evento_sem_mudanca_ou_com_erro_nao_emite_nem_levanta(monkeypatch):
    cli, s, ev = _ligar(monkeypatch)
    cli.manipuladores[ev.GroupInfoEv](cli, grupo())
    cli.manipuladores[ev.GroupInfoEv](cli, None)
    cli.manipuladores[ev.JoinedGroupEv](cli, N())
    assert s.enviados == []


def test_entrada_emite_groups_update(monkeypatch):
    cli, s, ev = _ligar(monkeypatch)
    info = N(JID=jid("120", "g.us"), GroupName=N(Name="Novo"))
    cli.manipuladores[ev.JoinedGroupEv](cli, N(GroupInfo=info))
    assert s.enviados[0] == ("groups.update", {
        "session": "s1", "data": [{"id": "120@g.us", "subject": "Novo"}]})
