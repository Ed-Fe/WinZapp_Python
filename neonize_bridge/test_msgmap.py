from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import Message

import msgmap


def test_texto_privado_recebido():
    m = msgmap.montar(conversa="5551999990000@s.whatsapp.net", de_mim=False, id="ABC",
                      remetente="5551999990000@s.whatsapp.net", ts=1700000000,
                      mensagem=Message(conversation="oi"))
    assert m["type"] == "chat" and m["body"] == "oi" and not m["fromMe"]
    assert m["id"]["_serialized"] == "false_5551999990000@c.us_ABC"
    assert m["from"] == "5551999990000@c.us" and m["t"] == 1700000000


def test_grupo_leva_autor_no_id():
    m = msgmap.montar(conversa="1203@g.us", de_mim=False, id="X",
                      remetente="5551:12@s.whatsapp.net", ts=1700000000000,
                      mensagem=Message(conversation="a"))
    assert m["id"]["_serialized"] == "false_1203@g.us_X_5551@c.us"
    assert m["author"] == "5551@c.us" and m["t"] == 1700000000


def test_imagem_com_legenda():
    msg = Message()
    msg.imageMessage.caption = "foto"
    msg.imageMessage.mediaKey = b"\x01\x02"
    m = msgmap.montar(conversa="1@s.whatsapp.net", de_mim=True, id="I",
                      remetente="2@s.whatsapp.net", ts=5, mensagem=msg)
    assert m["type"] == "image" and m["caption"] == "foto" and m["fromMe"]
    assert m["mediaKey"] == "AQI="


def test_reacao_nao_vira_mensagem():
    msg = Message()
    msg.reactionMessage.text = "x"
    assert msgmap.montar(conversa="1@s.whatsapp.net", de_mim=False, id="R",
                         remetente="1@s.whatsapp.net", ts=1, mensagem=msg) is None


def test_jids():
    assert msgmap.jid_wpp("a:3@s.whatsapp.net") == "a@c.us"
    assert msgmap.jid_whatsmeow("a@c.us") == "a@s.whatsapp.net"
    assert msgmap.jid_whatsmeow("a@lid") == "a@lid"


def test_ficha_da_midia_guarda_chaves_e_tira_miniatura_e_legenda():
    msg = Message()
    msg.imageMessage.caption = "foto"
    msg.imageMessage.mediaKey = b"\x01\x02"
    msg.imageMessage.directPath = "/v/t62/x"
    msg.imageMessage.JPEGThumbnail = b"\xff\xd8"
    ficha = msgmap.ficha_midia(msg)
    volta = Message.FromString(ficha)
    assert volta.imageMessage.mediaKey == b"\x01\x02"
    assert volta.imageMessage.directPath == "/v/t62/x"
    assert not volta.imageMessage.caption and not volta.imageMessage.JPEGThumbnail


def test_texto_nao_tem_ficha():
    assert msgmap.ficha_midia(Message(conversation="oi")) is None


def test_citada_de_texto_e_de_imagem():
    assert msgmap.citada({"type": "chat", "body": "oi"}).conversation == "oi"
    img = msgmap.citada({"type": "image", "caption": "foto", "mimetype": "image/png"})
    assert img.imageMessage.caption == "foto" and img.imageMessage.mimetype == "image/png"
    assert msgmap.citada({"type": "ptt"}).audioMessage.PTT is True


def test_participante_citado():
    assert msgmap.participante_citado({"fromMe": True}, "eu@s.whatsapp.net") == "eu@s.whatsapp.net"
    assert msgmap.participante_citado({"author": "5@c.us"}, "x") == "5@s.whatsapp.net"
    assert msgmap.participante_citado({"from": "9@lid"}, "x") == "9@lid"


def test_reacao_e_reconhecida_e_nao_vira_mensagem():
    msg = Message()
    msg.reactionMessage.key.ID = "ALVO"
    msg.reactionMessage.key.fromMe = True
    msg.reactionMessage.key.participant = "5551:3@s.whatsapp.net"
    msg.reactionMessage.text = "\U0001F44D"
    assert msgmap.reacao(msg) == ("ALVO", "\U0001F44D", True, "5551@c.us")
    assert msgmap.montar(conversa="1@s.whatsapp.net", de_mim=False, id="R",
                         remetente="1@s.whatsapp.net", ts=1, mensagem=msg) is None
    msg.reactionMessage.text = ""
    assert msgmap.reacao(msg)[1] == ""          # reacao tirada
    assert msgmap.reacao(Message(conversation="oi")) is None


def test_enquete_leva_posicao_e_limite_das_opcoes():
    msg = Message()
    msg.pollCreationMessage.name = "Almoco?"
    msg.pollCreationMessage.selectableOptionsCount = 1
    for nome in ("Sim", "Nao"):
        msg.pollCreationMessage.options.add().optionName = nome
    m = msgmap.montar(conversa="1@g.us", de_mim=False, id="P", remetente="2@s.whatsapp.net",
                      ts=5, mensagem=msg)
    assert m["type"] == "poll_creation" and m["pollSelectableOptionsCount"] == 1
    assert m["pollOptions"] == [{"name": "Sim", "localId": 0}, {"name": "Nao", "localId": 1}]


