"""Cada socket so recebe os eventos da propria sessao (sala = nome do token),
com um socketio.AsyncServer e um AsyncClient de verdade, sem WhatsApp."""
import asyncio

import pytest
import socketio
from aiohttp.test_utils import TestClient, TestServer

import autenticacao as aut
import server


@pytest.fixture
def sessoes(monkeypatch):
    """As Sessao que o servidor criar (o __init__ delas nao usa o neonize)."""
    criadas = []

    class Registrada(server.Sessao):
        def __init__(self, *args, **kw):
            super().__init__(*args, **kw)
            criadas.append(self)
    monkeypatch.setattr(server, "Sessao", Registrada)
    return criadas


async def _ponte(pasta, fixa=""):
    cliente = TestClient(TestServer(server.criar_app(str(pasta), fixa)))
    await cliente.start_server()
    segredo = (pasta / aut.ARQUIVO_DO_SEGREDO).read_text(encoding="utf-8")
    return cliente, segredo


async def _socket(cliente, token, sessao_dita=None):
    """Um cliente Socket.IO como o do WinZapp: auth com token e session."""
    recebidos = []
    sio = socketio.AsyncClient()
    sio.on("status-find", lambda dados: recebidos.append(dados))
    await sio.connect(str(cliente.make_url("/")), socketio_path="socket.io",
                      auth={"token": token, "session": sessao_dita or token.split(":")[0]},
                      headers={"apikey": token})
    return sio, recebidos


async def _espera(condicao, tempo=5):
    for _ in range(int(tempo / 0.02)):
        if condicao():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("o evento nao chegou")


def _token(segredo, nome):
    return f"{nome}:{aut.token_da_sessao(segredo, nome)}"


@pytest.mark.asyncio
async def test_evento_da_sessao_so_chega_ao_socket_dela(tmp_path, sessoes):
    cliente, segredo = await _ponte(tmp_path)
    sio_a = sio_b = None
    try:
        tok_a, tok_b = _token(segredo, "a"), _token(segredo, "b")
        # b diz no auth ser a sessao "a": a sala vem do token, nao do que o cliente escreve
        sio_a, recebidos_a = await _socket(cliente, tok_a)
        sio_b, recebidos_b = await _socket(cliente, tok_b, sessao_dita="a")
        for tok in (tok_a, tok_b):
            assert (await cliente.get(f"/api/{tok}/status-session")).status == 200
        por_nome = {s.nome: s for s in sessoes}

        por_nome["a"].emitir("status-find", {"session": "a", "status": "inChat"})
        por_nome["b"].emitir("status-find", {"session": "b", "status": "notLogged"})
        await _espera(lambda: recebidos_a and recebidos_b)
        await asyncio.sleep(0.1)    # tempo para um envio indevido aparecer

        assert [d["session"] for d in recebidos_a] == ["a"]
        assert [d["session"] for d in recebidos_b] == ["b"]
        assert recebidos_a[0] == {"session": "a", "status": "inChat"}   # payload intacto
    finally:
        for sio in (sio_a, sio_b):
            if sio is not None:
                await sio.disconnect()
        await cliente.close()


@pytest.mark.asyncio
async def test_sessao_unica_o_evento_chega_ao_socket_do_nome_do_token(tmp_path, sessoes):
    """Em --sessao-unica a Sessao nasce com o nome fixo e passa a levar o do token."""
    cliente, segredo = await _ponte(tmp_path, fixa="fixa")
    sio = None
    try:
        tok = _token(segredo, "conta1")
        sio, recebidos = await _socket(cliente, tok)
        assert (await cliente.get(f"/api/{tok}/status-session")).status == 200
        assert [s.nome for s in sessoes] == ["conta1"]

        sessoes[0].emitir("status-find", {"session": sessoes[0].nome, "status": "inChat"})
        await _espera(lambda: recebidos)
        assert recebidos == [{"session": "conta1", "status": "inChat"}]
    finally:
        if sio is not None:
            await sio.disconnect()
        await cliente.close()


