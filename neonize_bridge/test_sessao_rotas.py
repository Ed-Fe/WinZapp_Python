"""As rotas de sessao do servidor de verdade (criar_app), com a Sessao real e o
cliente neonize de mentira: start-session, status-session, check-connection-
session, close-session, host-device, statuses, status/{id}/delete e /healthz.
Nao testado contra uma conta real."""
import threading
import time

import pytest
from aiohttp.test_utils import TestClient, TestServer

import autenticacao as aut
import server


class ClienteFalso:
    def __init__(self, conectado=True):
        self.is_connected = conectado
        self.chamadas = []

    def disconnect(self):
        self.chamadas.append("disconnect")

    def stop(self):
        self.chamadas.append("stop")

    def logout(self):
        self.chamadas.append("logout")


@pytest.fixture
def sessoes(monkeypatch):
    criadas = {}

    class Registrada(server.Sessao):
        def __init__(self, nome, *args, **kw):
            super().__init__(nome, *args, **kw)
            criadas[nome] = self

    monkeypatch.setattr(server, "Sessao", Registrada)
    return criadas


async def _ponte(pasta):
    cliente = TestClient(TestServer(server.criar_app(str(pasta))))
    await cliente.start_server()
    segredo = (pasta / aut.ARQUIVO_DO_SEGREDO).read_text(encoding="utf-8")
    return cliente, f"conta1:{aut.token_da_sessao(segredo, 'conta1')}", segredo


@pytest.mark.asyncio
async def test_healthz_responde_como_o_node(tmp_path, sessoes):
    cliente, _, _ = await _ponte(tmp_path)
    try:
        resposta = await cliente.get("/healthz")
        # wait_for_api (client/core/wpp_update_health.py) exige {"message": "OK"}
        assert resposta.status == 200 and await resposta.json() == {"message": "OK"}
    finally:
        await cliente.close()


@pytest.mark.asyncio
async def test_start_session_roda_o_iniciar_fora_do_laco(tmp_path, sessoes, monkeypatch):
    """iniciar() espera a thread antiga terminar (ate 10 s): no laco do aiohttp
    isso congelava todas as rotas e o Socket.IO."""
    onde = {}

    def iniciar_falso(self, telefone=None):
        onde["thread"] = threading.get_ident()
        return True

    monkeypatch.setattr(server.Sessao, "iniciar", iniciar_falso)
    cliente, token, _ = await _ponte(tmp_path)
    try:
        resposta = await cliente.post(f"/api/{token}/start-session", json={})
        assert resposta.status == 200
        assert onde["thread"] != threading.get_ident()
        # o start-all (administracao) tambem
        onde.clear()
        resposta = await cliente.post(f"/api/{aut.carregar_segredo(str(tmp_path))}/start-all")
        assert resposta.status == 201
    finally:
        await cliente.close()


@pytest.mark.asyncio
async def test_start_session_depois_de_close_reconecta(tmp_path, sessoes, monkeypatch):
    """Ponta a ponta do defeito 1: close-session e start-session na mesma sessao."""
    clientes = []

    def rodar_falso(self, telefone, geracao):
        cli = self.cliente = ClienteFalso()
        clientes.append(cli)
        try:
            while "stop" not in cli.chamadas:
                time.sleep(0.01)
        finally:
            self._aplicar(geracao, "fim_da_thread")

    monkeypatch.setattr(server.Sessao, "_rodar", rodar_falso)
    cliente, token, _ = await _ponte(tmp_path)
    try:
        assert (await cliente.post(f"/api/{token}/start-session", json={})).status == 200
        assert (await cliente.post(f"/api/{token}/close-session")).status == 200
        resposta = await cliente.post(f"/api/{token}/start-session", json={})
        assert (await resposta.json())["status"] == "INITIALIZING"
        assert len(clientes) == 2
        await cliente.post(f"/api/{token}/close-session")
    finally:
        await cliente.close()


@pytest.mark.asyncio
async def test_check_connection_olha_o_socket_e_nao_so_o_status_em_cache(tmp_path, sessoes):
    cliente, token, _ = await _ponte(tmp_path)
    try:
        await cliente.get(f"/api/{token}/status-session")       # cria a sessao
        s = sessoes["conta1"]
        s.cliente = ClienteFalso(conectado=True)
        s.status = "CONNECTED"
        assert (await (await cliente.get(f"/api/{token}/check-connection-session")).json())[
            "status"] is True
        s.cliente.is_connected = False                          # queda de rede
        corpo = await (await cliente.get(f"/api/{token}/check-connection-session")).json()
        assert corpo["status"] is False and corpo["message"] == "Disconnected"
        s.status = "CLOSED"
        s.cliente.is_connected = True
        assert (await (await cliente.get(f"/api/{token}/check-connection-session")).json())[
            "status"] is False
    finally:
        await cliente.close()


