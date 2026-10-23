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

import msgmap
from store import Armazem

log = logging.getLogger("ponte")


def qr_data_url(texto: str) -> str:
    buf = io.BytesIO()
    segno.make(texto, error="m").save(buf, kind="png", scale=6, border=2)
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


HISTORICO_DIAS = 180     # quanto historico pedir ao celular no pareamento (0 = padrao)


def props_do_aparelho(dias):
    """DeviceProps: o nome que aparece no celular e quanto historico pedir."""
    from neonize.proto.waCompanionReg.WAWebProtobufsCompanionReg_pb2 import DeviceProps
    props = DeviceProps(os="WinZapp", platformType=DeviceProps.DESKTOP)
    if dias:
        props.requireFullSync = True
        props.historySyncConfig.fullSyncDaysLimit = dias
        props.historySyncConfig.fullSyncSizeMbLimit = 102400
        props.historySyncConfig.storageQuotaMb = 102400
    return props


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
        self.simular_codigo = False
        self.online = False         # janela do app em foco (set-online-presence)
        self.aberta = ""            # conversa aberta no app (subscribe-presence)
        self.thread = None
        self.status = "CLOSED"      # CLOSED INITIALIZING QRCODE CONNECTED
        self.urlcode = None
        self.eu = ""                # JID proprio (usuario@s.whatsapp.net)
        self.lid = ""
        os.makedirs(pasta, exist_ok=True)
        self.bd = Armazem(os.path.join(pasta, "ponte.db"))

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
            if not (telefone and self.status == "QRCODE"):
                return
            # tentativa de pareamento parada (o tempo do QR acabou): recomeca
            self.parar()
            self.thread.join(10)
        self.status = "INITIALIZING"
        self.thread = threading.Thread(
            target=self._rodar, args=(telefone,), daemon=True, name="neonize")
        self.thread.start()

    def _rodar(self, telefone):
        import neonize.events as nev
        from neonize.client import NewClient
        os.makedirs(self.pasta, exist_ok=True)
        cli = self.cliente = NewClient(os.path.join(self.pasta, "sessao.db"),
                                       props=props_do_aparelho(HISTORICO_DIAS))

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
            if not (telefone and self.simular_codigo):   # a simulacao avisa depois do codigo
                self.emitir("session-logged", {"session": self.nome, "status": True})
            threading.Thread(target=self._carregar_nomes, daemon=True).start()

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
                o = e.Info.MessageSource
                chat = jid_texto(o.Chat)
                w = msgmap.montar(
                    conversa=chat, de_mim=bool(o.IsFromMe), id=e.Info.ID,
                    remetente=jid_texto(o.Sender), ts=e.Info.Timestamp,
                    mensagem=e.Message, nome_de_quem=e.Info.Pushname)
                if w is None:
                    return
                self.bd.mensagem(chat, w, msgmap.ficha_midia(e.Message))
                self.bd.ao_chegar(chat, w["t"], w["fromMe"])
                if e.Info.Pushname and not o.IsFromMe:
                    self.bd.contato(jid_texto(o.Sender), pushname=e.Info.Pushname)
                self.emitir("received-message", {"session": self.nome, "response": w})
                # o WhatsApp Web marca como lida a conversa aberta com a janela em foco
                if not o.IsFromMe and self.online and self.aberta == chat:
                    threading.Thread(target=self.marcar_lidas, args=(chat,),
                                     daemon=True).start()
            except Exception:
                log.exception("mensagem recebida")

        @cli.event(nev.ReceiptEv)
        def _recibo(c, e):
            try:
                from neonize.proto.Neonize_pb2 import Receipt
                tipo = Receipt.DESCRIPTOR.fields_by_name["Type"].enum_type \
                    .values_by_number[e.Type].name
                ack = {"DELIVERED": 2, "READ": 3, "PLAYED": 4}.get(tipo)
                if ack is None:
                    return
                chat = msgmap.jid_wpp(jid_texto(e.MessageSource.Chat))
                for mid in e.MessageIDs:
                    self.emitir("onack", {
                        "session": self.nome, "ack": ack, "to": chat, "from": chat,
                        "id": {"fromMe": True, "remote": chat, "id": mid,
                               "_serialized": f"true_{chat}_{mid}"}})
            except Exception:
                log.exception("recibo")

        @cli.event(nev.HistorySyncEv)
        def _historico(c, e):
            try:
                self._guardar_historico(e.Data)
            except Exception:
                log.exception("historico")

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
                # espera a conexao decidir: ja pareada (CONNECTED) nao precisa de codigo
                for _ in range(40):
                    if self.status in ("QRCODE", "CONNECTED"):
                        break
                    time.sleep(0.5)
                if self.status == "CONNECTED":
                    if self.simular_codigo:     # so no modo de teste (--sessao-unica)
                        self.emitir("phoneCode", {"session": self.nome,
                                                  "data": "TEST-0000"})
                        time.sleep(2)
                        self.emitir("session-logged", {"session": self.nome,
                                                       "status": True})
                    return
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

    def _guardar_historico(self, dados):
        for x in dados.pushnames:
            if x.ID and x.pushname:
                self.bd.contato(x.ID, pushname=x.pushname)
        total = 0
        for c in dados.conversations:
            jid = c.ID
            if not jid or jid.endswith("@broadcast") or jid.endswith("@newsletter"):
                continue
            campos = {"name": c.name or c.displayName or None,
                      "ts": msgmap.segundos(c.conversationTimestamp or c.lastMsgTimestamp)}
            if c.HasField("unreadCount"):
                campos["unread"] = int(c.unreadCount)
            if c.HasField("archived"):
                campos["archived"] = int(bool(c.archived))
            if c.HasField("pinned"):
                campos["pinned"] = int(c.pinned)
            if c.HasField("muteEndTime"):
                campos["muted"] = int(c.muteEndTime)
            self.bd.conversa(jid, **{k: v for k, v in campos.items() if v is not None})
            linhas = []
            for h in c.messages:
                m = h.message
                chave = m.key
                w = msgmap.montar(
                    conversa=jid, de_mim=bool(chave.fromMe), id=chave.ID,
                    remetente=chave.participant or m.participant
                    or ("" if chave.fromMe else jid),
                    ts=m.messageTimestamp, mensagem=m.message,
                    nome_de_quem=m.pushName)
                if w is not None and w["id"]["id"]:
                    linhas.append((jid, w, msgmap.ficha_midia(m.message)))
            self.bd.mensagens_em_lote(linhas)
            total += len(linhas)
        log.info("historico: %d conversas, %d mensagens (%s%%)",
                 len(dados.conversations), total, dados.progress)

    def _carregar_nomes(self):
        try:
            for c in self.cliente.contact.get_all_contacts():
                nome = c.Info.FullName or c.Info.FirstName
                self.bd.contato(jid_texto(c.JID), name=nome or None,
                                pushname=c.Info.PushName or None)
        except Exception:
            log.exception("contatos")
        try:
            for g in self.cliente.get_joined_groups():
                self.bd.conversa(jid_texto(g.JID), name=g.GroupName.Name)
        except Exception:
            log.exception("grupos")

    def marcar_lidas(self, conversa):
        """Manda o recibo de leitura das mensagens recebidas e ainda nao lidas."""
        if self.cliente is None or self.status != "CONNECTED":
            return 0
        a_ler = self.bd.recebidas_a_ler(conversa)
        if not a_ler:
            return 0
        from neonize.utils import build_jid
        from neonize.utils.enum import ReceiptType

        def jid_de(texto):
            u, _, srv = msgmap.jid_whatsmeow(texto).partition("@")
            return build_jid(u, srv)

        por_autor = {}
        for mid, autor, ts in a_ler:
            por_autor.setdefault(autor, []).append(mid)
        for autor, ids in por_autor.items():
            try:
                self.cliente.mark_read(*ids, chat=jid_de(conversa), sender=jid_de(autor),
                                       receipt=ReceiptType.READ)
            except Exception:
                log.exception("mark_read")
        log.info("lidas %d mensagem(ns) em %s", len(a_ler), conversa[:6] + "...")
        return len(a_ler)

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


