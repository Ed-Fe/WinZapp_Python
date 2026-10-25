"""Quem pode falar com a ponte: segredo, token da sessao, rota e Host."""
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import autenticacao as aut

SEGREDO = "s" * 64


def test_o_segredo_e_criado_uma_vez_e_relido(tmp_path):
    primeiro = aut.carregar_segredo(str(tmp_path))
    assert len(primeiro) == 64
    assert aut.carregar_segredo(str(tmp_path)) == primeiro
    assert (tmp_path / aut.ARQUIVO_DO_SEGREDO).read_text(encoding="utf-8") == primeiro


def test_arquivo_de_segredo_vazio_gera_outro(tmp_path):
    (tmp_path / aut.ARQUIVO_DO_SEGREDO).write_text("  \n", encoding="utf-8")
    assert len(aut.carregar_segredo(str(tmp_path))) == 64


def test_token_da_sessao_depende_do_segredo_e_do_nome():
    assert aut.token_da_sessao(SEGREDO, "a") == aut.token_da_sessao(SEGREDO, "a")
    assert aut.token_da_sessao(SEGREDO, "a") != aut.token_da_sessao(SEGREDO, "b")
    assert aut.token_da_sessao(SEGREDO, "a") != aut.token_da_sessao("t" * 64, "a")


def test_credencial_so_vale_com_o_hash_desta_sessao():
    certo = "conta1:" + aut.token_da_sessao(SEGREDO, "conta1")
    assert aut.credencial_valida(SEGREDO, certo)
    assert not aut.credencial_valida(SEGREDO, "conta2:" + aut.token_da_sessao(SEGREDO, "conta1"))
    assert not aut.credencial_valida("t" * 64, certo)


@pytest.mark.parametrize("token", ["", None, "conta1", "conta1:", ":abc", "conta1:abc"])
def test_credencial_incompleta_ou_errada_e_recusada(token):
    assert not aut.credencial_valida(SEGREDO, token)


def test_segredo_valido_recusa_vazio_e_diferente():
    assert aut.segredo_valido(SEGREDO, SEGREDO)
    assert not aut.segredo_valido(SEGREDO, "")
    assert not aut.segredo_valido(SEGREDO, None)
    assert not aut.segredo_valido(SEGREDO, SEGREDO + "x")


@pytest.mark.parametrize("host", ["127.0.0.1:6300", "localhost:6300", "LOCALHOST", "[::1]:6300", "127.0.0.1"])
def test_host_da_propria_maquina_e_aceito(host):
    assert aut.host_permitido(host)


@pytest.mark.parametrize("host", ["", None, "evil.com", "evil.com:6300", "127.0.0.1.evil.com:6300",
                                  "192.168.0.5:6300", "0.0.0.0:6300"])
def test_outro_host_e_recusado_contra_dns_rebinding(host):
    assert not aut.host_permitido(host)


@pytest.mark.parametrize("modelo,partes,esperado", [
    ("/healthz", {}, ("aberta", "")),
    ("/health", {}, ("aberta", "")),
    ("/winzapp/identity", {}, ("aberta", "")),
    ("/metrics", {}, ("aberta", "")),
    ("/socket.io/", {}, ("aberta", "")),
    ("/api/{session}/{key}/generate-token", {"session": "conta1", "key": "KEY"}, ("chave", "KEY")),
    ("/api/{secretkey}/show-all-sessions", {"secretkey": "SEGREDO"}, ("chave", "SEGREDO")),
    ("/api/{secretkey}/start-all", {"secretkey": "SEGREDO"}, ("chave", "SEGREDO")),
    ("/api/{tok}/send-message", {"tok": "conta1:abc"}, ("sessao", "conta1:abc")),
    ("/api/{tok}/group-info/{groupId}", {"tok": "conta1:abc", "groupId": "1@g.us"}, ("sessao", "conta1:abc")),
    ("/api/{tok}/delete-all-chats", {"tok": "conta1:abc"}, ("sessao", "conta1:abc")),
    ("/api/{tok}/send-message", {"tok": "/"}, ("sessao", "/")),
])
def test_o_que_cada_rota_exige(modelo, partes, esperado):
    assert aut.exigencia(modelo, partes) == esperado


@pytest.mark.parametrize("modelo", ["/", "/outra", "/api/{x}/y", "/api", "/static/{p}"])
def test_rota_desconhecida_e_negada_por_padrao(modelo):
    assert aut.exigencia(modelo, {})[0] == "negada"


def test_comparacao_nao_falha_com_caracteres_fora_do_ascii():
    assert not aut.credencial_valida(SEGREDO, "conta1:\u00e9\u00e9\u00e9")
    assert not aut.segredo_valido(SEGREDO, "\u00e9" * 64)
    assert not aut.credencial_valida(SEGREDO, ["conta1", "x"])
    assert not aut.credencial_valida(SEGREDO, {"a": 1})
    assert not aut.segredo_valido(SEGREDO, ["x"])


def test_duas_pontes_criando_o_segredo_juntas_ficam_com_o_mesmo(tmp_path):
    import threading
    pasta = str(tmp_path / "dados")
    resultados = []

    def criar():
        resultados.append(aut.carregar_segredo(pasta))
    threads = [threading.Thread(target=criar) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(resultados) == 12 and len(set(resultados)) == 1
    assert resultados[0] == (tmp_path / "dados" / aut.ARQUIVO_DO_SEGREDO).read_text(encoding="utf-8")


async def _cliente():
    async def ok(req):
        return web.json_response({"ok": True})
    app = web.Application(middlewares=[aut.criar_middleware(SEGREDO)])
    app.router.add_get("/healthz", ok)
    app.router.add_get("/api/{tok}/status-session", ok)
    app.router.add_post("/api/{session}/{key}/generate-token", ok)
    app.router.add_get("/api/{secretkey}/show-all-sessions", ok)
    cliente = TestClient(TestServer(app))
    await cliente.start_server()
    return cliente


@pytest.mark.asyncio
async def test_middleware_deixa_passar_so_quem_tem_credencial():
    cliente = await _cliente()
    try:
        bom = "conta1:" + aut.token_da_sessao(SEGREDO, "conta1")
        assert (await cliente.get("/healthz")).status == 200
        assert (await cliente.get(f"/api/{bom}/status-session")).status == 200
        assert (await cliente.get("/api/conta1:errado/status-session")).status == 401
        assert (await cliente.get("/api/conta1/status-session")).status == 401
        assert (await cliente.post("/api/conta1/chave-errada/generate-token")).status == 401
        assert (await cliente.post(f"/api/conta1/{SEGREDO}/generate-token")).status == 200
        assert (await cliente.get("/api/nada/show-all-sessions")).status == 401
        assert (await cliente.get(f"/api/{SEGREDO}/show-all-sessions")).status == 200
    finally:
        await cliente.close()


@pytest.mark.asyncio
async def test_middleware_recusa_host_de_fora_mesmo_com_credencial():
    cliente = await _cliente()
    try:
        bom = "conta1:" + aut.token_da_sessao(SEGREDO, "conta1")
        resposta = await cliente.get(f"/api/{bom}/status-session", headers={"Host": "evil.com:6300"})
        assert resposta.status == 403
        resposta = await cliente.get("/healthz", headers={"Host": "evil.com"})
        assert resposta.status == 403
    finally:
        await cliente.close()


def test_nome_da_credencial_e_o_prefixo_do_token():
    assert aut.nome_da_credencial("conta1:abc") == "conta1"
    assert aut.nome_da_credencial("sem-separador") == "sem-separador"
    assert aut.nome_da_credencial(None) == ""
