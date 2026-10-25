"""Rotas de servidor e sessao: as funcoes puras e a ligacao das rotas, com
sessoes de mentira (nada de neonize)."""
import asyncio
import base64
import os
from types import SimpleNamespace as N

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import administracao

PNG = b"\x89PNG\r\n\x1a\nfake"


def data_url(texto):
    return "data:image/png;base64," + base64.b64encode(PNG).decode()


def test_identidade_usa_o_ambiente_e_o_protocolo_1():
    corpo = administracao.identidade(
        {"WINZAPP_INSTALLATION_ID": "inst", "WINZAPP_INSTANCE_ID": "node"}, 42, 41)
    assert corpo == {"installation_id": "inst", "instance_id": "node",
                     "protocol_version": 1, "pid": 42, "ppid": 41}


def test_identidade_sem_ambiente_nao_e_de_nenhuma_instalacao():
    corpo = administracao.identidade({}, 7)
    assert corpo["installation_id"] == "" and corpo["protocol_version"] == 1


def test_openapi_lista_caminhos_e_metodos_sem_head_nem_socketio():
    doc = administracao.documento_openapi([
        ("GET", "/api/{tok}/contact/{jid}"), ("HEAD", "/api/{tok}/contact/{jid}"),
        ("POST", "/api/{tok}/send-message"), ("*", "/socket.io/"), ("GET", "/healthz")])
    assert doc["openapi"].startswith("3.")
    assert list(doc["paths"]) == ["/api/{tok}/contact/{jid}", "/api/{tok}/send-message",
                                  "/healthz"]
    contato = doc["paths"]["/api/{tok}/contact/{jid}"]
    assert list(contato) == ["get"]
    assert [p["name"] for p in contato["get"]["parameters"]] == ["tok", "jid"]
    assert "parameters" not in doc["paths"]["/healthz"]["get"]


def test_metricas_contam_as_sessoes_por_estado():
    texto = administracao.texto_metricas(["CONNECTED", "CONNECTED", "QRCODE"], 12.9)
    assert 'winzapp_bridge_sessions{status="CONNECTED"} 2' in texto
    assert 'winzapp_bridge_sessions{status="QRCODE"} 1' in texto
    assert 'winzapp_bridge_sessions{status="CLOSED"} 0' in texto
    assert "winzapp_bridge_uptime_seconds 12\n" in texto
    assert texto.endswith("\n")


def test_sessoes_em_disco_so_as_pareadas(tmp_path):
    (tmp_path / "b").mkdir()
    (tmp_path / "b" / "sessao.db").write_bytes(b"")
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "sessao.db").write_bytes(b"")
    (tmp_path / "vazia").mkdir()
    (tmp_path / "ativas.json").write_text("[]")
    assert administracao.sessoes_em_disco(str(tmp_path)) == ["a", "b"]
    assert administracao.sessoes_em_disco(str(tmp_path / "nao-existe")) == []


def test_qrcode_com_codigo_devolve_o_png():
    assert administracao.resposta_qrcode(True, "QRCODE", "2@abc", data_url) == ("png", PNG)


def test_qrcode_sem_codigo_explica_o_porque():
    tipo, corpo = administracao.resposta_qrcode(False, "CLOSED", None, data_url)
    assert tipo == "json" and corpo["status"] is None and "start-session" in corpo["message"]
    assert administracao.resposta_qrcode(True, "CONNECTED", None, data_url) == (
        "json", {"status": "CONNECTED", "message": "QRCode is not available..."})


def test_reconexao_responde_200_com_a_sessao_viva_mesmo_reconectando():
    """O whatsmeow reconecta sozinho: 400 durante a queda fazia o cliente, apos
    tres respostas, fechar e reiniciar uma sessao que ia se recuperar."""
    ok = (200, {"status": "success", "response": {"ok": True}})
    for status in ("CONNECTED", "INITIALIZING", "QRCODE"):
        assert administracao.resposta_reconexao(status, True) == ok
    for status, viva in (("CONNECTED", False), ("CLOSED", True), ("CLOSED", False),
                         ("notLogged", True)):
        http, corpo = administracao.resposta_reconexao(status, viva)
        assert http == 400 and corpo["status"] == "error"


class ThreadFalsa:
    def __init__(self, viva=True):
        self.viva = viva

    def is_alive(self):
        return self.viva


class SessaoFalsa:
    def __init__(self, nome, status="CLOSED", urlcode=None, conectado=True, viva=None):
        self.nome, self.status, self.urlcode = nome, status, urlcode
        self.thread = None if viva is None else ThreadFalsa(viva)
        self.cliente = N(is_connected=conectado)
        self.iniciada = 0

    def iniciar(self):
        self.iniciada += 1
        self.thread = ThreadFalsa()


