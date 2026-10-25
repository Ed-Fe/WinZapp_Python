"""contacts.update: payload no formato que o cliente le, desduplicacao e os
manipuladores, tudo sem neonize (um `neonize.events` de mentira e eventos de
SimpleNamespace)."""
import sys
import types
from types import SimpleNamespace as N

import eventos_contatos as ec


def jid(usuario, servidor="s.whatsapp.net"):
    return N(User=usuario, Server=servidor)


# ------------------------------------------------------------------ puras
def test_so_pessoa_conta():
    assert ec.e_pessoa(jid("5511"))
    assert ec.e_pessoa(jid("99", "lid"))
    assert not ec.e_pessoa(jid("120", "g.us"))
    assert not ec.e_pessoa(jid("x", "newsletter"))
    assert not ec.e_pessoa(jid("", "s.whatsapp.net"))
    assert not ec.e_pessoa(None)


def test_nome_limpo():
    assert ec.nome_limpo("  Ana ") == "Ana"
    assert ec.nome_limpo("   ") == ""
    assert ec.nome_limpo(None) == ""


def test_contato_so_leva_o_que_ha():
    assert ec.contato_wpp("5511@c.us") == {"remoteJid": "5511@c.us"}
    assert ec.contato_wpp("5511@c.us", nome="Ana") == {
        "remoteJid": "5511@c.us", "pushName": "Ana"}
    assert ec.contato_wpp("5511@c.us", foto="https://x/y.jpg") == {
        "remoteJid": "5511@c.us", "profilePicUrl": "https://x/y.jpg"}
    assert ec.evento_de_contatos("s1", [{"remoteJid": "5511@c.us"}]) == {
        "session": "s1", "data": [{"remoteJid": "5511@c.us"}]}


def test_nao_repete_o_que_nao_mudou():
    a = ec.Anunciados()
    assert a.nome_mudou("5511@c.us", "Ana") is True
    assert a.nome_mudou("5511@c.us", "Ana") is False
    assert a.nome_mudou("5511@c.us", "Ana Maria") is True
    assert a.nome_mudou("5511@c.us", "") is False
    # nome e foto sao independentes
    assert a.foto_mudou("5511@c.us", "f1") is True
    assert a.foto_mudou("5511@c.us", "f1") is False
    assert a.nome_mudou("5511@c.us", "Ana Maria") is False
    assert a.foto_mudou("5511@c.us", "f2") is True


def test_memoria_de_anunciados_tem_limite(monkeypatch):
    monkeypatch.setattr(ec, "MAXIMO_DE_CONTATOS", 3)
    a = ec.Anunciados()
    for i in range(5):
        a.nome_mudou(f"{i}@c.us", "n")
    assert a.nome_mudou("0@c.us", "n") is True     # esquecido: volta a valer
    assert a.nome_mudou("4@c.us", "n") is False


# --------------------------------------------- manipuladores, sem neonize
class _Cli:
    def __init__(self, pares=None, fotos=None):
        self.manipuladores = {}
        self.pares = pares or {}
        self.fotos = fotos or {}

    def event(self, tipo):
        def liga(f):
            self.manipuladores[tipo] = f
            return f
        return liga

    def get_pn_from_lid(self, j):
        return self.pares[f"{j.User}@{j.Server}"]

    def get_profile_picture(self, j):
        resultado = self.fotos[f"{j.User}@{j.Server}"]
        if isinstance(resultado, Exception):
            raise resultado
        return resultado


class _Sessao:
    nome = "s1"

    def __init__(self):
        self.enviados = []

    def emitir(self, evento, dados):
        self.enviados.append((evento, dados))


def _ligar(monkeypatch, **kw):
    eventos = types.ModuleType("neonize.events")
    eventos.PictureEv = type("PictureEv", (), {})
    pacote = types.ModuleType("neonize")
    pacote.events = eventos
    monkeypatch.setitem(sys.modules, "neonize", pacote)
    monkeypatch.setitem(sys.modules, "neonize.events", eventos)
    # a busca da foto roda na hora, em vez de numa thread
    monkeypatch.setattr(ec, "_em_segundo_plano", lambda f, *a: f(*a))
    cli, s = _Cli(**kw), _Sessao()
    ec.registrar_eventos(cli, s)
    return cli, s, eventos


