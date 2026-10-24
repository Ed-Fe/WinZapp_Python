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
