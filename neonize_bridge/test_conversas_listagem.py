"""Rotas de leitura de conversas: as funcoes puras e as rotas contra um armazem
de verdade (sem neonize e sem conexao)."""
import asyncio
from types import SimpleNamespace as N

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import conversas_listagem as cl
import msgmap
from store import Armazem


def id_wpp(jid):
    usuario, _, servidor = msgmap.jid_wpp(jid).partition("@")
    return {"server": servidor, "user": usuario, "_serialized": f"{usuario}@{servidor}"}


def linha(jid, **campos):
    base = {"jid": jid, "name": "", "ts": 0, "unread": 0, "archived": 0,
            "pinned": 0, "muted": 0, "locked": 0}
    return {**base, **campos}


def msg(mid, de_mim=False, t=1):
    return {"id": {"id": mid, "fromMe": de_mim}, "fromMe": de_mim, "t": t}


def test_booleano():
    assert cl.booleano(None, True) is True
    assert cl.booleano("", False) is False
    assert cl.booleano("true", False) is True
    assert cl.booleano("False", True) is False
    assert cl.booleano("0", True) is False


def test_jid_da_conversa():
    assert cl.jid_da_conversa("5511@c.us") == "5511@s.whatsapp.net"
    assert cl.jid_da_conversa("5511") == "5511@s.whatsapp.net"
    assert cl.jid_da_conversa("1203", grupo=True) == "1203@g.us"
    assert cl.jid_da_conversa("1203@g.us", grupo=True) == "1203@g.us"
    assert cl.jid_da_conversa("9@lid") == "9@lid"


def test_conversa_para_wpp_segue_o_formato_do_list_chats():
    c = linha("5511@s.whatsapp.net", unread=2, ts=50, archived=1, pinned=7, muted=99)
    saida = cl.conversa_para_wpp(c, {"5511@c.us": "Ana"}, id_wpp)
    assert saida == {
        "id": id_wpp("5511@s.whatsapp.net"), "name": "Ana", "isGroup": False,
        "unreadCount": 2, "t": 50, "archive": True, "pin": 7,
        "muteExpiration": 99, "isLocked": False, "msgs": None}
    assert cl.conversa_para_wpp(linha("1@g.us", name="Grupo"), {}, id_wpp)["isGroup"] is True


def test_selecionar_arquivadas_transmissao_e_silenciadas():
    todas = [linha("a@s.whatsapp.net", archived=1), linha("b@s.whatsapp.net", muted=500),
             linha("c@s.whatsapp.net", muted=5), linha("lista@broadcast"),
             linha("status@broadcast", archived=1)]
    assert [c["jid"] for c in cl.selecionar(todas, "todas")] == [
        "a@s.whatsapp.net", "b@s.whatsapp.net", "c@s.whatsapp.net", "lista@broadcast"]
    assert [c["jid"] for c in cl.selecionar(todas, "arquivadas")] == ["a@s.whatsapp.net"]
    assert [c["jid"] for c in cl.selecionar(todas, "transmissao")] == ["lista@broadcast"]
    assert [c["jid"] for c in cl.selecionar(todas, "silenciadas", agora=100)] == ["b@s.whatsapp.net"]


def test_do_tipo_filtra_grupos_e_pessoas():
    cs = [linha("1@g.us"), linha("2@s.whatsapp.net"), linha("3@lid")]
    assert len(cl.do_tipo(cs, "all")) == 3
    assert [c["jid"] for c in cl.do_tipo(cs, "group")] == ["1@g.us"]
    assert [c["jid"] for c in cl.do_tipo(cs, "user")] == ["2@s.whatsapp.net", "3@lid"]
    assert cl.do_tipo(cs, "community") == []


def test_nao_lidas_recebidas_pega_as_ultimas_que_nao_sao_minhas():
    itens = [msg("1"), msg("2", de_mim=True), msg("3"), msg("4")]
    assert [m["id"]["id"] for m in cl.nao_lidas_recebidas(itens, 2)] == ["3", "4"]
    assert cl.nao_lidas_recebidas(itens, 0) == []
    assert [m["id"]["id"] for m in cl.sem_minhas(itens, False)] == ["1", "3", "4"]
    assert len(cl.sem_minhas(itens, True)) == 4


