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


def test_texto_do_link():
    assert ce.texto_do_link("https://a.com") == "https://a.com"
    assert ce.texto_do_link("https://a.com", "veja") == "veja https://a.com"
    assert ce.texto_do_link("https://a.com", "veja https://a.com") == "veja https://a.com"
    with pytest.raises(ValueError):
        ce.texto_do_link("ftp://a.com")
    with pytest.raises(ValueError):
        ce.texto_do_link("")


def test_dados_de_base64_com_e_sem_prefixo():
    b64 = base64.b64encode(b"abc").decode()
    assert ce.dados_de_base64(b64) == (b"abc", "")
    assert ce.dados_de_base64("data:Image/PNG;base64," + b64) == (b"abc", "image/png")
    assert ce.dados_de_base64("data:;base64," + b64) == (b"abc", "")


@pytest.mark.parametrize("texto", ["", "###", "data:image/png;base64,"])
def test_dados_de_base64_recusa_o_invalido(texto):
    with pytest.raises(ValueError):
        ce.dados_de_base64(texto)


def test_fonte_do_arquivo_aceita_url_e_base64_mas_nunca_caminho_de_disco():
    assert ce.fonte_do_arquivo("https://a.com/x.webp") == ("https://a.com/x.webp", "")
    assert ce.fonte_do_arquivo(base64.b64encode(b"zz").decode())[0] == b"zz"
    with pytest.raises(ValueError):
        ce.fonte_do_arquivo("/etc/passwd")
    with pytest.raises(ValueError):
        ce.fonte_do_arquivo("C:\\Users\\a\\x.png")


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


def test_rota_sem_conexao_responde_400_sem_enviar():
    (status, corpo), _ = pedir(
        "/api/t/send-link-preview", {"phone": "55", "url": "https://a.com"}, False)
    assert status == 400 and corpo["message"] == "Disconnected"


def test_isgroup_vira_jid_de_grupo_no_destino():
    _, chamadas = pedir("/api/t/send-link-preview",
                        {"phone": "1203", "isGroup": True, "url": "https://a.com"}, False)
    assert chamadas == ["1203@g.us"]
