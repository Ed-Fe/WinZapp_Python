"""Presenca: a memoria, os eventos no formato do cliente e as respostas, tudo sem
neonize (os manipuladores sao testados com um `neonize.events` de mentira)."""
import asyncio
import sys
import types
from types import SimpleNamespace as N

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import perfil
import presenca


def jid(usuario, servidor="s.whatsapp.net"):
    return N(User=usuario, Server=servidor)


# ------------------------------------------------------------------ puras
def test_chave_normaliza_numero_c_us_e_dispositivo():
    assert presenca.chave("5511999") == "5511999@c.us"
    assert presenca.chave("5511999@c.us") == "5511999@c.us"
    assert presenca.chave("5511999:12@s.whatsapp.net") == "5511999@c.us"
    assert presenca.chave("99@lid") == "99@lid"
    assert presenca.chave("") == ""
    assert presenca.chaves_da_pessoa("99@lid", "5511999@s.whatsapp.net") == [
        "99@lid", "5511999@c.us"]
    assert presenca.chaves_da_pessoa("5511999@s.whatsapp.net", "5511999@c.us") == [
        "5511999@c.us"]


def test_estado_da_atividade_distingue_texto_audio_e_pausa():
    assert presenca.estado_da_atividade(1, 1) == "composing"
    assert presenca.estado_da_atividade(1, 2) == "recording"
    assert presenca.estado_da_atividade(2, 2) == "paused"
    assert presenca.estado_da_atividade(0, 0) == ""


def test_visto_zerado_ou_negativo_e_escondido():
    assert presenca.visto_de(1700000000) == 1700000000
    assert presenca.visto_de(0) is None
    assert presenca.visto_de(-62135596800) is None


def test_online_vale_ate_expirar():
    m = presenca.Presencas()
    m.definir_presenca(["5511@c.us"], True, None, 1000)
    assert m.ler("5511", 1001) == {"estado": "available", "isOnline": True, "lastSeen": None}
    assert m.ler("5511", 1000 + presenca.VALIDADE_ONLINE) is None
    assert m.ler("outro", 1001) is None


def test_offline_guarda_o_visto_por_ultimo_e_o_novo_online_o_apaga():
    m = presenca.Presencas()
    m.definir_presenca(["5511@c.us", "99@lid"], False, 1699999000, 1000)
    assert m.ler("99@lid", 1010) == {
        "estado": "unavailable", "isOnline": False, "lastSeen": 1699999000}
    m.definir_presenca(["5511@c.us"], True, None, 1020)
    assert m.ler("5511@s.whatsapp.net", 1030)["isOnline"] is True


def test_digitar_vale_poucos_segundos_e_pausa_limpa():
    m = presenca.Presencas()
    m.definir_atividade("5511@c.us", "5511@c.us", "composing", 1000)
    assert m.ler("5511", 1005)["estado"] == "composing"
    assert m.ler("5511", 1005)["isOnline"] is True
    assert m.ler("5511", 1000 + presenca.VALIDADE_ATIVIDADE) is None
    m.definir_atividade("5511@c.us", "5511@c.us", "recording", 2000)
    assert m.ler("5511", 2001)["estado"] == "recording"
    m.definir_atividade("5511@c.us", "5511@c.us", "paused", 2002)
    assert m.ler("5511", 2003) is None


def test_atividade_em_grupo_nao_vira_presenca_privada():
    m = presenca.Presencas()
    m.definir_atividade("120@g.us", "5511@c.us", "composing", 1000)
    assert m.ler("5511", 1001) is None


def test_memoria_tem_limite_de_pessoas(monkeypatch):
    monkeypatch.setattr(presenca, "MAXIMO_DE_PESSOAS", 3)
    m = presenca.Presencas()
    for i in range(5):
        m.definir_presenca([f"{i}@c.us"], True, None, 1000 + i)
    assert m.ler("0", 1010) is None and m.ler("1", 1010) is None
    assert m.ler("4", 1010) is not None


def test_evento_de_presenca_no_formato_do_cliente():
    on = presenca.evento_de_presenca("5511@c.us", True, None, 1700000000.9)
    assert on == {"id": "5511@c.us", "state": "available", "isOnline": True,
                  "t": 1700000000, "isGroup": False, "isUser": True,
                  "snapshotStatus": "ready"}
    off = presenca.evento_de_presenca("5511@c.us", False, 1699999000, 1700000000)
    assert off["state"] == "unavailable" and off["lastSeen"] == 1699999000
    assert off["snapshotStatus"] == "ready"
    escondido = presenca.evento_de_presenca("5511@c.us", False, None, 1700000000)
    assert "lastSeen" not in escondido and "snapshotStatus" not in escondido


def test_evento_de_atividade_privada_e_de_grupo():
    p = presenca.evento_de_atividade("5511@c.us", "5511@c.us", "recording", 5, False)
    assert p == {"id": "5511@c.us", "state": "recording", "t": 5,
                 "isGroup": False, "isUser": True, "isOnline": True}
    pausa = presenca.evento_de_atividade("5511@c.us", "5511@c.us", "paused", 5, False)
    assert "isOnline" not in pausa
    g = presenca.evento_de_atividade("120@g.us", "5511@c.us", "composing", 5, True)
    assert g["isGroup"] is True and g["id"] == "120@g.us"
    assert g["participants"] == [{"id": "5511@c.us", "state": "composing", "shortName": ""}]


