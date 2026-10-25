"""Rotas restantes: a plataforma de uma mensagem, os termos da Meta AI e os 501."""
import asyncio
import ctypes
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


class GoFalso:
    """Imita a CDLL do goneonize: AcceptTOSNotice devolve um ponteiro para uma
    string em memoria (ou 0) e FreeString anota o que liberou."""

    def __init__(self, resposta=b""):
        self._buffer = ctypes.create_string_buffer(resposta) if resposta else None
        self.chamadas = []
        self.liberados = []

        # funcao simples (nao metodo), como a da CDLL: aceita argtypes e restype
        def aceitar(uuid, aviso, estagio):
            self.chamadas.append((uuid, aviso, estagio))
            return ctypes.addressof(self._buffer) if self._buffer else None
        self.AcceptTOSNotice = aceitar

    def FreeString(self, ponteiro):
        self.liberados.append(ponteiro)


def test_aceitar_aviso_devolve_vazio_quando_o_go_nao_reclama():
    go = GoFalso()
    assert restantes.aceitar_aviso(go, b"u1", "20250502", "5") == ""
    assert go.chamadas == [(b"u1", b"20250502", b"5")]
    assert go.AcceptTOSNotice.restype is ctypes.c_void_p
    assert go.AcceptTOSNotice.argtypes == [ctypes.c_char_p] * 3
    assert go.liberados == []


def test_aceitar_aviso_le_o_erro_do_go_e_libera_o_ponteiro():
    go = GoFalso(b"info query returned status 400")
    assert restantes.aceitar_aviso(go, b"u1", "20250502", "5") == \
        "info query returned status 400"
    assert go.liberados == [ctypes.addressof(go._buffer)]


def test_go_do_cliente_acha_o_atributo_do_cliente_go():
    class Cliente:
        _NewClient__client = "go"
    assert restantes.go_do_cliente(Cliente()) == "go"
    assert restantes.go_do_cliente(object()) is None


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
    assert len(saidas) == 8
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


def test_accept_chama_o_go_e_responde_o_estado_que_o_cliente_confere():
    go = GoFalso()
    (status, corpo), = rodar(sessao(go), [("POST", "/meta-ai-terms/accept")])
    assert status == 200 and corpo["response"]["state"] == "ACCEPTED"
    assert go.chamadas == [(b"u1", b"20250502", b"5")]


def test_accept_com_erro_do_whatsapp_e_502_sem_dizer_aceito():
    go = GoFalso(b"recusado")
    (status, corpo), = rodar(sessao(go), [("POST", "/meta-ai-terms/accept")])
    assert status == 502 and corpo["code"] == "meta_ai_terms_failed"
    assert "response" not in corpo


def test_accept_sem_a_funcao_no_go_e_501_e_desconectado_e_400():
    class GoSemFuncao:
        pass
    (status, corpo), = rodar(sessao(GoSemFuncao()), [("POST", "/meta-ai-terms/accept")])
    assert status == 501 and corpo["code"] == "meta_ai_terms_unsupported"
    (status, _), = rodar(sessao(GoFalso(), status="CLOSED"), [("POST", "/meta-ai-terms/accept")])
    assert status == 400


def test_get_platform_from_message_responde_a_palavra_em_json():
    saidas = rodar(sessao(), [
        ("GET", "/get-platform-from-message/false_5511@c.us_3EB0AA"),
        ("GET", "/get-platform-from-message/true_5511@c.us_3AFF00")])
    assert saidas == [(200, "web"), (200, "iphone")]