def test_voto_e_reconhecido_e_as_opcoes_saem_do_hash():
    import hashlib
    msg = Message()
    msg.pollUpdateMessage.pollCreationMessageKey.ID = "P"
    assert msgmap.voto(msg) == "P" and msgmap.voto(Message(conversation="x")) is None
    hashes = [hashlib.sha256(n.encode()).digest() for n in ("Nao", "Talvez")]
    assert msgmap.indices_do_voto(["Sim", "Nao", "Talvez"], hashes) == [1, 2]
    assert msgmap.indices_do_voto(["Sim"], [b"outro"]) == []


def test_aviso_de_edicao_e_reconhecido_e_nao_vira_mensagem():
    from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import ProtocolMessage
    msg = Message()
    msg.protocolMessage.type = ProtocolMessage.MESSAGE_EDIT
    msg.protocolMessage.key.ID = "ORIG"
    msg.protocolMessage.editedMessage.conversation = "texto novo"
    alvo, nova = msgmap.edicao(msg)
    assert alvo == "ORIG" and nova.conversation == "texto novo"
    assert msgmap.edicao(Message(conversation="oi")) is None
    assert msgmap.montar(conversa="1@s.whatsapp.net", de_mim=False, id="E",
                         remetente="1@s.whatsapp.net", ts=1, mensagem=msg) is None


def test_campo_de_midia():
    msg = Message()
    msg.imageMessage.caption = "x"
    assert msgmap.campo_de_midia(msg) == "imageMessage"
    assert msgmap.campo_de_midia(Message(conversation="oi")) == ""


def test_aviso_de_fixar_e_soltar_mensagem():
    from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import PinInChatMessage
    msg = Message()
    msg.pinInChatMessage.key.ID = "ALVO"
    msg.pinInChatMessage.type = PinInChatMessage.PIN_FOR_ALL
    msg.messageContextInfo.messageAddOnDurationInSecs = 604800
    assert msgmap.fixacao(msg) == ("ALVO", True, 604800)
    msg.pinInChatMessage.type = PinInChatMessage.UNPIN_FOR_ALL
    assert msgmap.fixacao(msg)[1] is False
    assert msgmap.fixacao(Message(conversation="oi")) is None


def test_status_leva_quem_publicou_como_origem_e_autor():
    m = msgmap.montar(conversa="status@broadcast", de_mim=False, id="S",
                      remetente="5551:7@s.whatsapp.net", ts=5,
                      mensagem=Message(conversation="bom dia"))
    assert m["isStatus"] and m["from"] == "5551@c.us" and m["author"] == "5551@c.us"
    meu = msgmap.montar(conversa="status@broadcast", de_mim=True, id="T",
                        remetente="1@s.whatsapp.net", ts=5,
                        mensagem=Message(conversation="oi"))
    assert meu["fromMe"] and meu["from"] == ""


def test_cor_do_status():
    assert msgmap.cor_argb("#25D366") == 0xFF25D366
    assert msgmap.cor_argb("lixo") == 0xFF1B8755 and msgmap.cor_argb(None) == 0xFF1B8755


def _montar(msg, conversa="1203@g.us"):
    return msgmap.montar(conversa=conversa, de_mim=False, id="M",
                         remetente="5551@s.whatsapp.net", ts=5, mensagem=msg)


def test_mencao_encaminhada_e_resposta_de_texto():
    msg = Message()
    e = msg.extendedTextMessage
    e.text = "oi @a @b"
    e.contextInfo.mentionedJID.extend(["5551:3@s.whatsapp.net", "777@lid"])
    e.contextInfo.isForwarded = True
    e.contextInfo.stanzaID = "ORIG"
    e.contextInfo.participant = "5552@s.whatsapp.net"
    e.contextInfo.quotedMessage.conversation = "texto citado"
    m = _montar(msg)
    assert m["mentionedJidList"] == ["5551@c.us", "777@lid"]
    assert m["isForwarded"] is True
    assert m["quotedStanzaID"] == "ORIG" and m["quotedParticipant"] == "5552@c.us"
    assert m["quotedMsg"] == {"type": "chat", "body": "texto citado"}


def test_sem_contexto_nao_leva_campos_de_contexto():
    m = _montar(Message(conversation="oi"))
    for chave in ("mentionedJidList", "isForwarded", "quotedMsg", "quotedStanzaID"):
        assert chave not in m


def test_so_encaminhada_ou_so_mencao_sem_resposta():
    msg = Message()
    msg.imageMessage.contextInfo.isForwarded = True
    m = _montar(msg)
    assert m["isForwarded"] is True and "quotedMsg" not in m and "quotedStanzaID" not in m
    msg = Message()
    msg.extendedTextMessage.text = "@x"
    msg.extendedTextMessage.contextInfo.mentionedJID.append("9@lid")
    m = _montar(msg)
    assert m["mentionedJidList"] == ["9@lid"] and "isForwarded" not in m


