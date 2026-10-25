"""Mensagem do whatsmeow (proto) -> objeto no formato do WhatsApp Web que o
WPPConnect devolve (type "chat", body, id "false_<conversa>_<ID>", from, to, t).
Sem dependencia de servidor: funcao pura, testada em test_msgmap.py."""
import base64
import hashlib

import botoes
import mensagens_de_comercio

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


def indices_do_voto(nomes, selecionados):
    """Posicoes das opcoes de um voto: o WhatsApp manda o SHA-256 do texto de
    cada opcao marcada."""
    por_hash = {hashlib.sha256(n.encode()).digest(): i for i, n in enumerate(nomes)}
    return sorted({por_hash[bytes(h)] for h in selecionados if bytes(h) in por_hash})


def edicao(mensagem):
    """(id da mensagem editada, mensagem nova) de um aviso de edicao, ou None."""
    from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import ProtocolMessage
    interna = desembrulhar(mensagem)
    if _campo(interna) != "protocolMessage":
        return None
    p = interna.protocolMessage
    if p.type != ProtocolMessage.MESSAGE_EDIT or not p.key.ID:
        return None
    return p.key.ID, p.editedMessage


def fixacao(mensagem):
    """(id da mensagem, fixar?, duracao em segundos) de um aviso de fixar ou
    soltar uma mensagem, ou None."""
    from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import PinInChatMessage
    interna = desembrulhar(mensagem)
    if _campo(interna) != "pinInChatMessage":
        return None
    p = interna.pinInChatMessage
    if not p.key.ID:
        return None
    duracao = int(interna.messageContextInfo.messageAddOnDurationInSecs or 0)
    return p.key.ID, p.type == PinInChatMessage.PIN_FOR_ALL, duracao


def campo_de_midia(mensagem):
    """Nome do campo de midia preenchido (imageMessage...) ou ''."""
    for nome in MIDIAS:
        try:
            if mensagem.HasField(nome):
                return nome
        except ValueError:
            pass
    return ""


def voto(mensagem):
    """(id da enquete, ) de uma mensagem de voto, ou None."""
    interna = desembrulhar(mensagem)
    if _campo(interna) != "pollUpdateMessage":
        return None
    return interna.pollUpdateMessage.pollCreationMessageKey.ID or None


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