# ---------------------------------------------------------------- HTTP
def criar_app(pasta, fixa=""):
    sio = socketio.AsyncServer(async_mode="aiohttp", cors_allowed_origins="*",
                               logger=False, engineio_logger=False)
    app = web.Application(client_max_size=2 * 1024**3)
    sio.attach(app)
    estado = {"sessoes": {}, "fixa": fixa}

    def sessao(req):
        tok = req.match_info["tok"]
        nome = estado["fixa"] or tok.split(":", 1)[0]
        s = estado["sessoes"].get(nome)
        if s is None:
            s = estado["sessoes"][nome] = Sessao(
                nome, os.path.join(pasta, nome), sio, asyncio.get_running_loop())
        if estado["fixa"]:      # os eventos levam o nome que o cliente usa
            s.nome = tok.split(":", 1)[0]
            s.simular_codigo = True
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
        if s.status == "CONNECTED":     # ja logada: o cliente espera os avisos
            if corpo.get("phone") and s.simular_codigo:
                await sio.emit("phoneCode", {"session": s.nome, "data": "TEST-0000"})
                await asyncio.sleep(2)
            await sio.emit("session-logged", {"session": s.nome, "status": True})
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

    def id_wpp(jid):
        usuario, _, servidor = msgmap.jid_wpp(jid).partition("@")
        return {"server": servidor, "user": usuario,
                "_serialized": f"{usuario}@{servidor}"}

    async def list_chats(req):
        s = sessao(req)
        nomes = {msgmap.jid_wpp(c["jid"]): c["name"] or c["pushname"]
                 for c in s.bd.contatos()}
        saida = []
        for c in s.bd.conversas():
            jid = msgmap.jid_wpp(c["jid"])
            saida.append({
                "id": id_wpp(c["jid"]),
                "name": c["name"] or nomes.get(jid) or None,
                "isGroup": jid.endswith("@g.us"),
                "unreadCount": c["unread"], "t": c["ts"],
                "archive": bool(c["archived"]), "pin": c["pinned"] or False,
                "muteExpiration": c["muted"], "msgs": None,
            })
        return web.json_response(saida)

    async def all_contacts(req):
        s = sessao(req)
        return ok(status="success", response=[{
            "id": id_wpp(c["jid"]), "name": c["name"] or None,
            "pushname": c["pushname"] or None, "shortName": c["name"] or None,
            "isMyContact": bool(c["name"])} for c in s.bd.contatos()])

    async def get_messages(req):
        s = sessao(req)
        q = req.query
        antes = q.get("id") if q.get("direction") == "before" else None
        if antes:   # vem serializado: true_<conversa>_<ID>[_autor]
            partes = antes.split("_")
            antes = partes[2] if len(partes) > 2 else antes
        jid = req.match_info["phone"]
        if "@" not in jid:
            jid += "@c.us"
        conversa = msgmap.jid_whatsmeow(jid)
        itens = s.bd.mensagens(conversa, int(q.get("count", 20)), antes)
        return ok(status="success", response=itens)

    async def send_message(req):
        s = sessao(req)
        corpo = await req.json()
        if s.status != "CONNECTED" or s.cliente is None:
            return web.json_response({"status": "error", "message": "Disconnected"}, status=400)
        from neonize.utils import build_jid
        fone = corpo["phone"]
        if isinstance(fone, list):      # o WinZapp manda uma lista de destinos
            fone = fone[0]
        if "@" in fone:
            u, _, srv = msgmap.jid_whatsmeow(fone).partition("@")
            destino = build_jid(u, srv)
        else:
            destino = build_jid(fone)
        resp = await asyncio.get_running_loop().run_in_executor(
            None, lambda: s.cliente.send_message(destino, corpo["message"]))
        from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import Message
        chat = jid_texto(destino)
        w = msgmap.montar(conversa=chat, de_mim=True, id=resp.ID, remetente=s.eu,
                          ts=resp.Timestamp or time.time(),
                          mensagem=Message(conversation=corpo["message"]))
        s.bd.mensagem(chat, w)
        s.bd.ao_chegar(chat, w["t"], True)
        return ok(status="success", response=[{"id": w["id"]["_serialized"], "ack": 1}])

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
    async def pn_lid(req):
        """Mapeamento numero <-> LID (o neonize guarda no armazenamento do aparelho)."""
        s = sessao(req)
        jid = msgmap.jid_whatsmeow(req.match_info["jid"].replace("~", ""))
        if s.cliente is None or s.status != "CONNECTED":
            return web.json_response({"status": "error", "message": "Disconnected"}, status=503)
        from neonize.utils import build_jid
        usuario, _, servidor = jid.partition("@")
        alvo = build_jid(usuario, servidor)

        def consultar():
            try:
                if servidor == "lid":
                    return jid_texto(s.cliente.get_pn_from_lid(alvo)), jid
                return jid, jid_texto(s.cliente.get_lid_from_pn(alvo))
            except Exception:
                return "", ""
        fone, lid = await asyncio.get_running_loop().run_in_executor(None, consultar)
        if not fone or not lid:
            return web.json_response({"status": "error", "message": "mapeamento nao encontrado"}, status=404)
        return ok(lid={"_serialized": lid}, phone={"_serialized": msgmap.jid_wpp(fone)},
                  id={"_serialized": msgmap.jid_wpp(fone)})

    async def blocklist(req):
        s = sessao(req)
        try:
            lista = await asyncio.get_running_loop().run_in_executor(
                None, lambda: list(s.cliente.get_blocklist().JIDs))
            itens = [{"phone": j.User, "id": {"_serialized": msgmap.jid_wpp(jid_texto(j))}}
                     for j in lista]
        except Exception:
            log.exception("blocklist")
            itens = []
        return ok(status="success", response=itens)

    async def history_status(req):
        s = sessao(req)
        return ok(status="success", response={
            "backendWorkerBridgeReady": True, "unprocessedChunks": 0,
            "initialSyncComplete": True, "recentCompleted": True,
            "onDemandEnabled": False, "chunkStatus": {},
            "storeCounts": s.bd.contagens()})

    async def send_seen(req):
        s = sessao(req)
        corpo = await req.json()
        fone = corpo.get("phone")
        fone = fone[0] if isinstance(fone, list) else fone
        if fone:
            await asyncio.get_running_loop().run_in_executor(
                None, s.marcar_lidas, msgmap.jid_whatsmeow(fone))
        return vazio_ok_resp()

    async def subscribe_presence(req):
        s = sessao(req)
        corpo = await req.json()
        fone = corpo.get("phone")
        fone = fone[0] if isinstance(fone, list) else fone
        if fone:
            s.aberta = msgmap.jid_whatsmeow(fone)
            if s.cliente is not None and s.status == "CONNECTED":
                from neonize.utils import build_jid
                u, _, srv = s.aberta.partition("@")
                try:
                    await asyncio.get_running_loop().run_in_executor(
                        None, s.cliente.subscribe_presence, build_jid(u, srv))
                except Exception:
                    log.info("subscribe_presence nao aceito para %s", s.aberta[:6])
        return ok(status="success", response={})

    async def set_online(req):
        s = sessao(req)
        corpo = await req.json()
        s.online = bool(corpo.get("isOnline"))
        if s.online and s.aberta:      # voltou ao foco: le o que chegou enquanto isso
            await asyncio.get_running_loop().run_in_executor(None, s.marcar_lidas, s.aberta)
        return vazio_ok_resp()

    def vazio_ok_resp():
        return ok(status="success", response={})

    async def get_media(req):
        """Baixa e decifra a midia de uma mensagem guardada (id serializado
        'false_<conversa>_<ID>[_autor]'); bytes brutos ou JSON com base64."""
        s = sessao(req)
        partes = req.match_info["msgid"].split("_")
        id_msg = partes[2] if len(partes) > 2 else partes[-1]
        ficha = s.bd.ficha(id_msg)
        if ficha is None:
            return web.json_response({"status": "error", "message": "midia nao encontrada"}, status=404)
        if s.cliente is None or s.status != "CONNECTED":
            return web.json_response({"status": "error", "message": "Disconnected"}, status=400)
        from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import Message
        mensagem = Message.FromString(ficha)
        try:
            dados = await asyncio.get_running_loop().run_in_executor(
                None, s.cliente.download_any, mensagem)
        except Exception as erro:
            log.exception("download")
            texto = str(erro).lower()
            # midia que saiu do servidor do WhatsApp: o app trata 410 como "expirada"
            status = 410 if ("404" in texto or "410" in texto or "not found" in texto) else 500
            return web.json_response({"status": "error", "message": str(erro)}, status=status)
        if "octet-stream" in req.headers.get("Accept", ""):
            return web.Response(body=dados, content_type="application/octet-stream")
        import base64 as b64
        return ok(base64=b64.b64encode(dados).decode())

    def destino_de(fone):
        from neonize.utils import build_jid
        fone = fone[0] if isinstance(fone, list) else fone
        if "@" in fone:
            u, _, srv = msgmap.jid_whatsmeow(fone).partition("@")
            return build_jid(u, srv)
        return build_jid(fone)

    def guardar_enviada(s, destino, resp, texto_ou_msg=None):
        """Guarda a mensagem recem-enviada e devolve a resposta do WPPConnect."""
        chat = jid_texto(destino)
        w = msgmap.montar(conversa=chat, de_mim=True, id=resp.ID, remetente=s.eu,
                          ts=resp.Timestamp or time.time(), mensagem=resp.Message)
        if w is not None:
            s.bd.mensagem(chat, w, msgmap.ficha_midia(resp.Message))
            s.bd.ao_chegar(chat, w["t"], True)
        return ok(status="success", response=[{
            "id": w["id"]["_serialized"] if w else f"true_{msgmap.jid_wpp(chat)}_{resp.ID}",
            "ack": 1}])

    def enviar_midia(s, destino, tipo, arquivo, legenda, nome, mime, ptt):
        c = s.cliente
        if tipo == "image":
            return c.send_image(destino, arquivo, caption=legenda or None)
        if tipo == "video":
            return c.send_video(destino, arquivo, caption=legenda or None)
        if tipo == "audio":
            return c.send_audio(destino, arquivo, ptt=ptt)
        return c.send_document(destino, arquivo, caption=legenda or None,
                               filename=nome or None, mimetype=mime or None)

    async def send_file(req):
        """Envio multipart (imagem, video, audio, documento) como o WPPConnect."""
        import tempfile
        s = sessao(req)
        if s.status != "CONNECTED" or s.cliente is None:
            return web.json_response({"status": "error", "message": "Disconnected"}, status=400)
        campos, caminho, mime = {}, None, ""
        leitor = await req.multipart()
        try:
            while True:
                parte = await leitor.next()
                if parte is None:
                    break
                if parte.filename is not None:      # o arquivo, direto para o disco
                    sufixo = os.path.splitext(parte.filename)[1]
                    mime = parte.headers.get("Content-Type", "")
                    with tempfile.NamedTemporaryFile(delete=False, suffix=sufixo) as f:
                        caminho = f.name
                        while True:
                            bloco = await parte.read_chunk(1 << 20)
                            if not bloco:
                                break
                            f.write(bloco)
                else:
                    campos.setdefault(parte.name, []).append((await parte.text()))
            if caminho is None:
                return web.json_response({"status": "error", "message": "arquivo ausente"}, status=400)

            def um(nome, padrao=""):
                return (campos.get(nome) or [padrao])[0]
            destino = destino_de(campos.get("phone") or campos.get("phone[]") or [""])
            if um("quotedMessageId"):
                log.info("resposta citada ainda nao suportada no envio de midia: enviando sem citar")
            resp = await asyncio.get_running_loop().run_in_executor(
                None, enviar_midia, s, destino, um("type", "document"), caminho,
                um("caption"), um("filename"), mime, um("isPtt") == "true")
            return guardar_enviada(s, destino, resp)
        except Exception as erro:
            log.exception("send-file")
            return web.json_response({"status": "error", "message": str(erro)}, status=500)
        finally:
            if caminho:
                try:
                    os.unlink(caminho)
                except OSError:
                    pass

    async def send_voice_base64(req):
        import base64 as b64
        s = sessao(req)
        corpo = await req.json()
        if s.status != "CONNECTED" or s.cliente is None:
            return web.json_response({"status": "error", "message": "Disconnected"}, status=400)
        dados = b64.b64decode(corpo["base64Ptt"].split(",", 1)[-1])
        destino = destino_de(corpo["phone"])
        try:
            resp = await asyncio.get_running_loop().run_in_executor(
                None, enviar_midia, s, destino, "audio", dados, "", "", "", True)
        except Exception as erro:
            log.exception("send-voice-base64")
            return web.json_response({"status": "error", "message": str(erro)}, status=500)
        return guardar_enviada(s, destino, resp)

    r.add_post("/api/{tok}/send-file", send_file)
    r.add_post("/api/{tok}/send-voice-base64", send_voice_base64)
    r.add_post("/api/{tok}/get-media-by-message/{msgid}", get_media)
    r.add_post("/api/{tok}/send-seen", send_seen)
    r.add_post("/api/{tok}/typing", vazio_ok)
    r.add_post("/api/{tok}/recording", vazio_ok)
    r.add_post("/api/{tok}/set-limit", vazio_ok)
    r.add_get("/api/{tok}/contact/pn-lid/{jid}", pn_lid)
    r.add_get("/api/{tok}/blocklist", blocklist)
    r.add_get("/api/{tok}/history-sync-status", history_status)
    r.add_post("/api/{tok}/unblock-history-sync", history_status)
    r.add_post("/api/{tok}/set-online-presence", set_online)
    r.add_post("/api/{tok}/subscribe-presence", subscribe_presence)
    r.add_post("/api/{tok}/list-chats", list_chats)
    r.add_get("/api/{tok}/list-chats", list_chats)
    r.add_get("/api/{tok}/all-contacts", all_contacts)
    r.add_get("/api/{tok}/get-messages/{phone}", get_messages)
    r.add_post("/api/{tok}/send-message", send_message)
    r.add_get("/healthz", saude)
    r.add_get("/health", saude)
    return app


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--porta", type=int, default=6300)
    ap.add_argument("--sessao-unica", default="",
                    help="todas as sessoes pedidas usam esta conta (testes)")
    ap.add_argument("--historico-dias", type=int, default=180,
                    help="historico a pedir no pareamento (0 = o padrao do WhatsApp)")
    ap.add_argument("--pasta", default=os.path.join(os.path.dirname(__file__), "dados"))
    a = ap.parse_args()
    global HISTORICO_DIAS
    HISTORICO_DIAS = a.historico_dias
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    web.run_app(criar_app(a.pasta, a.sessao_unica), host="127.0.0.1", port=a.porta, print=None)


if __name__ == "__main__":
    main()