def test_citada_de_midia_leva_tipo_legenda_e_nome_sem_chaves():
    msg = Message()
    msg.imageMessage.caption = "resposta"
    c = msg.imageMessage.contextInfo
    c.stanzaID = "Q"
    c.quotedMessage.documentMessage.fileName = "a.pdf"
    c.quotedMessage.documentMessage.caption = "contrato"
    c.quotedMessage.documentMessage.mediaKey = b"\x01"
    c.quotedMessage.documentMessage.JPEGThumbnail = b"\xff"
    q = _montar(msg)["quotedMsg"]
    assert q == {"type": "document", "caption": "contrato", "filename": "a.pdf"}


def test_citada_de_nota_de_voz_figurinha_video_redondo_contato_e_enquete():
    def citando(preencher):
        msg = Message()
        msg.extendedTextMessage.contextInfo.stanzaID = "Q"
        preencher(msg.extendedTextMessage.contextInfo.quotedMessage)
        return _montar(msg)["quotedMsg"]
    assert citando(lambda q: setattr(q.audioMessage, "PTT", True)) == {
        "type": "ptt", "caption": "", "isPtt": True}
    assert citando(lambda q: setattr(q.stickerMessage, "isAnimated", False)) == {
        "type": "sticker", "caption": ""}
    assert citando(lambda q: setattr(q.ptvMessage, "seconds", 3))["type"] == "video"

    def contato(q):
        q.contactMessage.displayName = "Ana"
    assert citando(contato) == {"type": "contact", "caption": "Ana"}

    def enquete(q):
        q.pollCreationMessage.name = "Almoco?"
    assert citando(enquete) == {"type": "chat", "body": "Almoco?"}


def test_citada_dentro_de_embrulho_e_com_mencoes_proprias():
    msg = Message()
    msg.extendedTextMessage.contextInfo.stanzaID = "Q"
    interna = msg.extendedTextMessage.contextInfo.quotedMessage
    e = interna.ephemeralMessage.message.extendedTextMessage
    e.text = "oi @x"
    e.contextInfo.mentionedJID.append("5@s.whatsapp.net")
    assert _montar(msg)["quotedMsg"] == {
        "type": "chat", "body": "oi @x", "mentionedJidList": ["5@c.us"]}


def test_citacao_de_reacao_nao_vira_quotedmsg():
    msg = Message()
    msg.extendedTextMessage.contextInfo.stanzaID = "Q"
    msg.extendedTextMessage.contextInfo.quotedMessage.reactionMessage.text = "x"
    m = _montar(msg)
    assert m["quotedStanzaID"] == "Q" and "quotedMsg" not in m


def test_video_redondo_vira_ptv_com_os_campos_do_video():
    msg = Message()
    msg.ptvMessage.seconds = 7
    msg.ptvMessage.mediaKey = b"\x01\x02"
    msg.ptvMessage.directPath = "/v/x"
    msg.ptvMessage.contextInfo.isForwarded = True
    m = _montar(msg)
    assert m["type"] == "ptv" and m["duration"] == 7 and m["mediaKey"] == "AQI="
    assert m["mimetype"] == "video/mp4" and m["clientUrl"] == "/v/x" and m["isGif"] is False
    assert m["isForwarded"] is True


def test_localizacao_em_tempo_real_vira_liveLocation():
    msg = Message()
    msg.liveLocationMessage.degreesLatitude = -30.5
    msg.liveLocationMessage.degreesLongitude = -51.25
    msg.liveLocationMessage.caption = "Cheguei"
    m = _montar(msg)
    assert m["type"] == "liveLocation" and m["lat"] == -30.5 and m["lng"] == -51.25
    assert m["loc"] == "Cheguei" and m["body"] == "Cheguei"


def test_varios_contatos_viram_um_cartao_com_o_nome_do_conjunto():
    msg = Message()
    msg.contactsArrayMessage.displayName = "Ana e outros 1"
    for nome in ("Ana", "Beto"):
        c = msg.contactsArrayMessage.contacts.add()
        c.displayName = nome
        c.vcard = f"BEGIN:VCARD\nFN:{nome}\nEND:VCARD"
    m = _montar(msg)
    assert m["type"] == "vcard" and m["displayName"] == "Ana e outros 1"
    assert m["body"].count("BEGIN:VCARD") == 2
    msg.contactsArrayMessage.displayName = ""
    assert _montar(msg)["displayName"] == "Ana"


def test_protocolo_que_nao_e_revogacao_continua_sem_mensagem():
    from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import ProtocolMessage
    msg = Message()
    msg.protocolMessage.type = ProtocolMessage.EPHEMERAL_SETTING
    assert _montar(msg) is None