def _foto(url, id_="F1"):
    return N(URL=url, ID=id_)


def test_foto_mudou_emite_a_url_pelo_numero(monkeypatch):
    cli, s, ev = _ligar(
        monkeypatch, pares={"99@lid": jid("5511")},
        fotos={"99@lid": _foto("https://pps.whatsapp.net/a.jpg")})
    cli.manipuladores[ev.PictureEv](
        cli, N(JID=jid("99", "lid"), Author=jid("99", "lid"), Timestamp=1, Remove=False))
    assert s.enviados == [("contacts.update", {"session": "s1", "data": [{
        "remoteJid": "5511@c.us", "profilePicUrl": "https://pps.whatsapp.net/a.jpg"}]})]


def test_foto_repetida_nao_e_emitida_de_novo(monkeypatch):
    cli, s, ev = _ligar(monkeypatch, fotos={"5511@s.whatsapp.net": _foto("https://x/a.jpg")})
    evento = N(JID=jid("5511"), Author=jid("5511"), Timestamp=1, Remove=False)
    cli.manipuladores[ev.PictureEv](cli, evento)
    cli.manipuladores[ev.PictureEv](cli, evento)
    assert len(s.enviados) == 1
    cli.fotos["5511@s.whatsapp.net"] = _foto("https://x/b.jpg", "F2")
    cli.manipuladores[ev.PictureEv](cli, evento)
    assert len(s.enviados) == 2
    assert s.enviados[1][1]["data"][0]["profilePicUrl"] == "https://x/b.jpg"


def test_foto_removida_de_grupo_ou_sem_url_nao_emite(monkeypatch):
    cli, s, ev = _ligar(monkeypatch, fotos={
        "5511@s.whatsapp.net": _foto(""),
        "5522@s.whatsapp.net": RuntimeError("profile picture not found"),
        "5533@s.whatsapp.net": RuntimeError("timeout")})
    tratar = cli.manipuladores[ev.PictureEv]
    tratar(cli, N(JID=jid("5511"), Remove=True))                    # removida
    tratar(cli, N(JID=jid("120", "g.us"), Remove=False))            # grupo
    tratar(cli, N(JID=jid("5511"), Remove=False))                   # sem URL
    tratar(cli, N(JID=jid("5522"), Remove=False))                   # sem foto
    tratar(cli, N(JID=jid("5533"), Remove=False))                   # falha de rede
    assert s.enviados == []


def test_apelido_novo_emite_uma_vez(monkeypatch):
    cli, s, _ = _ligar(monkeypatch)
    assert ec.anunciar_nome(cli, s, jid("5511"), " Ana ") is True
    assert s.enviados == [("contacts.update", {"session": "s1", "data": [{
        "remoteJid": "5511@c.us", "pushName": "Ana"}]})]
    assert ec.anunciar_nome(cli, s, jid("5511"), "Ana") is False
    assert ec.anunciar_nome(cli, s, jid("5511"), "") is False
    assert ec.anunciar_nome(cli, s, jid("120", "g.us"), "Grupo") is False
    assert ec.anunciar_nome(cli, s, jid("5511"), "Ana B") is True
    assert len(s.enviados) == 2


def test_apelido_de_lid_so_vai_com_o_numero(monkeypatch):
    cli, s, _ = _ligar(monkeypatch, pares={"99@lid": jid("5511")})
    assert ec.anunciar_nome(cli, s, jid("99", "lid"), "Ana") is True
    assert s.enviados[0][1]["data"][0]["remoteJid"] == "5511@c.us"
    # LID sem numero conhecido: o cliente criaria um contato com chave de LID
    assert ec.anunciar_nome(cli, s, jid("77", "lid"), "Beto") is False
    assert len(s.enviados) == 1