def test_respostas_que_o_cliente_aceita():
    sem_dado = presenca.resposta_last_seen(None)
    assert sem_dado == {"response": False, "presence": {"status": "unsupported"}}
    escondido = presenca.resposta_last_seen(
        {"estado": "unavailable", "isOnline": False, "lastSeen": None})
    assert escondido["presence"] == {"status": "unsupported"}
    offline = presenca.resposta_last_seen(
        {"estado": "unavailable", "isOnline": False, "lastSeen": 1699999000})
    assert offline == {"response": 1699999000, "presence": {
        "status": "ready", "isOnline": False, "lastSeen": 1699999000,
        "state": "unavailable"}}
    online = presenca.resposta_last_seen(
        {"estado": "composing", "isOnline": True, "lastSeen": None})
    assert online["presence"] == {"status": "ready", "isOnline": True,
                                  "lastSeen": None, "state": "composing"}
    assert presenca.resposta_online(None) == {
        "response": False, "presence": {"status": "unsupported"}}
    assert presenca.resposta_online(
        {"estado": "available", "isOnline": True, "lastSeen": None}) == {"response": True}


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

    def get_lid_from_pn(self, j):
        return self.pares[f"{j.User}@{j.Server}"]


class _Sessao:
    nome = "s1"

    def __init__(self):
        self.enviados = []

    def emitir(self, evento, dados):
        self.enviados.append((evento, dados))


def _ligar(monkeypatch, pares=None):
    eventos = types.ModuleType("neonize.events")
    eventos.PresenceEv = type("PresenceEv", (), {})
    eventos.ChatPresenceEv = type("ChatPresenceEv", (), {})
    pacote = types.ModuleType("neonize")
    pacote.events = eventos
    monkeypatch.setitem(sys.modules, "neonize", pacote)
    monkeypatch.setitem(sys.modules, "neonize.events", eventos)
    cli, s = _Cli(pares), _Sessao()
    presenca.registrar_eventos(cli, s)
    return cli, s, eventos


def test_evento_de_presenca_guarda_e_emite(monkeypatch):
    cli, s, ev = _ligar(monkeypatch, {"99@lid": N(User="5511", Server="s.whatsapp.net")})
    cli.manipuladores[ev.PresenceEv](
        cli, N(From=jid("99", "lid"), Unavailable=False, LastSeen=0))
    nome, dados = s.enviados[0]
    assert nome == "onpresencechanged"
    assert dados["session"] == "s1" and dados["id"] == "5511@c.us"
    assert dados["isOnline"] is True
    # a memoria responde pelo numero e pelo LID
    import time
    assert s.presencas.ler("5511", time.time())["isOnline"] is True
    assert s.presencas.ler("99@lid", time.time())["isOnline"] is True


def test_evento_de_digitar_privado_e_em_grupo(monkeypatch):
    cli, s, ev = _ligar(monkeypatch)
    cli.get_lid_from_pn = lambda j: (_ for _ in ()).throw(RuntimeError("sem par"))
    origem = N(IsFromMe=False, IsGroup=False, Chat=jid("5511"), Sender=jid("5511"))
    cli.manipuladores[ev.ChatPresenceEv](cli, N(MessageSource=origem, State=1, Media=2))
    assert s.enviados[0][1]["state"] == "recording" and s.enviados[0][1]["id"] == "5511@c.us"
    grupo = N(IsFromMe=False, IsGroup=True, Chat=jid("120", "g.us"), Sender=jid("5522"))
    cli.manipuladores[ev.ChatPresenceEv](cli, N(MessageSource=grupo, State=1, Media=1))
    dados = s.enviados[1][1]
    assert dados["isGroup"] and dados["id"] == "120@g.us"
    assert dados["participants"][0] == {"id": "5522@c.us", "state": "composing", "shortName": ""}
    meu = N(IsFromMe=True, IsGroup=False, Chat=jid("5511"), Sender=jid("5511"))
    cli.manipuladores[ev.ChatPresenceEv](cli, N(MessageSource=meu, State=1, Media=1))
    assert len(s.enviados) == 2


# ------------------------------------------------------------ rotas
def _app(memoria):
    app = web.Application()
    sessao = N(cliente=None, status="CLOSED", eu="", presencas=memoria)
    ok = lambda **kw: web.json_response(kw)
    perfil.registrar(app.router, sessao=lambda req: sessao, ok=ok,
                     jid_de_texto=lambda t: t, jid_texto=lambda j: str(j))
    presenca.registrar(app.router, sessao=lambda req: sessao, ok=ok)
    return app


def _pegar(memoria, caminho):
    async def cenario():
        async with TestClient(TestServer(_app(memoria))) as c:
            r = await c.get(caminho)
            return r.status, await r.json()
    return asyncio.run(cenario())


def test_rotas_respondem_da_memoria(monkeypatch):
    import time
    memoria = presenca.Presencas()
    memoria.definir_presenca(["5511@c.us"], False, 1699999000, time.time())
    memoria.definir_presenca(["5522@c.us"], True, None, time.time())
    status, corpo = _pegar(memoria, "/api/tok/last-seen/5511@c.us")
    assert status == 200 and corpo["presence"]["lastSeen"] == 1699999000
    assert corpo["presence"]["status"] == "ready"
    _, corpo = _pegar(memoria, "/api/tok/last-seen/5999")
    assert corpo["presence"] == {"status": "unsupported"}
    assert _pegar(memoria, "/api/tok/chat-is-online/5522")[1]["response"] is True
    assert _pegar(memoria, "/api/tok/chat-is-online/5511")[1]["response"] is False
    assert _pegar(memoria, "/api/tok/chat-is-online/120-4@g.us")[0] == 400


def test_sem_memoria_as_rotas_dizem_unsupported():
    assert _pegar(None, "/api/tok/last-seen/5511")[1]["presence"] == {"status": "unsupported"}
    assert _pegar(None, "/api/tok/chat-is-online/5511")[1]["response"] is False
