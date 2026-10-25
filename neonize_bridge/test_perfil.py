"""Rotas de perfil e privacidade: as funcoes puras que montam o que o cliente le."""
from types import SimpleNamespace as N

import perfil


def test_jid_do_perfil_aceita_as_formas_de_id():
    assert perfil.jid_do_perfil("5511999") == "5511999@s.whatsapp.net"
    assert perfil.jid_do_perfil("5511999@c.us") == "5511999@s.whatsapp.net"
    assert perfil.jid_do_perfil("333@lid") == "333@lid"
    assert perfil.jid_do_perfil("1203-4455", True) == "1203-4455@g.us"
    assert perfil.jid_do_perfil("1203-4455@g.us", True) == "1203-4455@g.us"


def test_so_pessoa_vale_para_last_seen():
    for bom in ("5511999", "5511999@c.us", "5511999@s.whatsapp.net", "333@lid"):
        assert perfil.id_de_pessoa_valido(bom)
    for ruim in ("", "1203-4455@g.us", "x@broadcast", "5511999@c.us/../x", "abc"):
        assert not perfil.id_de_pessoa_valido(ruim)


def test_ajuste_valido_so_aceita_o_que_o_whatsapp_aceita():
    assert perfil.ajuste_valido("lastSeen", "contacts")
    assert perfil.ajuste_valido("online", "match_last_seen")
    assert perfil.ajuste_valido("readReceipts", "none")
    assert not perfil.ajuste_valido("online", "none")
    assert not perfil.ajuste_valido("groupAdd", "none")
    assert not perfil.ajuste_valido("desconhecido", "all")
    assert not perfil.ajuste_valido("lastSeen", None)
    assert not perfil.ajuste_valido(None, "all")


def test_todo_ajuste_do_cliente_tem_um_tipo_do_neonize():
    # os seis ajustes que o dialogo de privacidade do WinZapp conhece
    assert set(perfil.AJUSTES) == {"lastSeen", "online", "about", "profilePicture",
                                   "readReceipts", "groupAdd"}
    assert set(perfil.CAMPOS_PRIVACIDADE.values()) == set(perfil.AJUSTES)


def test_ajustes_para_wpp_traduz_o_enum_e_deixa_none_o_que_nao_se_sabe():
    p = N(LastSeen=3, Online=5, Status=7, Profile=2, ReadReceipts=2, GroupAdd=1)
    assert perfil.ajustes_para_wpp(p) == {
        "lastSeen": "contacts", "online": "match_last_seen", "about": "none",
        "profilePicture": "all", "readReceipts": "all", "groupAdd": None}
    assert perfil.ajustes_para_wpp(N(LastSeen=4))["lastSeen"] == "contact_blacklist"
    assert perfil.ajustes_para_wpp(N())["lastSeen"] is None


def test_sem_foto_e_resposta_definitiva_e_outro_erro_nao():
    assert perfil.sem_foto(Exception("that user or group does not have a profile picture"))
    assert perfil.sem_foto(Exception("the user has hidden their profile picture from you"))
    assert perfil.sem_foto(Exception("iq error: not-authorized"))
    assert not perfil.sem_foto(Exception("timed out"))


def test_foto_para_wpp_traz_as_chaves_que_o_cliente_tenta():
    info = N(URL="https://pps.whatsapp.net/a.jpg", ID="123")
    assert perfil.foto_para_wpp(info, "5511@c.us") == {
        "eurl": info.URL, "img": info.URL, "imgFull": info.URL, "id": "5511@c.us", "tag": "123"}
    assert perfil.foto_para_wpp(N(URL="", ID=""), "x") is None
    assert perfil.foto_para_wpp(None, "x") is None


def test_recado_e_o_da_primeira_pessoa_que_tem():
    infos = [N(UserInfo=N(Status="")), N(UserInfo=N(Status="Ocupado"))]
    assert perfil.recado_de(infos) == "Ocupado"
    assert perfil.recado_de([]) == ""
    assert perfil.recado_de(None) == ""


def test_so_jpeg_vai_como_foto_de_perfil():
    assert perfil.e_jpeg(b"\xff\xd8\xff\xe0abc")
    assert not perfil.e_jpeg(b"\x89PNG\r\n\x1a\n")
    assert not perfil.e_jpeg(b"")


def _imagem(formato):
    import io

    from PIL import Image
    saida = io.BytesIO()
    Image.new("RGB", (16, 16), "blue").save(saida, format=formato)
    return saida.getvalue()


def test_foto_de_perfil_converte_png_e_deixa_o_jpeg_passar():
    jpeg = _imagem("JPEG")
    assert perfil.foto_de_perfil(jpeg) is jpeg
    assert perfil.e_jpeg(perfil.foto_de_perfil(_imagem("PNG")))


def test_foto_de_perfil_recusa_o_que_nao_e_imagem():
    import pytest
    with pytest.raises(Exception):
        perfil.foto_de_perfil(b"texto qualquer")


def test_perfil_proprio_marca_none_o_que_nao_se_leu():
    assert perfil.perfil_proprio(" Ana ", "Ocupada", True) == {
        "name": "Ana", "status": "Ocupada", "photo": True}
    assert perfil.perfil_proprio(None, "", None) == {
        "name": None, "status": None, "photo": None}
    assert perfil.perfil_proprio("Ana", None, False)["photo"] is False


def _app_com_rotas():
    from aiohttp import web
    app = web.Application()
    sessao = N(cliente=None, status="CLOSED", eu="")
    perfil.registrar(app.router, sessao=lambda req: sessao,
                     ok=lambda **kw: web.json_response(kw),
                     jid_de_texto=lambda t: t, jid_texto=lambda j: str(j))
    return app


def test_last_seen_responde_unsupported_como_o_cliente_entende():
    import asyncio
    from aiohttp.test_utils import TestClient, TestServer

    async def cenario():
        async with TestClient(TestServer(_app_com_rotas())) as c:
            boa = await c.get("/api/tok/last-seen/5511999@c.us")
            ruim = await c.get("/api/tok/last-seen/1203-4455@g.us")
            return boa.status, await boa.json(), ruim.status
    status, corpo, status_ruim = asyncio.run(cenario())
    assert status == 200 and corpo["presence"] == {"status": "unsupported"}
    assert status_ruim == 400


def test_sem_conexao_as_rotas_de_privacidade_dizem_privacy_unavailable():
    import asyncio
    from aiohttp.test_utils import TestClient, TestServer

    async def cenario():
        async with TestClient(TestServer(_app_com_rotas())) as c:
            lida = await c.post("/api/tok/privacy", json={})
            invalida = await c.post("/api/tok/privacy/set",
                                    json={"setting": "online", "value": "none"})
            return lida.status, await lida.json(), invalida.status, await invalida.json()
    lida, corpo, invalida, corpo_invalida = asyncio.run(cenario())
    assert lida == 503 and corpo["code"] == "privacy_unavailable"
    assert invalida == 400 and corpo_invalida["code"] == "invalid_privacy_setting"
