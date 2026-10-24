"""Mensagem do whatsmeow (proto) -> objeto no formato do WhatsApp Web que o
WPPConnect devolve (type "chat", body, id "false_<conversa>_<ID>", from, to, t).
Sem dependencia de servidor: funcao pura, testada em test_msgmap.py."""
import base64

# camadas que so embrulham outra mensagem
EMBRULHOS = ("ephemeralMessage", "viewOnceMessage", "viewOnceMessageV2",
             "viewOnceMessageV2Extension", "documentWithCaptionMessage",
             "deviceSentMessage", "editedMessage", "botInvokeMessage")

MIDIAS = {  # campo -> (type do WhatsApp Web, mimetype padrao)
    "imageMessage": ("image", "image/jpeg"),
    "videoMessage": ("video", "video/mp4"),
    "audioMessage": ("audio", "audio/ogg; codecs=opus"),
    "documentMessage": ("document", ""),
    "stickerMessage": ("sticker", "image/webp"),
}


def segundos(ts):
    """Timestamp (segundos ou milissegundos) -> segundos inteiros."""
    t = float(ts or 0)
    return int(t / 1000 if t > 1e11 else t)


def jid_wpp(jid: str) -> str:
    """'x:12@s.whatsapp.net' -> 'x@c.us' (o WhatsApp Web usa @c.us)."""
    if not jid:
        return ""
    usuario, _, servidor = jid.partition("@")
    usuario = usuario.split(":")[0]
    if servidor == "s.whatsapp.net":
        servidor = "c.us"
    return f"{usuario}@{servidor}"


def jid_whatsmeow(jid: str) -> str:
    """'x@c.us' -> 'x@s.whatsapp.net'."""
    usuario, _, servidor = (jid or "").partition("@")
    return f"{usuario}@{'s.whatsapp.net' if servidor in ('c.us', '') else servidor}"


def desembrulhar(msg):
    """Mensagem sem as camadas de embrulho (efemera, uma vez, edicao...)."""
    for _ in range(6):
        for nome in EMBRULHOS:
            try:
                if not msg.HasField(nome):
                    continue
            except ValueError:
                continue
            interna = getattr(msg, nome).message
            if not interna.ListFields():
                return msg
            msg = interna
            break
        else:
            return msg
    return msg


def ficha_midia(mensagem):
    """O minimo que o WhatsApp exige para baixar a midia depois (chaves e
    caminho), sem miniatura nem legenda nem resposta citada; bytes do proto
    ou None se a mensagem nao tem midia."""
    from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import Message
    interna = desembrulhar(mensagem)
    try:
        if interna.HasField("ptvMessage"):      # video redondo: baixa como videoMessage
            video = type(interna.videoMessage)()
            video.CopyFrom(interna.ptvMessage)
            interna = Message(videoMessage=video)
    except ValueError:
        pass
    for nome in MIDIAS:
        try:
            if not interna.HasField(nome):
                continue
        except ValueError:
            continue
        sub = type(getattr(interna, nome))()
        sub.CopyFrom(getattr(interna, nome))
        for campo in ("contextInfo", "JPEGThumbnail", "caption"):
            try:
                sub.ClearField(campo)
            except ValueError:
                pass
        return Message(**{nome: sub}).SerializeToString()
    return None


def citada(w):
    """Mensagem minima para o balao de citacao a partir do formato WhatsApp Web."""
    from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import Message
    m = Message()
    tipo, corpo = w.get("type"), w.get("body") or ""
    if tipo == "image":
        m.imageMessage.caption = w.get("caption") or ""
        m.imageMessage.mimetype = w.get("mimetype") or "image/jpeg"
    elif tipo == "video":
        m.videoMessage.caption = w.get("caption") or ""
        m.videoMessage.mimetype = w.get("mimetype") or "video/mp4"
    elif tipo in ("audio", "ptt"):
        m.audioMessage.PTT = tipo == "ptt"
        m.audioMessage.mimetype = w.get("mimetype") or "audio/ogg; codecs=opus"
    elif tipo == "document":
        m.documentMessage.fileName = w.get("filename") or ""
        m.documentMessage.caption = w.get("caption") or ""
        m.documentMessage.mimetype = w.get("mimetype") or ""
    elif tipo == "sticker":
        m.stickerMessage.mimetype = w.get("mimetype") or "image/webp"
    else:
        m.conversation = corpo
    return m


def participante_citado(w, eu):
    """Quem escreveu a mensagem citada, no formato whatsmeow."""
    if w.get("fromMe"):
        return eu
    return jid_whatsmeow(w.get("author") or w.get("from") or "")


def _b64(dado) -> str:
    return base64.b64encode(dado).decode() if dado else ""