@pytest.mark.asyncio
async def test_host_device_por_estado(tmp_path, sessoes):
    cliente, token, _ = await _ponte(tmp_path)
    try:
        # ponte recem-iniciada: antes era 200 com numero vazio, lido como "desvinculada"
        resposta = await cliente.get(f"/api/{token}/host-device")
        assert resposta.status == 503
        s = sessoes["conta1"]
        s.eu, s.lid = "5511999990000@s.whatsapp.net", "123@lid"
        s.status = "CONNECTED"
        resposta = await cliente.get(f"/api/{token}/host-device")
        corpo = await resposta.json()
        assert resposta.status == 200
        assert corpo["response"]["phoneNumber"]["_serialized"] == "5511999990000@s.whatsapp.net"
        s.status = "INITIALIZING"                               # reconectando: nao sabe
        assert (await cliente.get(f"/api/{token}/host-device")).status == 503
        s.status = "notLogged"                                  # desvinculada de verdade
        resposta = await cliente.get(f"/api/{token}/host-device")
        assert resposta.status == 200
        assert (await resposta.json())["response"] == {}
    finally:
        await cliente.close()


@pytest.mark.asyncio
async def test_logout_do_celular_fica_visivel_na_consulta_de_estado(tmp_path, sessoes):
    cliente, token, _ = await _ponte(tmp_path)
    try:
        await cliente.get(f"/api/{token}/status-session")
        s = sessoes["conta1"]
        s.emitir = lambda *a, **kw: None
        s.eu = "5511999990000@s.whatsapp.net"
        g = s.ciclo.abrir()
        s.ciclo.aplicar(g, "conectou")
        s._ao_ser_desvinculada(g, ClienteFalso())
        corpo = await (await cliente.get(f"/api/{token}/status-session")).json()
        assert corpo["status"] == "notLogged"
        host = await cliente.get(f"/api/{token}/host-device")
        assert (await host.json())["response"] == {}            # nao devolve o numero antigo
        await cliente.post(f"/api/{token}/close-session")       # o que o cliente faz depois
        corpo = await (await cliente.get(f"/api/{token}/status-session")).json()
        assert corpo["status"] == "notLogged"
    finally:
        await cliente.close()


def _status(id_, de_mim=True, t=None):
    return {"id": {"id": id_, "fromMe": de_mim, "remote": "status@broadcast"},
            "t": t or int(time.time()), "fromMe": de_mim, "to": "status@broadcast",
            "type": "chat", "body": "x", "author": "5511@c.us"}


@pytest.mark.asyncio
async def test_statuses_banco_vazio_nao_e_a_palavra_final_do_whatsapp(tmp_path, sessoes):
    """myStatusReady:true com o banco vazio fazia o cliente apagar as historias
    proprias que tinha em cache."""
    cliente, token, _ = await _ponte(tmp_path)
    try:
        corpo = await (await cliente.get(f"/api/{token}/statuses")).json()
        assert corpo["response"]["myStatus"] == []
        assert corpo["response"]["myStatusReady"] is False
        sessoes["conta1"].bd.mensagem("status@broadcast", _status("ST1"))
        corpo = await (await cliente.get(f"/api/{token}/statuses")).json()
        assert [m["id"]["id"] for m in corpo["response"]["myStatus"]] == ["ST1"]
        assert corpo["response"]["myStatusReady"] is True
    finally:
        await cliente.close()


@pytest.mark.asyncio
async def test_apagar_status_que_nao_existe_ou_nao_e_meu_responde_not_found(tmp_path, sessoes):
    cliente, token, _ = await _ponte(tmp_path)
    try:
        await cliente.get(f"/api/{token}/status-session")
        s = sessoes["conta1"]
        s.bd.mensagem("status@broadcast", _status("DOUTRO", de_mim=False))
        s.bd.mensagem("5511@s.whatsapp.net", {**_status("CONVERSA"), "to": "5511@c.us"})
        for id_ in ("NAOEXISTE", "DOUTRO", "CONVERSA"):
            resposta = await cliente.post(f"/api/{token}/status/{id_}/delete")
            assert resposta.status == 404, id_
            corpo = await resposta.json()
            assert corpo["code"] == "not_found" and corpo["status"] == "error"
        # um status meu existente segue o caminho de antes (aqui, sem conexao)
        s.bd.mensagem("status@broadcast", _status("MEU"))
        resposta = await cliente.post(f"/api/{token}/status/MEU/delete")
        assert resposta.status == 404                 # "Disconnected", como o WPPConnect
        assert (await resposta.json()).get("code") != "not_found"
    finally:
        await cliente.close()


@pytest.mark.asyncio
async def test_rotas_de_sessao_aceitam_bearer_com_so_o_nome_da_sessao(tmp_path, sessoes):
    cliente, _, segredo = await _ponte(tmp_path)
    try:
        resposta = await cliente.get("/api/conta1/check-connection-session")
        assert resposta.status == 401
        resposta = await cliente.get("/api/conta1/check-connection-session",
                                     headers={"Authorization": f"Bearer {segredo}"})
        assert resposta.status == 200
        resposta = await cliente.post("/api/conta1/close-session",
                                      headers={"Authorization": f"Bearer {segredo}"})
        assert resposta.status == 200
    finally:
        await cliente.close()