def _conteudo(interna, campo, conversa_w, saida) -> bool:
    """Preenche `saida` com o tipo e o conteudo da mensagem; False se for so
    controle/reacao (nada a mostrar)."""
    if campo == "conversation":
        saida["body"] = interna.conversation
    elif campo == "extendedTextMessage":
        e = interna.extendedTextMessage
        saida["body"] = e.text
        if e.title or e.description:
            saida.update(title=e.title, description=e.description,
                         canonicalUrl=e.matchedText)
    elif campo in MIDIAS or campo == "ptvMessage":
        # video redondo: mesmos campos do video, com o type proprio do cliente
        tipo, mime = MIDIAS["videoMessage" if campo == "ptvMessage" else campo]
        m = getattr(interna, campo)
        if campo == "audioMessage":
            tipo = "ptt" if m.PTT else "audio"
            saida["isPtt"] = bool(m.PTT)
        saida.update(type="ptv" if campo == "ptvMessage" else tipo,
                     mimetype=m.mimetype or mime,
                     mediaKey=_b64(m.mediaKey),
                     clientUrl=m.URL or m.directPath,
                     size=int(m.fileLength or 0))
        for nome in ("caption", "seconds", "fileName", "title"):
            if hasattr(m, nome) and getattr(m, nome):
                saida[{"seconds": "duration"}.get(nome, nome)] = getattr(m, nome)
        if campo in ("videoMessage", "ptvMessage"):
            saida["isGif"] = bool(m.gifPlayback)
        if campo == "documentMessage":
            saida["filename"] = m.fileName or m.title
    elif campo == "locationMessage":
        loc = interna.locationMessage
        saida.update(type="location", lat=loc.degreesLatitude,
                     lng=loc.degreesLongitude,
                     loc=loc.name or loc.address, body=loc.name)
    elif campo == "liveLocationMessage":
        loc = interna.liveLocationMessage
        saida.update(type="liveLocation", lat=loc.degreesLatitude,
                     lng=loc.degreesLongitude, loc=loc.caption, body=loc.caption)
    elif campo == "contactMessage":
        saida.update(type="vcard", body=interna.contactMessage.vcard,
                     displayName=interna.contactMessage.displayName)
    elif campo == "contactsArrayMessage":
        # o cliente so tem o cartao unico ("vcard"): vira um cartao cujo nome e
        # o do conjunto ("Ana e outros 2"); os cartoes seguem juntos no corpo
        lista = interna.contactsArrayMessage
        saida.update(type="vcard",
                     body="\n".join(c.vcard for c in lista.contacts if c.vcard),
                     displayName=lista.displayName or next(
                         (c.displayName for c in lista.contacts if c.displayName), ""))
    elif campo == "protocolMessage":
        p = interna.protocolMessage
        if p.type != 0:        # so a revogacao (apagar para todos) importa
            return False
        alvo = p.key.ID
        ser = f"{'true' if p.key.fromMe else 'false'}_{conversa_w}_{alvo}"
        saida.update(type="revoked", body="", protocolMessageKey={
            "id": alvo, "remote": conversa_w, "fromMe": bool(p.key.fromMe),
            "_serialized": ser})
    elif campo.startswith("pollCreationMessage"):
        enquete = getattr(interna, campo)
        saida.update(type="poll_creation", body=enquete.name,
                     pollName=enquete.name,
                     pollSelectableOptionsCount=int(enquete.selectableOptionsCount or 0),
                     pollOptions=[{"name": o.optionName, "localId": i}
                                  for i, o in enumerate(enquete.options)])
    elif campo == "orderMessage":
        saida.update(mensagens_de_comercio.campos_do_pedido(interna.orderMessage))
    elif campo == "productMessage":
        saida.update(mensagens_de_comercio.campos_do_produto(interna.productMessage))
    elif campo in ("reactionMessage", "pollUpdateMessage", "keepInChatMessage", ""):
        return False
    else:
        saida["type"] = "unknown"
        saida.update(botoes.campos_de_botoes(interna))
    return True


# campos de conteudo que carregam contextInfo (resposta, mencao, encaminhada)
CONTEXTO = ("extendedTextMessage", "imageMessage", "videoMessage", "ptvMessage",
            "audioMessage", "documentMessage", "stickerMessage", "locationMessage",
            "liveLocationMessage", "contactMessage", "contactsArrayMessage")

# o que o cliente le de uma citacao: o resto (chaves, miniatura...) so incharia
# o que ele guarda (core/utils.py _slim_quoted_message)
CAMPOS_DA_CITADA = ("type", "body", "caption", "filename", "isPtt")


def _ctx_de(interna):
    """contextInfo do campo de conteudo preenchido, ou None."""
    for nome in CONTEXTO:
        try:
            if interna.HasField(nome):
                return getattr(interna, nome).contextInfo
        except ValueError:
            pass
    return None


def _citada_wpp(quotada, conversa_w):
    """Mensagem citada (proto) -> dicionario no formato `quotedMsg` do
    WPPConnect, ou None se nao ha o que mostrar. Midia leva so tipo, legenda e
    nome do arquivo; o que nao tem previa propria vira texto (a previa do
    cliente so mostra legenda para foto, video, audio, documento e figurinha)."""
    interna = desembrulhar(quotada)
    q = {"type": "chat", "body": ""}
    if not _conteudo(interna, _campo(interna), conversa_w, q):
        return None
    tipo = q.get("type")
    if tipo in ("image", "video", "ptv", "audio", "ptt", "document", "sticker"):
        q["type"] = "video" if tipo == "ptv" else tipo
        q.pop("body", None)
        q["caption"] = q.get("caption") or ""
    elif tipo == "vcard":
        q = {"type": "contact", "caption": q.get("displayName") or ""}
    else:
        # texto, enquete, localizacao...: a previa e o texto da mensagem
        q = {"type": "chat", "body": q.get("body") or ""}
    saida = {k: q[k] for k in CAMPOS_DA_CITADA if k in q}
    ctx = _ctx_de(interna)       # as mencoes da propria citada, ainda a resolver
    mencionados = [jid_wpp(j) for j in ctx.mentionedJID if j] if ctx is not None else []
    if mencionados:
        saida["mentionedJidList"] = mencionados
    return saida


