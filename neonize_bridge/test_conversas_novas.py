"""all-new-messages (cursor por sessao sobre o armazem) e send-status."""
import asyncio
from types import SimpleNamespace as N

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import conversas_novas as cn
from store import Armazem


def msg(id_, de_mim=False, tipo="chat", status=False, t=100):
    return {"id": {"id": id_, "_serialized": "x_" + id_}, "fromMe": de_mim, "type": tipo,
            "isStatus": status, "t": t, "body": id_}


def test_so_conta_o_que_outras_pessoas_mandaram():
    assert cn.e_nova_recebida(msg("a"))
    assert not cn.e_nova_recebida(msg("a", de_mim=True))
    assert not cn.e_nova_recebida(msg("a", status=True))
    assert not cn.e_nova_recebida(msg("a", tipo="revoked"))


def test_novas_e_cursor_passa_por_todas_as_linhas_lidas():
    linhas = [(5, msg("a")), (6, msg("b", de_mim=True)), (7, msg("c"))]
    novas, cursor = cn.novas_e_cursor(linhas, 4)
    assert [m["id"]["id"] for m in novas] == ["a", "c"] and cursor == 7
    assert cn.novas_e_cursor([(8, msg("d", de_mim=True))], 7) == ([], 8)
    assert cn.novas_e_cursor([], 7) == ([], 7)


def test_texto_do_status():
    assert cn.texto_do_status({"message": "oi"}) == "oi"
    assert cn.texto_do_status({"text": "oi"}) == "oi"
    for ruim in ({}, {"message": "  "}, {"message": 3}, [], None):
        with pytest.raises(ValueError):
            cn.texto_do_status(ruim)


def test_armazem_devolve_so_o_que_veio_depois_do_cursor(tmp_path):
    bd = Armazem(str(tmp_path / "x.db"))
    assert cn.ultimo_rowid(bd) == 0
    bd.mensagem("a@s.whatsapp.net", msg("m1"))
    inicio = cn.ultimo_rowid(bd)
    bd.mensagem("b@s.whatsapp.net", msg("m2"))
    bd.mensagem("a@s.whatsapp.net", msg("m3"))
    assert [m["id"]["id"] for _, m in cn.guardadas_depois(bd, inicio)] == ["m2", "m3"]
    assert [m["id"]["id"] for _, m in cn.guardadas_depois(bd, inicio, limite=1)] == ["m2"]
    assert cn.guardadas_depois(bd, cn.ultimo_rowid(bd)) == []


def servidor(tmp_path):
    bd = Armazem(str(tmp_path / "x.db"))
    s = N(nome="s1", bd=bd)
    publicados = []

    async def publicar_status(req, montar):
        publicados.append(montar("S", "DESTINO", await req.json()))
        return web.json_response({"status": "success"})

    def status_de_texto(sessao, destino, corpo):
        return (sessao, destino, corpo)
    app = web.Application()
    cn.registrar(app.router, sessao=lambda req: s, ok=lambda **kw: web.json_response(kw),
                 erro=lambda m, status=500: web.json_response(
                     {"status": "error", "message": str(m)}, status=status),
                 publicar_status=publicar_status, status_de_texto=status_de_texto)
    return bd, app, publicados


def test_all_new_messages_primeira_chamada_marca_o_inicio(tmp_path):
    bd, app, _ = servidor(tmp_path)
    bd.mensagem("a@s.whatsapp.net", msg("antiga"))

    async def rodar():
        async with TestClient(TestServer(app)) as c:
            primeira = await (await c.get("/api/t/all-new-messages")).json()
            bd.mensagem("a@s.whatsapp.net", msg("nova1"))
            bd.mensagem("a@s.whatsapp.net", msg("minha", de_mim=True))
            bd.mensagem("b@s.whatsapp.net", msg("nova2"))
            segunda = await (await c.get("/api/t/all-new-messages")).json()
            terceira = await (await c.get("/api/t/all-new-messages")).json()
            bd.mensagem("a@s.whatsapp.net", msg("nova3"))
            quarta = await (await c.get("/api/t/all-new-messages")).json()
            return primeira, segunda, terceira, quarta
    primeira, segunda, terceira, quarta = asyncio.run(rodar())
    assert primeira["response"] == []
    assert [m["id"]["id"] for m in segunda["response"]] == ["nova1", "nova2"]
    assert terceira["response"] == []
    assert [m["id"]["id"] for m in quarta["response"]] == ["nova3"]


def test_send_status_publica_o_texto(tmp_path):
    _, app, publicados = servidor(tmp_path)

    async def rodar():
        async with TestClient(TestServer(app)) as c:
            ruim = await c.post("/api/t/send-status", json={"message": ""})
            bom = await c.post("/api/t/send-status", json={"message": "oi", "options": {"font": 2}})
            return ruim.status, bom.status
    ruim, bom = asyncio.run(rodar())
    assert (ruim, bom) == (400, 200)
    assert publicados == [("S", "DESTINO", {"text": "oi", "options": {"font": 2}})]
