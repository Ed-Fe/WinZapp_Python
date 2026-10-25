"""Rotas de envio: as funcoes puras e as rotas que nao precisam do neonize
(validacao do corpo e desconexao)."""
import asyncio
import base64
from types import SimpleNamespace as N

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import conversas_envios as ce


def test_coordenadas_aceita_numeros_e_texto():
    assert ce.coordenadas("-23.5", 46.6) == (-23.5, 46.6)


@pytest.mark.parametrize("lat,lng", [("x", 1), (None, 1), (91, 0), (0, 181)])
def test_coordenadas_recusa_o_invalido(lat, lng):
    with pytest.raises(ValueError):
        ce.coordenadas(lat, lng)


def test_dados_de_base64_com_e_sem_prefixo():
    b64 = base64.b64encode(b"abc").decode()
    assert ce.dados_de_base64(b64) == (b"abc", "")
    assert ce.dados_de_base64("data:Image/PNG;base64," + b64) == (b"abc", "image/png")
    assert ce.dados_de_base64("data:;base64," + b64) == (b"abc", "")


@pytest.mark.parametrize("texto", ["", "###", "data:image/png;base64,"])
def test_dados_de_base64_recusa_o_invalido(texto):
    with pytest.raises(ValueError):
        ce.dados_de_base64(texto)


def test_fonte_do_arquivo_aceita_so_base64_e_data_uri():
    assert ce.fonte_do_arquivo(base64.b64encode(b"zz").decode())[0] == b"zz"
    uri = "data:image/webp;base64," + base64.b64encode(b"zz").decode()
    assert ce.fonte_do_arquivo(uri) == (b"zz", "image/webp")


@pytest.mark.parametrize("texto", [
    "https://a.com/x.webp",
    "http://127.0.0.1:6311/api/x",
    "http://localhost/x",
    "http://169.254.169.254/latest/meta-data/",
    "http://0.0.0.0/",
    "http://[::ffff:127.0.0.1]/",
    "http://[::1]/",
    "http://2130706433/",      # 127.0.0.1 em decimal
    "http://0177.0.0.1/",      # octal
    "http://0x7f.0.0.1/",      # hexa
    "  HTTPS://A.COM/X.PNG  ",
    "ftp://a.com/x",
    "file:///etc/passwd",
    "gopher://127.0.0.1:6379/_x",
])
def test_fonte_do_arquivo_recusa_url_de_qualquer_esquema(texto):
    with pytest.raises(ValueError, match="url sources"):
        ce.fonte_do_arquivo(texto)


@pytest.mark.parametrize("texto", [
    "/etc/passwd", "C:\\Users\\a\\x.png", "\\\\servidor\\pasta\\x.png",
    "../x.png", "file:/etc/passwd", "http:/127.0.0.1/", "x.png"])
def test_fonte_do_arquivo_recusa_caminho_de_disco(texto):
    with pytest.raises(ValueError):
        ce.fonte_do_arquivo(texto)


def test_tipo_do_arquivo_pelo_mime_ou_pelo_nome():
    assert ce.tipo_do_arquivo("image/png") == "image"
    assert ce.tipo_do_arquivo("video/mp4") == "video"
    assert ce.tipo_do_arquivo("audio/ogg; codecs=opus") == "audio"
    assert ce.tipo_do_arquivo("application/pdf") == "document"
    assert ce.tipo_do_arquivo("", "foto.jpg") == "image"
    assert ce.tipo_do_arquivo("", "sem-extensao") == "document"


def test_com_grupo():
    assert ce.com_grupo(["1203"], True) == "1203@g.us"
    assert ce.com_grupo("1203", False) == "1203"
    assert ce.com_grupo("55@c.us", True) == "55@c.us"


def pedir(caminho, corpo, conectada):
    s = N(cliente=object() if conectada else None, status="CONNECTED" if conectada else "CLOSED")
    chamadas = []

    async def rodar():
        app = web.Application()
        ce.registrar(
            app.router, sessao=lambda req: s, ok=lambda **kw: web.json_response(kw),
            erro=lambda m, status=500: web.json_response(
                {"status": "error", "message": str(m)}, status=status),
            destino_de=lambda fone: chamadas.append(fone) or fone,
            guardar_enviada=None, enviar_midia=None, contexto_citado=None,
            em_thread=None)
        async with TestClient(TestServer(app)) as cliente:
            resp = await cliente.post(caminho, json=corpo)
            return resp.status, await resp.json()
    return asyncio.run(rodar()), chamadas


def test_rota_sem_campo_obrigatorio_e_400():
    (status, corpo), _ = pedir("/api/t/send-location", {"phone": "55"}, True)
    assert status == 400 and corpo["status"] == "error"


def test_rota_com_coordenada_invalida_e_400():
    (status, _), _ = pedir("/api/t/send-location", {"phone": "55", "lat": 99, "lng": 0}, True)
    assert status == 400


def test_rota_de_arquivo_com_base64_invalido_e_400():
    (status, _), _ = pedir("/api/t/send-image", {"phone": "55", "base64": "###"}, True)
    assert status == 400


def test_rota_de_voz_recusa_caminho_de_disco():
    (status, _), _ = pedir("/api/t/send-voice", {"phone": "55", "path": "/etc/passwd"}, True)
    assert status == 400


@pytest.mark.parametrize("rota,campo", [
    ("send-voice", "path"), ("send-sticker", "path"), ("send-sticker", "base64"),
    ("send-sticker-gif", "path"), ("send-sticker-gif", "base64")])
def test_rotas_de_voz_e_figurinha_recusam_url_sem_baixar(rota, campo):
    # conectada: se a URL passasse da validacao, a rota chegaria ao neonize
    (status, corpo), _ = pedir(
        "/api/t/" + rota, {"phone": "55", campo: "http://169.254.169.254/latest/"}, True)
    assert status == 400 and "url sources" in corpo["message"]


def test_rota_sem_conexao_responde_400_sem_enviar():
    (status, corpo), _ = pedir(
        "/api/t/send-location", {"phone": "55", "lat": 1, "lng": 2}, False)
    assert status == 404 and corpo["status"] == "Disconnected"


def test_isgroup_vira_jid_de_grupo_no_destino():
    _, chamadas = pedir("/api/t/send-location",
                        {"phone": "1203", "isGroup": True, "lat": 1, "lng": 2}, False)
    assert chamadas == ["1203@g.us"]


def test_send_link_preview_responde_501_e_nunca_chega_ao_neonize():
    """O neonize buscaria a pagina (e a og:image) da maquina do usuario: SSRF."""
    for conectada in (True, False):
        (status, corpo), chamadas = pedir(
            "/api/t/send-link-preview",
            {"phone": "55", "url": "http://169.254.169.254/latest/"}, conectada)
        assert status == 501 and "link previews are not available" in corpo["message"]
        assert chamadas == []
