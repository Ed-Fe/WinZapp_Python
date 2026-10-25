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
