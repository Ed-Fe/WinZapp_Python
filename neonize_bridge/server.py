"""Servidor-ponte: fala a API do WPPConnect Server (HTTP + Socket.IO) e, por
tras, usa o neonize. Fase 0: login (QR e codigo), estado, host-device, enviar
texto e receber texto. O cliente do WinZapp aponta para ca pela opcao
"servidor personalizado" (wpp_custom_api).

Uso:  .venv\\Scripts\\python server.py [--porta 6300] [--pasta dados]
"""
import argparse
import asyncio
import base64
import hashlib
import io
import logging
import os
import threading
import time

import segno
import socketio
from aiohttp import web

log = logging.getLogger("ponte")


def qr_data_url(texto: str) -> str:
    buf = io.BytesIO()
    segno.make(texto, error="m").save(buf, kind="png", scale=6, border=2)
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def jid_texto(jid) -> str:
    """JID do neonize -> 'usuario@servidor' (vazio se nao ha)."""
    if jid is None or not getattr(jid, "User", ""):
        return ""
    return f"{jid.User}@{jid.Server}"


class Sessao:
    """Uma conta: o cliente neonize, o estado no vocabulario do WPPConnect e
    a ponte entre a thread do neonize e o laco do aiohttp."""

    def __init__(self, nome, pasta, sio, laco):
        self.nome = nome
        self.pasta = pasta
        self.sio = sio
        self.laco = laco
        self.cliente = None
        self.thread = None
        self.status = "CLOSED"      # CLOSED INITIALIZING QRCODE CONNECTED
        self.urlcode = None
        self.eu = ""                # JID proprio (usuario@s.whatsapp.net)
        self.lid = ""

    # ------------------------------------------------------------ eventos
    def emitir(self, evento, dados):
        asyncio.run_coroutine_threadsafe(self.sio.emit(evento, dados), self.laco)

    def _estado(self, status):
        self.status = status
        self.emitir("status-find", {"session": self.nome, "status": {
            "CONNECTED": "inChat", "QRCODE": "notLogged",
            "CLOSED": "browserClose", "INITIALIZING": "initBrowser",
        }.get(status, status)})

    # --------------------------------------------------------- ciclo de vida
    def iniciar(self, telefone=None):
        if self.thread and self.thread.is_alive():
            return
        self.status = "INITIALIZING"
        self.thread = threading.Thread(
            target=self._rodar, args=(telefone,), daemon=True, name="neonize")
        self.thread.start()

    def _rodar(self, telefone):
        import neonize.events as nev
        from neonize.client import NewClient
        os.makedirs(self.pasta, exist_ok=True)
        cli = self.cliente = NewClient(os.path.join(self.pasta, "sessao.db"))

        @cli.event(nev.ConnectedEv)
        def _conectou(c, e):
            try:
                me = c.get_me()
                self.eu = jid_texto(me.JID)
                self.lid = jid_texto(me.LID)
            except Exception:
                log.exception("get_me")
            self.urlcode = None
            self.status = "CONNECTED"
            log.info("conectado como %s", self.eu)
            self.emitir("session-logged", {"session": self.nome, "status": True})

        @cli.event(nev.LoggedOutEv)
        def _saiu(c, e):
            self.status = "CLOSED"
            self.emitir("status-find", {"session": self.nome,
                                        "status": "disconnectedMobile"})
            self.emitir("session-logged", {"session": self.nome, "status": False})

        @cli.event(nev.DisconnectedEv)
        def _caiu(c, e):
            log.info("desconectado")

        @cli.event(nev.MessageEv)
        def _mensagem(c, e):
            try:
                self.emitir("received-message", {
                    "session": self.nome, "response": mensagem_para_wpp(e)})
            except Exception:
                log.exception("mensagem recebida")

        @cli.event.qr
        def _qr(c, dados):
            codigo = dados.decode() if isinstance(dados, bytes) else str(dados)
            self.urlcode = codigo
            self.status = "QRCODE"
            self.emitir("qrCode", {"session": self.nome, "urlcode": codigo,
                                   "data": qr_data_url(codigo)})

        if telefone:
            def _codigo():
                from neonize.utils.enum import ClientName, ClientType
                time.sleep(2)
                for tentativa in range(5):
                    try:
                        cod = cli.PairPhone("".join(ch for ch in telefone if ch.isdigit()),
                                            True, ClientName.WINDOWS, ClientType.EDGE)
                        self.emitir("phoneCode", {"session": self.nome, "data": cod})
                        return
                    except Exception as erro:
                        log.warning("codigo de pareamento, tentativa %d: %r",
                                    tentativa + 1, erro)
                        time.sleep(3)
                self.emitir("phoneCodeError", {"session": self.nome,
                                               "error": "falha ao gerar o codigo"})
            threading.Thread(target=_codigo, daemon=True).start()

        try:
            cli.connect()      # bloqueia ate desconectar
        except Exception:
            log.exception("connect")
        finally:
            if self.status != "CLOSED":
                self.status = "CLOSED"

    def parar(self):
        c = self.cliente
        if c is not None:
            try:
                c.disconnect()
            except Exception:
                log.exception("disconnect")
        self.status = "CLOSED"

    def sair(self):
        c = self.cliente
        if c is not None:
            try:
                c.logout()
            except Exception:
                log.exception("logout")
        self.parar()