def _campo(msg):
    """Nome do campo de conteudo preenchido (oneof implicito) ou ''."""
    for f, _ in msg.ListFields():
        if f.name not in ("messageContextInfo", "senderKeyDistributionMessage"):
            return f.name
    return ""


def reacao(mensagem):
    """(id da mensagem alvo, emoji, key.fromMe, participante) de uma mensagem
    de reacao, ou None se a mensagem nao e uma reacao. Emoji vazio = reacao
    tirada. `fromMe` e do ponto de vista de quem reagiu."""
    interna = desembrulhar(mensagem)
    if _campo(interna) != "reactionMessage":
        return None
    r = interna.reactionMessage
    if not r.key.ID:
        return None
    return r.key.ID, r.text, bool(r.key.fromMe), jid_wpp(r.key.participant)


def montar(*, conversa: str, de_mim: bool, id: str, remetente: str, ts,
           mensagem, nome_de_quem: str = ""):
    """Objeto no formato WPPConnect, ou None se for so controle/reacao."""
    interna = desembrulhar(mensagem)
    campo = _campo(interna)
    conversa_w = jid_wpp(conversa)
    remetente_w = jid_wpp(remetente)
    grupo = conversa_w.endswith("@g.us")
    serial = f"{'true' if de_mim else 'false'}_{conversa_w}_{id}"
    if grupo:
        serial += f"_{remetente_w}"
    saida = {
        "id": {"fromMe": de_mim, "remote": conversa_w, "id": id,
               "_serialized": serial},
        "fromMe": de_mim,
        "from": "" if de_mim else (remetente_w if grupo else conversa_w),
        "to": conversa_w,
        "t": segundos(ts), "timestamp": segundos(ts),
        "isStatus": conversa_w == "status@broadcast",
        "notifyName": nome_de_quem,
        "type": "chat", "body": "",
    }
    if grupo and not de_mim:
        saida["author"] = remetente_w
    if campo == "conversation":
        saida["body"] = interna.conversation
    elif campo == "extendedTextMessage":
        e = interna.extendedTextMessage
        saida["body"] = e.text
        if e.title or e.description:
            saida.update(title=e.title, description=e.description,
                         canonicalUrl=e.matchedText)
    elif campo in MIDIAS:
        tipo, mime = MIDIAS[campo]
        m = getattr(interna, campo)
        if campo == "audioMessage":
            tipo = "ptt" if m.PTT else "audio"
            saida["isPtt"] = bool(m.PTT)
        saida.update(type=tipo, mimetype=m.mimetype or mime,
                     mediaKey=_b64(m.mediaKey),
                     clientUrl=m.URL or m.directPath,
                     size=int(m.fileLength or 0))
        for nome in ("caption", "seconds", "fileName", "title"):
            if hasattr(m, nome) and getattr(m, nome):
                saida[{"seconds": "duration"}.get(nome, nome)] = getattr(m, nome)
        if campo == "videoMessage":
            saida["isGif"] = bool(m.gifPlayback)
        if campo == "documentMessage":
            saida["filename"] = m.fileName or m.title
    elif campo == "locationMessage":
        loc = interna.locationMessage
        saida.update(type="location", lat=loc.degreesLatitude,
                     lng=loc.degreesLongitude,
                     loc=loc.name or loc.address, body=loc.name)
    elif campo == "contactMessage":
        saida.update(type="vcard", body=interna.contactMessage.vcard,
                     displayName=interna.contactMessage.displayName)
    elif campo == "protocolMessage":
        p = interna.protocolMessage
        if p.type != 0:        # so a revogacao (apagar para todos) importa
            return None
        alvo = p.key.ID
        ser = f"{'true' if p.key.fromMe else 'false'}_{conversa_w}_{alvo}"
        saida.update(type="revoked", body="", protocolMessageKey={
            "id": alvo, "remote": conversa_w, "fromMe": bool(p.key.fromMe),
            "_serialized": ser})
    elif campo.startswith("pollCreationMessage"):
        enquete = getattr(interna, campo)
        saida.update(type="poll_creation", body=enquete.name,
                     pollName=enquete.name,
                     pollOptions=[{"name": o.optionName} for o in enquete.options])
    elif campo in ("reactionMessage", "pollUpdateMessage", "keepInChatMessage", ""):
        return None
    else:
        saida["type"] = "unknown"
    ctx = None
    for nome in ("extendedTextMessage", "imageMessage", "videoMessage",
                 "audioMessage", "documentMessage", "stickerMessage"):
        try:
            if interna.HasField(nome):
                ctx = getattr(interna, nome).contextInfo
                break
        except ValueError:
            pass
    if ctx is not None and ctx.stanzaID:
        saida["quotedStanzaID"] = ctx.stanzaID
        saida["quotedParticipant"] = jid_wpp(ctx.participant)
    return saida
