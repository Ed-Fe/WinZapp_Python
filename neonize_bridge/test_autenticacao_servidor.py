"""O servidor de verdade (criar_app) exige o segredo: de ponta a ponta, sem WhatsApp."""
import pytest
from aiohttp.test_utils import TestClient, TestServer

import autenticacao as aut
import server


async def _cliente(pasta):
    cliente = TestClient(TestServer(server.criar_app(str(pasta))))
    await cliente.start_server()
    return cliente


@pytest.mark.asyncio
async def test_sem_segredo_nada_passa_e_com_o_token_gerado_a_rota_responde(tmp_path):
    cliente = await _cliente(tmp_path)
    try:
        segredo = (tmp_path / aut.ARQUIVO_DO_SEGREDO).read_text(encoding="utf-8")
        assert (await cliente.get("/healthz")).status == 200
        # o que a chave padrao do cliente (publica no repositorio) conseguia antes
        assert (await cliente.post("/api/conta1/wz-local-api-key/generate-token")).status == 401
        assert (await cliente.get("/api/conta1:qualquer/status-session")).status == 401
        assert (await cliente.get("/api/qualquer/show-all-sessions")).status == 401

        resposta = await cliente.post(f"/api/conta1/{segredo}/generate-token")
        assert resposta.status == 200
        corpo = await resposta.json()
        assert corpo["full"] == "conta1:" + aut.token_da_sessao(segredo, "conta1")

        assert (await cliente.get(f"/api/{corpo['full']}/status-session")).status == 200
        assert (await cliente.get(f"/api/{segredo}/show-all-sessions")).status == 200
    finally:
        await cliente.close()


@pytest.mark.asyncio
async def test_nome_de_sessao_invalido_nao_gera_token(tmp_path):
    cliente = await _cliente(tmp_path)
    try:
        segredo = (tmp_path / aut.ARQUIVO_DO_SEGREDO).read_text(encoding="utf-8")
        resposta = await cliente.post(f"/api/a b/{segredo}/generate-token")
        assert resposta.status == 400
    finally:
        await cliente.close()


def test_o_app_nasce_com_o_teto_de_corpo_da_ponte(tmp_path):
    app = server.criar_app(str(tmp_path))
    assert app._client_max_size == aut.TAMANHO_MAXIMO_DO_CORPO
    # abaixo dos 2 GiB de antes, mas acima de 100 MiB de midia em base64 (~134 MiB)
    assert 134 * 1024**2 < aut.TAMANHO_MAXIMO_DO_CORPO < 2 * 1024**3


@pytest.mark.asyncio
async def test_multipart_em_blocos_passa_do_teto_e_json_grande_nao():
    """O send-file le o multipart por blocos (`read_chunk`), que o
    client_max_size nao conta; o corpo lido de uma vez (`req.json()`) conta."""
    from aiohttp import FormData, web

    async def enviar(req):
        total = 0
        leitor = await req.multipart()
        while (parte := await leitor.next()) is not None:
            while bloco := await parte.read_chunk(1 << 16):
                total += len(bloco)
        return web.json_response({"bytes": total})

    async def json_(req):
        return web.json_response(await req.json())

    app = web.Application(client_max_size=1024)
    app.router.add_post("/enviar", enviar)
    app.router.add_post("/json", json_)
    cliente = TestClient(TestServer(app))
    await cliente.start_server()
    try:
        form = FormData()
        form.add_field("file", b"x" * 200_000, filename="a.bin",
                       content_type="application/octet-stream")
        resposta = await cliente.post("/enviar", data=form)
        assert resposta.status == 200
        assert (await resposta.json())["bytes"] == 200_000
        assert (await cliente.post("/json", json={"x": "y" * 5000})).status == 413
    finally:
        await cliente.close()