def mensagem_para_wpp(e):
    """MessageEv -> o formato 'estilo Baileys' que o WinZapp ja le no evento
    received-message (key / message / messageType / messageTimestamp)."""
    from google.protobuf.json_format import MessageToDict
    info = e.Info
    origem = info.MessageSource
    corpo = MessageToDict(e.Message, preserving_proto_field_name=False)
    tipo = next((k for k in corpo if k != "messageContextInfo"), "conversation")
    return {
        "key": {
            "remoteJid": jid_texto(origem.Chat),
            "fromMe": bool(origem.IsFromMe),
            "id": info.ID,
            **({"participant": jid_texto(origem.Sender)} if origem.IsGroup else {}),
        },
        "message": corpo,
        "messageType": tipo,
        "messageTimestamp": int(info.Timestamp // 1000 if info.Timestamp > 10**11
                                else info.Timestamp),
        "pushName": info.Pushname,
    }


# ---------------------------------------------------------------- HTTP
def criar_app(pasta):
    sio = socketio.AsyncServer(async_mode="aiohttp", cors_allowed_origins="*",
                               logger=False, engineio_logger=False)
    app = web.Application(client_max_size=2 * 1024**3)
    sio.attach(app)
    estado = {"sessoes": {}}

    def sessao(req):
        tok = req.match_info["tok"]
        nome = tok.split(":", 1)[0]
        s = estado["sessoes"].get(nome)
        if s is None:
            s = estado["sessoes"][nome] = Sessao(
                nome, os.path.join(pasta, nome), sio, asyncio.get_running_loop())
        return s

    def ok(**kw):
        return web.json_response(kw)

    @sio.event
    async def connect(sid, environ, auth):
        cred = (auth or {}).get("token") or environ.get("HTTP_APIKEY", "")
        if not cred:
            raise socketio.exceptions.ConnectionRefusedError("sem credencial")
        log.info("socket conectado (%s)", (auth or {}).get("session", "?"))

    async def gerar_token(req):
        nome, chave = req.match_info["session"], req.match_info["key"]
        tok = hashlib.sha256((nome + chave).encode()).hexdigest()
        return web.json_response({"status": "success", "session": nome,
                                  "token": tok, "full": f"{nome}:{tok}"})

    async def start_session(req):
        s = sessao(req)
        try:
            corpo = await req.json()
        except Exception:
            corpo = {}
        s.iniciar(corpo.get("phone"))
        return ok(status=s.status, qrcode=None, urlcode=s.urlcode)

    async def status_session(req):
        s = sessao(req)
        return ok(status=s.status, qrcode=qr_data_url(s.urlcode) if s.urlcode else None,
                  urlcode=s.urlcode, session=s.nome)

    async def check_connection(req):
        s = sessao(req)
        conectado = s.status == "CONNECTED"
        return ok(status=conectado, message="Connected" if conectado else "Disconnected")

    async def close_session(req):
        sessao(req).parar()
        return ok(status=True, message="Session successfully closed")

    async def logout_session(req):
        sessao(req).sair()
        return ok(status=True, message="Session successfully closed")

    async def host_device(req):
        s = sessao(req)
        return ok(status="success", response={
            "phoneNumber": {"_serialized": s.eu}, "wid": {"_serialized": s.eu},
            "lid": {"_serialized": s.lid}})

    async def vazio_ok(req):
        return ok(status="success", response={})

    async def lista_vazia(req):
        return web.json_response([])

    async def send_message(req):
        s = sessao(req)
        corpo = await req.json()
        if s.status != "CONNECTED" or s.cliente is None:
            return web.json_response({"status": "error", "message": "Disconnected"}, status=400)
        from neonize.utils import build_jid
        fone = corpo["phone"]
        destino = build_jid(fone.split("@")[0], fone.split("@")[1]) if "@" in fone \
            else build_jid(fone)
        resp = await asyncio.get_running_loop().run_in_executor(
            None, lambda: s.cliente.send_message(destino, corpo["message"]))
        return ok(status="success", response=[{"id": resp.ID, "ack": 1}])

    async def saude(req):
        return ok(status="ok")

    r = app.router
    r.add_post("/api/{session}/{key}/generate-token", gerar_token)
    r.add_post("/api/{tok}/start-session", start_session)
    r.add_get("/api/{tok}/status-session", status_session)
    r.add_get("/api/{tok}/check-connection-session", check_connection)
    r.add_post("/api/{tok}/close-session", close_session)
    r.add_post("/api/{tok}/logout-session", logout_session)
    r.add_get("/api/{tok}/host-device", host_device)
    r.add_post("/api/{tok}/set-limit", vazio_ok)
    r.add_post("/api/{tok}/list-chats", lista_vazia)
    r.add_get("/api/{tok}/all-contacts", lista_vazia)
    r.add_post("/api/{tok}/send-message", send_message)
    r.add_get("/healthz", saude)
    r.add_get("/health", saude)
    return app


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--porta", type=int, default=6300)
    ap.add_argument("--pasta", default=os.path.join(os.path.dirname(__file__), "dados"))
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    web.run_app(criar_app(a.pasta), host="127.0.0.1", port=a.porta, print=None)


if __name__ == "__main__":
    main()
