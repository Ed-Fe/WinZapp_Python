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