def pedir(tmp_path, metodo, caminho, preparar):
    """Sobe so as rotas de leitura com um armazem real e faz um pedido."""
    bd = Armazem(str(tmp_path / "x.db"))
    preparar(bd)
    s = N(bd=bd, votos_wpp=lambda serial: None)

    async def rodar():
        app = web.Application()
        cl.registrar(
            app.router, sessao=lambda req: s,
            ok=lambda **kw: web.json_response(kw),
            erro=lambda m, status=500: web.json_response(
                {"status": "error", "message": str(m)}, status=status),
            id_wpp=id_wpp, agora=lambda: 100)
        async with TestClient(TestServer(app)) as cliente:
            resp = await cliente.request(metodo, caminho)
            return resp.status, await resp.json()
    return asyncio.run(rodar())


def popular(bd):
    bd.conversa("a@s.whatsapp.net", name="Ana", ts=10, unread=1, archived=0)
    bd.conversa("b@s.whatsapp.net", name="Bia", ts=20, archived=1)
    bd.mensagem("a@s.whatsapp.net", msg("m1", t=10))
    bd.mensagem("a@s.whatsapp.net", msg("m2", de_mim=True, t=11))


def test_rota_all_chats_archived(tmp_path):
    status, corpo = pedir(tmp_path, "GET", "/api/t/all-chats-archived", popular)
    assert status == 200
    assert [c["name"] for c in corpo["response"]] == ["Bia"]


def test_rota_all_chats_with_messages_so_com_mensagens(tmp_path):
    status, corpo = pedir(tmp_path, "GET", "/api/t/all-chats-with-messages", popular)
    assert [c["name"] for c in corpo["response"]] == ["Ana"]
    assert len(corpo["response"][0]["msgs"]) == 1
    status, corpo = pedir(tmp_path, "GET",
                          "/api/t/all-chats-with-messages?withNewMessageOnly=true", popular)
    assert [c["name"] for c in corpo["response"]] == ["Ana"]


def test_rota_chat_by_id_acha_ou_404(tmp_path):
    status, corpo = pedir(tmp_path, "GET", "/api/t/chat-by-id/a@c.us", popular)
    assert status == 200 and corpo["response"]["name"] == "Ana"
    status, corpo = pedir(tmp_path, "GET", "/api/t/chat-by-id/zzz@c.us", popular)
    assert status == 404 and corpo["status"] == "error"


def test_rota_all_messages_in_chat_respeita_include_me(tmp_path):
    status, corpo = pedir(tmp_path, "GET", "/api/t/all-messages-in-chat/a@c.us", popular)
    assert [m["id"]["id"] for m in corpo["response"]] == ["m1", "m2"]
    status, corpo = pedir(tmp_path, "GET",
                          "/api/t/load-messages-in-chat/a@c.us?includeMe=false", popular)
    assert [m["id"]["id"] for m in corpo["response"]] == ["m1"]


def test_rota_unread_messages(tmp_path):
    status, corpo = pedir(tmp_path, "GET", "/api/t/all-unread-messages", popular)
    assert [m["id"]["id"] for m in corpo["response"]] == ["m1"]
    status, corpo = pedir(tmp_path, "GET", "/api/t/unread-messages", popular)
    assert corpo["response"][0]["name"] == "Ana"
    assert [m["id"]["id"] for m in corpo["response"][0]["messages"]] == ["m1"]


def test_rota_list_mutes_e_votes(tmp_path):
    def com_silencio(bd):
        popular(bd)
        bd.conversa("b@s.whatsapp.net", muted=2**31 - 1)
    status, corpo = pedir(tmp_path, "GET", "/api/t/list-mutes/all", com_silencio)
    assert [c["name"] for c in corpo["response"]] == ["Bia"]
    status, corpo = pedir(tmp_path, "GET", "/api/t/votes/true_a@c.us_m9", popular)
    assert status == 404