@pytest.mark.asyncio
async def test_socket_sem_credencial_valida_e_recusado(tmp_path, sessoes):
    cliente, _ = await _ponte(tmp_path)
    try:
        sio = socketio.AsyncClient()
        with pytest.raises(socketio.exceptions.ConnectionError):
            await sio.connect(str(cliente.make_url("/")), socketio_path="socket.io",
                              auth={"token": "a:errado", "session": "a"})
    finally:
        await cliente.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("auth", [
    ["token"], "token", 7, {"token": ["a:b"]}, {"token": 7}, {"token": "é:éé"},
    {"token": ""}, {}, None])
async def test_credencial_malformada_recusa_a_conexao_sem_derrubar_nada(tmp_path, sessoes, auth):
    """Achado M2: uma excecao no connect deixava o socket conectado sem credencial."""
    cliente, segredo = await _ponte(tmp_path)
    sio = socketio.AsyncClient()
    try:
        with pytest.raises(socketio.exceptions.ConnectionError):
            await sio.connect(str(cliente.make_url("/")), socketio_path="socket.io",
                              auth=auth, headers={"apikey": "éé"})
        assert not sio.connected
        await asyncio.sleep(0.1)
        servidor = cliente.server.app["sio"]
        assert list(servidor.manager.get_participants("/", None)) == []
    finally:
        await sio.disconnect()
        await cliente.close()


def test_o_servidor_nao_liga_o_log_de_acesso():
    """Achado H1: o log de acesso grava cada URL, e nela vao o segredo e os telefones."""
    assert server.OPCOES_DO_SERVIDOR["access_log"] is None
    assert server.OPCOES_DO_SERVIDOR["host"] == "127.0.0.1"


@pytest.mark.asyncio
async def test_nenhum_log_leva_o_segredo_nem_o_telefone_de_uma_requisicao(tmp_path, caplog):
    import logging
    import aiohttp
    from aiohttp import web
    app = server.criar_app(str(tmp_path))
    runner = web.AppRunner(app, access_log=server.OPCOES_DO_SERVIDOR["access_log"])
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    porta = site._server.sockets[0].getsockname()[1]
    segredo = (tmp_path / aut.ARQUIVO_DO_SEGREDO).read_text(encoding="utf-8")
    try:
        with caplog.at_level(logging.DEBUG):
            async with aiohttp.ClientSession() as http:
                base = f"http://127.0.0.1:{porta}"
                await http.post(f"{base}/api/conta1/{segredo}/generate-token")
                token = f"conta1:{aut.token_da_sessao(segredo, 'conta1')}"
                await http.get(f"{base}/api/{token}/get-messages/5511999998888@c.us?count=2")
    finally:
        await runner.cleanup()
    texto = caplog.text
    assert segredo not in texto and aut.token_da_sessao(segredo, "conta1") not in texto
    assert "5511999998888" not in texto


@pytest.mark.asyncio
@pytest.mark.parametrize("pacote", [
    '40["x"]', '40"x"', '407', '40{"token":["a"]}', '40{"token":7}', '40{"token":"é:éé"}'])
async def test_conectar_cru_com_credencial_malformada_nao_deixa_socket_registrado(
        tmp_path, sessoes, pacote):
    """O achado M2 como a revisao o reproduziu: handshake de polling e um pacote
    CONNECT malformado. O socket nao pode ficar conectado ao servidor."""
    import json
    cliente, segredo = await _ponte(tmp_path)
    try:
        resposta = await cliente.get("/socket.io/?EIO=4&transport=polling")
        sid = json.loads((await resposta.text())[1:])["sid"]
        await cliente.post(f"/socket.io/?EIO=4&transport=polling&sid={sid}",
                           data=pacote.encode("utf-8"))
        await asyncio.sleep(0.3)
        servidor = cliente.server.app["sio"]
        assert list(servidor.manager.get_participants("/", None)) == []
    finally:
        await cliente.close()