def _contexto(ctx, conversa_w):
    """contextInfo (proto) -> campos de resposta, mencao e encaminhamento do
    formato WPPConnect. `@lid` continua `@lid`: so o `@s.whatsapp.net` vira
    `@c.us` (jid_wpp)."""
    saida = {}
    if ctx.stanzaID:
        saida["quotedStanzaID"] = ctx.stanzaID
        saida["quotedParticipant"] = jid_wpp(ctx.participant)
        if ctx.HasField("quotedMessage"):
            citada_w = _citada_wpp(ctx.quotedMessage, conversa_w)
            if citada_w:
                saida["quotedMsg"] = citada_w
    mencionados = [jid_wpp(j) for j in ctx.mentionedJID if j]
    if mencionados:
        saida["mentionedJidList"] = mencionados
    if ctx.isForwarded:
        saida["isForwarded"] = True
    return saida


def montar(*, conversa: str, de_mim: bool, id: str, remetente: str, ts,
           mensagem, nome_de_quem: str = ""):
    """Objeto no formato WPPConnect, ou None se for so controle/reacao."""
    interna = desembrulhar(mensagem)
    campo = _campo(interna)
    conversa_w = jid_wpp(conversa)
    remetente_w = jid_wpp(remetente)
    grupo = conversa_w.endswith("@g.us")
    status = conversa_w == "status@broadcast"      # o autor e quem publicou
    serial = f"{'true' if de_mim else 'false'}_{conversa_w}_{id}"
    if grupo:
        serial += f"_{remetente_w}"
    saida = {
        "id": {"fromMe": de_mim, "remote": conversa_w, "id": id,
               "_serialized": serial},
        "fromMe": de_mim,
        "from": "" if de_mim else (remetente_w if (grupo or status) else conversa_w),
        "to": conversa_w,
        "t": segundos(ts), "timestamp": segundos(ts),
        "isStatus": conversa_w == "status@broadcast",
        "notifyName": nome_de_quem,
        "type": "chat", "body": "",
    }
    if (grupo or status) and not de_mim:
        saida["author"] = remetente_w
    if not _conteudo(interna, campo, conversa_w, saida):
        return None
    ctx = _ctx_de(interna)
    if ctx is not None:
        saida.update(_contexto(ctx, conversa_w))
    return saida


def previa_de_link(opcoes):
    """`options.linkPreview` dos envios de texto -> campos do ExtendedTextMessage
    (cartao do link), ou None. O cliente ja buscou a pagina na maquina dele: a
    ponte so repassa os campos, nunca busca a URL (o neonize buscaria da maquina
    do usuario a partir de uma URL escolhida por quem chama: SSRF)."""
    previa = opcoes.get("linkPreview") if isinstance(opcoes, dict) else None
    if not isinstance(previa, dict):
        return None
    url = str(previa.get("canonicalUrl") or previa.get("matchedText") or "")
    titulo = str(previa.get("title") or "")
    if not url and not titulo:
        return None
    # no proto o endereco da pagina e o matchedText (nao ha campo canonicalURL)
    return {"title": titulo, "description": str(previa.get("description") or ""),
            "matchedText": url}


def cor_argb(texto, padrao=0xFF1B8755):
    """'#25D366' -> 0xFF25D366 (cor de fundo do status de texto)."""
    t = (texto or "").lstrip("#")
    try:
        return 0xFF000000 | int(t, 16) if len(t) == 6 else padrao
    except ValueError:
        return padrao
