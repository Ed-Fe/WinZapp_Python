"""Rotas restantes: a plataforma de uma mensagem, os termos da Meta AI e os 501."""
import asyncio
from types import SimpleNamespace as N

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import restantes


@pytest.mark.parametrize("serial,esperado", [
    ("false_5511999@c.us_3EB0ABCDEF0123456789", "web"),
    ("true_5511999@c.us_3A1B2C3D4E5F60718293", "iphone"),
    ("false_5511999@c.us_" + "A" * 22, "android"),
    ("false_5511999@c.us_ABCDEF0123456789ABCD", "unknown"),
    # em grupo o id e o terceiro pedaco; o participante vem depois
    ("false_120363-1@g.us_3EB0ABCDEF_5511888@c.us", "web"),
    ("3A1B2C3D4E5F", "iphone"),
    ("", "unknown"),
    (None, "unknown"),
])
def test_plataforma_da_mensagem_segue_as_regras_do_wa_js(serial, esperado):
    assert restantes.plataforma_da_mensagem(serial) == esperado


def test_plataforma_no_limite_de_21_caracteres():
    assert restantes.plataforma_da_mensagem("x_y_" + "B" * 21) == "unknown"
    assert restantes.plataforma_da_mensagem("x_y_" + "B" * 22) == "android"


def rodar(s, requisicoes):
    """Sobe um servidor aiohttp so com estas rotas e faz os pedidos."""
    async def principal():
        app = web.Application()
        restantes.registrar(app.router, sessao=lambda req: s,
                            ok=lambda **kw: web.json_response(kw))
        async with TestClient(TestServer(app)) as http:
            saidas = []
            for metodo, caminho in requisicoes:
                resp = await http.request(metodo, "/api/tok" + caminho)
                saidas.append((resp.status, await resp.json()))
            return saidas
    return asyncio.run(principal())


def sessao(go=None, status="CONNECTED"):
    cliente = N(uuid=b"u1", **{"_NewClient__client": go}) if go is not None \
        else N(uuid=b"u1")
    return N(cliente=cliente, status=status)


def test_rotas_impossiveis_respondem_501_com_codigo_e_motivo():
    s = sessao()
    pedidos = [(m, "/" + r.replace("{communityId}", "1@g.us").replace("{subgroupId}", "2@g.us")
                .replace("{id}", "123")) for m, r, _, _ in restantes.IMPOSSIVEIS]
    saidas = rodar(s, pedidos)
    assert len(saidas) == len(restantes.IMPOSSIVEIS)
    for (status, corpo), (_, _, codigo, _) in zip(saidas, restantes.IMPOSSIVEIS):
        assert status == 501 and corpo["status"] == "error"
        assert corpo["code"] == codigo and corpo["message"]


def test_nenhuma_rota_impossivel_e_a_de_ultima_vez_ou_de_presenca():
    rotas = {r for _, r, _, _ in restantes.IMPOSSIVEIS}
    assert not any("last-seen" in r or "chat-is-online" in r for r in rotas)


def test_meta_ai_terms_diz_unknown_sem_precisar_de_conexao():
    (status, corpo), = rodar(sessao(status="CLOSED"), [("GET", "/meta-ai-terms")])
    assert status == 200
    assert corpo["response"] == {"noticeId": "20250502", "state": "UNKNOWN"}


def test_aceitar_os_termos_da_meta_ai_responde_501_e_nao_registra_consentimento():
    (status, corpo), = rodar(sessao(), [("POST", "/meta-ai-terms/accept")])
    assert status == 501 and corpo["code"] == "meta_ai_terms_unsupported"
    assert "response" not in corpo


def test_restantes_nao_chama_codigo_nativo():
    """O consentimento nao e registrado por ctypes: nada de AcceptTOSNotice aqui."""
    codigo = open(restantes.__file__, encoding="utf-8").read()
    assert "ctypes" not in codigo


def test_get_platform_from_message_responde_a_palavra_em_json():
    saidas = rodar(sessao(), [
        ("GET", "/get-platform-from-message/false_5511@c.us_3EB0AA"),
        ("GET", "/get-platform-from-message/true_5511@c.us_3AFF00")])
    assert saidas == [(200, "web"), (200, "iphone")]