def montar(tmp_path, fixa=""):
    estado = {"sessoes": {}, "fixa": fixa}
    ativas = {}

    def sessao(req):
        nome = estado["fixa"] or req.match_info["tok"].split(":", 1)[0]
        return estado["sessoes"].setdefault(nome, SessaoFalsa(nome))

    app = web.Application()
    administracao.registrar(
        app.router, sessao=sessao, ok=lambda **kw: web.json_response(kw), estado=estado,
        pasta=str(tmp_path), marcar_ativa=lambda n, a: ativas.__setitem__(n, a),
        qr_data_url=data_url)
    return app, estado, ativas


def com_cliente(app, roteiro):
    async def rodar():
        async with TestClient(TestServer(app)) as cliente:
            return await roteiro(cliente)
    return asyncio.run(rodar())


def test_rotas_de_identidade_docs_e_metricas(tmp_path):
    app, estado, _ = montar(tmp_path)
    estado["sessoes"]["x"] = SessaoFalsa("x", "CONNECTED")

    async def roteiro(c):
        identidade = await (await c.get("/winzapp/identity")).json()
        docs = await (await c.get("/api-docs")).json()
        metricas = await (await c.get("/metrics")).text()
        return identidade, docs, metricas

    identidade, docs, metricas = com_cliente(app, roteiro)
    assert identidade["protocol_version"] == 1
    # o cliente confere estes dois contra `ponte.pid` antes de mandar o segredo
    assert identidade["pid"] == os.getpid() and identidade["ppid"] == os.getppid()
    assert "segredo" not in identidade and "secret" not in identidade
    assert "/winzapp/identity" in docs["paths"]
    assert "/api/{tok}/qrcode-session" in docs["paths"]
    assert 'winzapp_bridge_sessions{status="CONNECTED"} 1' in metricas


def test_show_all_sessions_lista_os_nomes_em_memoria(tmp_path):
    app, estado, _ = montar(tmp_path)
    estado["sessoes"].update({"b": SessaoFalsa("b"), "a": SessaoFalsa("a")})

    async def roteiro(c):
        return await (await c.get("/api/segredo/show-all-sessions")).json()

    assert com_cliente(app, roteiro) == {"response": ["a", "b"]}


def test_start_all_inicia_as_contas_do_disco_e_as_marca_ativas(tmp_path):
    for nome in ("a", "b"):
        (tmp_path / nome).mkdir()
        (tmp_path / nome / "sessao.db").write_bytes(b"")
    app, estado, ativas = montar(tmp_path)

    async def roteiro(c):
        resposta = await c.post("/api/segredo/start-all")
        return resposta.status, await resposta.json()

    status, corpo = com_cliente(app, roteiro)
    assert status == 201 and corpo == {"status": "success", "message": "Starting all sessions"}
    assert {n: s.iniciada for n, s in estado["sessoes"].items()} == {"a": 1, "b": 1}
    assert ativas == {"a": True, "b": True}


def test_start_all_no_modo_de_conta_unica_inicia_so_ela(tmp_path):
    app, estado, ativas = montar(tmp_path, fixa="teste")

    async def roteiro(c):
        return (await c.post("/api/segredo/start-all")).status

    assert com_cliente(app, roteiro) == 201
    assert list(estado["sessoes"]) == ["teste"] and ativas == {"teste": True}


def test_qrcode_session_png_e_json(tmp_path):
    app, estado, _ = montar(tmp_path)
    estado["sessoes"]["com"] = SessaoFalsa("com", "QRCODE", urlcode="2@abc")
    estado["sessoes"]["com"].thread = ThreadFalsa()

    async def roteiro(c):
        png = await c.get("/api/com:tok/qrcode-session")
        corpo_png = await png.read()
        sem = await c.get("/api/nova:tok/qrcode-session")
        return png.content_type, corpo_png, await sem.json()

    tipo, corpo, sem = com_cliente(app, roteiro)
    assert tipo == "image/png" and corpo == PNG
    assert sem["status"] is None


def test_reconnect_socket_stream_olha_a_thread_nao_o_socket(tmp_path):
    app, estado, _ = montar(tmp_path)
    estado["sessoes"]["viva"] = SessaoFalsa("viva", "CONNECTED", conectado=True, viva=True)
    # socket momentaneamente caido, sessao de pe: o whatsmeow reconecta, nao e erro
    estado["sessoes"]["caida"] = SessaoFalsa("caida", "CONNECTED", conectado=False, viva=True)

    async def roteiro(c):
        viva = await c.post("/api/viva:tok/reconnect-socket-stream")
        caida = await c.post("/api/caida:tok/reconnect-socket-stream")
        parada = await c.post("/api/parada:tok/reconnect-socket-stream")
        return viva.status, caida.status, parada.status

    assert com_cliente(app, roteiro) == (200, 200, 400)
