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
