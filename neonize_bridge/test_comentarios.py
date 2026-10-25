"""Comentarios de avisos de comunidade: a cifra (com vetores fixos), o segredo no
banco do whatsmeow, o armazenamento e o recebimento. Sem WhatsApp; a cifra segue
a leitura do msgsecret.go e nada aqui foi provado contra uma conta real."""
import hashlib
import hmac
import os
import sqlite3
from types import SimpleNamespace as N

import pytest
from neonize.proto.waCommon.WACommon_pb2 import MessageKey
from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import (
    EncCommentMessage, ExtendedTextMessage, Message, ProtocolMessage)

import comentarios as com
from store import Armazem

SEGREDO = bytes(range(32))
IV = bytes(0xA0 + i for i in range(12))
# Calculados com o crypto/hkdf e o crypto/cipher do Go sobre a logica de
# generateMsgSecretKey (msgsecret.go), para estes mesmos argumentos.
CHAVE_GO = "abd952f75d77af491838d816826b68235a81a10de4cb0459e3b97c8813109de6"
CIFRADO_GO = "cf1e31a0b5cde872022c37bec232fe25c493af60ec2420661e65f7d24dee5054d8"
CHAVE_GO_NUMEROS = "125e0e6aa9ac8d9d07fa1eff12dc9b5d17dd5e0410cf0d6544fa7ff00e64ddff"
PLANO = bytes.fromhex("0a0f6f6c612c20636f6d756e6964616465")     # Message(conversation=...)

GRUPO = "120363000000000001@g.us"
AUTOR = "111222333@lid"
EU_LID = "999888777666555@lid"
EU = "5511888880000@s.whatsapp.net"


def hkdf_sha256(ikm, info, tamanho=32):
    """RFC 5869 sem sal, escrito a mao: confere a biblioteca."""
    prk = hmac.new(bytes(32), ikm, hashlib.sha256).digest()
    saida, bloco = b"", b""
    contador = 1
    while len(saida) < tamanho:
        bloco = hmac.new(prk, bloco + info + bytes([contador]), hashlib.sha256).digest()
        saida += bloco
        contador += 1
    return saida[:tamanho]


# -------------------------------------------------------------------- cifra
def test_vetores_fixos_do_go():
    chave = com.chave_do_comentario(SEGREDO, "3EB0AABBCCDD1122", "111222333@lid", "444555666@lid")
    assert chave.hex() == CHAVE_GO
    assert com.chave_do_comentario(SEGREDO, "ABC", "5511999990000@s.whatsapp.net",
                                   "5511888880000@s.whatsapp.net").hex() == CHAVE_GO_NUMEROS
    carga, iv = com.cifrar_comentario(SEGREDO, "3EB0AABBCCDD1122", "111222333@lid",
                                      "444555666@lid", PLANO, IV)
    assert (carga.hex(), iv) == (CIFRADO_GO, IV)
    assert Message.FromString(PLANO).conversation == "ola, comunidade"


def test_a_chave_e_o_hkdf_do_rfc_com_o_info_do_whatsmeow():
    info = b"IDX" + b"1@lid" + b"2@lid" + b"Enc Comment"
    assert com.chave_do_comentario(SEGREDO, "IDX", "1@lid", "2@lid") == hkdf_sha256(SEGREDO, info)


def test_dispositivo_e_agente_nao_entram_na_chave():
    base = com.chave_do_comentario(SEGREDO, "A", "1@lid", "2@s.whatsapp.net")
    assert com.chave_do_comentario(SEGREDO, "A", "1:7@lid", "2:3@s.whatsapp.net") == base
    assert com.chave_do_comentario(SEGREDO, "A", "1.0:7@lid", "2@c.us") == base
    assert com.sem_dispositivo("5511:12@s.whatsapp.net") == "5511@s.whatsapp.net"
    assert com.sem_dispositivo("") == ""


def test_ida_e_volta_e_o_tag_vai_no_fim():
    carga, iv = com.cifrar_comentario(SEGREDO, "ID1", AUTOR, EU_LID, b"oi")
    assert len(iv) == 12 and len(carga) == 2 + 16
    assert com.decifrar_comentario(SEGREDO, "ID1", [AUTOR], [EU_LID], iv, carga) == b"oi"


def test_remetente_errado_ou_carga_adulterada_nao_abre():
    carga, iv = com.cifrar_comentario(SEGREDO, "ID1", AUTOR, EU_LID, b"oi")
    with pytest.raises(com.ComentarioIlegivel):
        com.decifrar_comentario(SEGREDO, "ID1", ["1@lid"], [EU_LID], iv, carga)
    with pytest.raises(com.ComentarioIlegivel):
        com.decifrar_comentario(SEGREDO, "ID1", [AUTOR], [EU_LID], iv, carga[:-1] + b"\x00")
    with pytest.raises(com.ComentarioIlegivel):
        com.decifrar_comentario(SEGREDO, "OUTRO", [AUTOR], [EU_LID], iv, carga)
    with pytest.raises(com.ComentarioIlegivel):
        com.decifrar_comentario(SEGREDO, "ID1", [AUTOR], [EU_LID], b"curto", carga)


def test_decifrar_tenta_a_outra_forma_do_remetente():
    """O comentario foi cifrado com o remetente como numero; a chave diz LID."""
    carga, iv = com.cifrar_comentario(SEGREDO, "ID1", "5511@s.whatsapp.net", EU_LID, b"x")
    assert com.decifrar_comentario(
        SEGREDO, "ID1", [AUTOR, "5511@s.whatsapp.net"], [EU, EU_LID], iv, carga) == b"x"


def test_remetente_original_da_chave():
    f = com.remetente_original_da_chave
    assert f(True, "qualquer@lid", "7:3@lid") == "7@lid"           # fromMe: quem comentou
    assert f(False, "8:1@lid", "7@lid") == "8@lid"
    assert f(False, "8@c.us", "7@lid") == "8@s.whatsapp.net"
    assert f(False, "", "7@lid") == ""
    assert f(False, "120363@g.us", "7@lid") == ""


# ------------------------------------------------------------------- segredo
def banco_do_whatsmeow(caminho, segredos=(), mapa=None):
    con = sqlite3.connect(caminho)
    con.execute("CREATE TABLE whatsmeow_message_secrets(our_jid TEXT, chat_jid TEXT, "
                "sender_jid TEXT, message_id TEXT, key BLOB NOT NULL, "
                "PRIMARY KEY(our_jid, chat_jid, sender_jid, message_id))")
    if mapa is not None:
        con.execute("CREATE TABLE whatsmeow_lid_map(lid TEXT PRIMARY KEY, pn TEXT)")
        con.executemany("INSERT INTO whatsmeow_lid_map VALUES (?,?)", mapa)
    con.executemany("INSERT INTO whatsmeow_message_secrets VALUES ('eu:1@s.whatsapp.net',?,?,?,?)",
                    segredos)
    con.commit()
    con.close()


def test_segredo_lido_do_banco_do_whatsmeow(tmp_path):
    caminho = str(tmp_path / "sessao.db")
    banco_do_whatsmeow(caminho, [(GRUPO, AUTOR, "ID1", SEGREDO)], mapa=[])
    assert com.segredo_da_mensagem(caminho, GRUPO, AUTOR, "ID1") == (SEGREDO, AUTOR)
    assert com.segredo_da_mensagem(caminho, GRUPO, "111222333:5@lid", "ID1") == (SEGREDO, AUTOR)
    assert com.segredo_da_mensagem(caminho, GRUPO, AUTOR, "OUTRO") is None
    assert com.segredo_da_mensagem(caminho, "1@g.us", AUTOR, "ID1") is None


def test_segredo_casa_numero_e_lid_pelo_mapa(tmp_path):
    caminho = str(tmp_path / "sessao.db")
    banco_do_whatsmeow(caminho, [(GRUPO, AUTOR, "ID1", SEGREDO)],
                       mapa=[("111222333", "5511777770000")])
    achado = com.segredo_da_mensagem(caminho, GRUPO, "5511777770000@s.whatsapp.net", "ID1")
    assert achado == (SEGREDO, AUTOR)       # devolve o remetente como foi guardado


def test_segredo_sem_a_tabela_do_mapa_ainda_acha_o_exato(tmp_path):
    caminho = str(tmp_path / "sessao.db")
    banco_do_whatsmeow(caminho, [(GRUPO, AUTOR, "ID1", SEGREDO)], mapa=None)
    assert com.segredo_da_mensagem(caminho, GRUPO, AUTOR, "ID1") == (SEGREDO, AUTOR)


def test_segredo_sem_banco_ou_sem_tabela_e_nada_e_nao_escreve(tmp_path):
    assert com.segredo_da_mensagem(str(tmp_path / "nao.db"), GRUPO, AUTOR, "ID1") is None
    vazio = str(tmp_path / "vazio.db")
    sqlite3.connect(vazio).close()
    assert com.segredo_da_mensagem(vazio, GRUPO, AUTOR, "ID1") is None
    caminho = str(tmp_path / "sessao.db")
    banco_do_whatsmeow(caminho, [(GRUPO, AUTOR, "ID1", SEGREDO)], mapa=[])
    antes = open(caminho, "rb").read()
    com.segredo_da_mensagem(caminho, GRUPO, AUTOR, "ID1")
    assert open(caminho, "rb").read() == antes


# --------------------------------------------------------- conteudo e formatos
def test_texto_do_comentario():
    assert com.texto_do_comentario(Message(conversation="a")) == "a"
    assert com.texto_do_comentario(Message(extendedTextMessage=ExtendedTextMessage(text="b"))) == "b"
    midia = Message()
    midia.imageMessage.caption = "legenda"
    assert com.texto_do_comentario(midia) == "legenda"
    assert com.texto_do_comentario(Message()) == ""


def test_mensagem_de_comentario_e_o_que_o_whatsmeow_monta():
    m = com.mensagem_de_comentario(GRUPO, False, "111222333:4@lid", "ID1", b"carga", IV)
    enc = m.encCommentMessage
    assert enc.targetMessageKey.remoteJID == GRUPO and enc.targetMessageKey.ID == "ID1"
    assert enc.targetMessageKey.participant == AUTOR and not enc.targetMessageKey.fromMe
    assert (enc.encPayload, enc.encIV) == (b"carga", IV)


def test_comentario_para_wpp_e_eventos():
    linha = {"id": "C1", "chat": GRUPO, "author": "1@lid", "ts": 50, "type": "comment",
             "body": "oi", "from_me": 0}
    assert com.comentario_para_wpp(linha, "false_g_P_1@lid", "Ana") == {
        "id": "C1", "parentMsgId": "false_g_P_1@lid", "chatId": GRUPO, "author": "1@lid",
        "authorName": "Ana", "fromMe": False, "timestamp": 50, "type": "comment", "body": "oi"}
    assert com.comentario_para_wpp({**linha, "type": "ciphertext"}, "s")["body"] == ""
    assert com.evento_de_contagem("c", "false_g_P", GRUPO, 3) == {
        "session": "c", "id": "false_g_P", "chatId": GRUPO, "replyCount": 3}
    previa = com.evento_de_previa("c", GRUPO, {**linha, "from_me": 1})
    assert previa == {"session": "c", "kind": "preview", "chatId": GRUPO, "preview": {
        "type": "comment", "msgKey": f"true_{GRUPO}_C1", "sender": "1@lid", "timestamp": 50}}
    assert com.evento_de_previa("c", GRUPO, None)["preview"] is None


# ---------------------------------------------------------------- armazenamento
def test_banco_antigo_ganha_a_tabela_sem_perder_nada(tmp_path):
    caminho = str(tmp_path / "ponte.db")
    antigo = sqlite3.connect(caminho)
    antigo.execute("CREATE TABLE chats(jid TEXT PRIMARY KEY, name TEXT DEFAULT '', ts INTEGER "
                   "DEFAULT 0, unread INTEGER DEFAULT 0, archived INTEGER DEFAULT 0, "
                   "pinned INTEGER DEFAULT 0, muted INTEGER DEFAULT 0)")
    antigo.execute("INSERT INTO chats(jid, name) VALUES ('1@g.us', 'Avisos')")
    antigo.commit()
    antigo.close()
    bd = Armazem(caminho)
    assert bd.conversas()[0]["name"] == "Avisos"
    assert bd.comentarios_de("1@g.us", "P") == []
    bd.fechar()
    Armazem(caminho).fechar()       # abrir de novo nao quebra


def test_comentarios_guardados_contados_e_apagados(tmp_path):
    bd = Armazem(str(tmp_path / "ponte.db"))
    bd.guardar_comentario("C1", "P", GRUPO, "1@lid", 10, "comment", "oi", False, "false_g_P_1@lid")
    bd.guardar_comentario("C2", "P", GRUPO, "2@lid", 20, "ciphertext", "", False, "false_g_P_1@lid")
    bd.guardar_comentario("C3", "Q", GRUPO, "2@lid", 30, "comment", "outro", True, "s")
    bd.guardar_comentario("C1", "P", GRUPO, "1@lid", 10, "comment", "REPETIDO", False, "x")
    assert [c["id"] for c in bd.comentarios_de(GRUPO, "P")] == ["C1", "C2"]
    assert bd.comentarios_de(GRUPO, "P")[0]["body"] == "oi"      # o repetido nao troca
    assert bd.contar_comentarios(GRUPO, "P") == 2               # o ilegivel conta
    assert bd.contagens_de_comentarios() == {"P": 2, "Q": 1}
    assert bd.ultimo_comentario(GRUPO)["id"] == "C3"
    # o ilegivel vira legivel quando o mesmo comentario chega de novo e abre
    bd.guardar_comentario("C2", "P", GRUPO, "2@lid", 20, "comment", "agora le", False, "s")
    assert bd.comentarios_de(GRUPO, "P")[1]["body"] == "agora le"
    assert bd.revogar_comentario(GRUPO, "C3") == ("Q", "s")
    assert bd.revogar_comentario(GRUPO, "C3") is None           # ja apagado
    assert bd.revogar_comentario(GRUPO, "NAO") is None
    assert bd.contar_comentarios(GRUPO, "Q") == 0
    assert bd.ultimo_comentario(GRUPO)["id"] == "C2"
    assert bd.comentarios_de(GRUPO, "Q")[0]["type"] == "revoked"
    bd.fechar()


def test_resposta_de_mensagens_leva_o_replycount(tmp_path):
    bd = Armazem(str(tmp_path / "ponte.db"))
    bd.guardar_comentario("C1", "P", GRUPO, "1@lid", 10, "comment", "oi", False, "s")
    saida = bd.para_resposta([{"id": {"id": "P"}, "t": 1}, {"id": {"id": "Z"}, "t": 2}])
    assert saida[0]["replyCount"] == 1 and "replyCount" not in saida[1]
    bd.fechar()


# ------------------------------------------------------------------- recepcao
class SessaoFalsa:
    def __init__(self, pasta):
        self.pasta = str(pasta)
        self.nome = "conta"
        self.eu, self.lid = EU, EU_LID
        self.bd = Armazem(str(pasta / "ponte.db"))
        self.eventos = []

    def emitir(self, evento, dados):
        self.eventos.append((evento, dados))


def evento_de(mensagem, id_msg="C1", quem=AUTOR, de_mim=False, ts=1700000000):
    usuario, _, servidor = quem.partition("@")
    return (N(Sender=N(User=usuario, Server=servidor), IsFromMe=de_mim),
            N(Message=mensagem, Info=N(ID=id_msg, Timestamp=ts, Pushname="Ana")))


def comentario_cifrado(texto, id_pai="P", quem=AUTOR, pai_de=AUTOR, de_mim_na_chave=False,
                       cifrar_como=None):
    carga, iv = com.cifrar_comentario(SEGREDO, id_pai, pai_de, cifrar_como or quem,
                                      com.plano_de_texto(texto))
    return Message(encCommentMessage=EncCommentMessage(
        targetMessageKey=MessageKey(remoteJID=GRUPO, participant=pai_de, fromMe=de_mim_na_chave,
                                    ID=id_pai),
        encPayload=carga, encIV=iv))


@pytest.fixture
def sessao(tmp_path):
    s = SessaoFalsa(tmp_path)
    banco_do_whatsmeow(str(tmp_path / "sessao.db"), [(GRUPO, AUTOR, "P", SEGREDO)], mapa=[])
    yield s
    s.bd.fechar()


def test_comentario_recebido_e_decifrado_guardado_e_anunciado(sessao):
    o, e = evento_de(comentario_cifrado("ola", quem="444555666@lid"), quem="444555666@lid")
    assert com.receber(sessao, GRUPO, o, e) is True
    linha = sessao.bd.comentarios_de(GRUPO, "P")[0]
    assert (linha["id"], linha["type"], linha["body"], linha["author"], linha["ts"]) == (
        "C1", "comment", "ola", "444555666@lid", 1700000000)
    assert linha["from_me"] == 0
    serial = f"false_{GRUPO}_P_{AUTOR}"
    assert sessao.eventos[0] == ("announcement-replies", {
        "session": "conta", "id": serial, "chatId": GRUPO, "replyCount": 1})
    assert sessao.eventos[1][1]["preview"] == {
        "type": "comment", "msgKey": f"false_{GRUPO}_C1_444555666@lid",
        "sender": "444555666@lid", "timestamp": 1700000000}
    assert sessao.bd.contato_de("444555666@lid") == {"pushname": "Ana"}


def test_comentario_da_propria_conta_em_outro_aparelho(sessao):
    """IsFromMe: quem comenta e a conta; o aparelho pode usar o numero em vez do LID."""
    msg = comentario_cifrado("meu", quem=EU_LID)
    o, e = evento_de(msg, quem=EU, de_mim=True)
    assert com.receber(sessao, GRUPO, o, e) is True
    linha = sessao.bd.comentarios_de(GRUPO, "P")[0]
    assert linha["type"] == "comment" and linha["body"] == "meu" and linha["from_me"] == 1


def test_mensagem_comentada_minha_usa_fromme_da_chave(sessao):
    """O aviso e meu (fromMe na chave) e eu mesma comentei: o remetente original e eu."""
    os.remove(os.path.join(sessao.pasta, "sessao.db"))
    banco_do_whatsmeow(os.path.join(sessao.pasta, "sessao.db"),
                       [(GRUPO, EU_LID, "P", SEGREDO)], mapa=[])
    msg = comentario_cifrado("eu", pai_de=EU_LID, quem=EU_LID, de_mim_na_chave=True)
    o, e = evento_de(msg, quem=EU_LID, de_mim=True)
    assert com.receber(sessao, GRUPO, o, e) is True
    assert sessao.bd.comentarios_de(GRUPO, "P")[0]["body"] == "eu"
    assert sessao.eventos[0][1]["id"].startswith(f"true_{GRUPO}_P")


def test_sem_segredo_ou_segredo_errado_vira_ciphertext(sessao, tmp_path):
    o, e = evento_de(comentario_cifrado("x", id_pai="SEM_SEGREDO"), id_msg="C9")
    assert com.receber(sessao, GRUPO, o, e) is True
    assert sessao.bd.comentarios_de(GRUPO, "SEM_SEGREDO")[0]["type"] == "ciphertext"
    # existe o segredo, mas o comentario foi cifrado com outro (adulterado)
    msg = comentario_cifrado("x")
    msg.encCommentMessage.encPayload = msg.encCommentMessage.encPayload[:-1] + b"\x00"
    o, e = evento_de(msg, id_msg="C10")
    com.receber(sessao, GRUPO, o, e)
    linha = sessao.bd.comentarios_de(GRUPO, "P")[0]
    assert linha["type"] == "ciphertext" and linha["body"] == ""
    assert sessao.eventos[-2][1]["replyCount"] == 1     # o ilegivel conta


def test_comentario_fora_de_grupo_e_ignorado(sessao):
    o, e = evento_de(comentario_cifrado("x"))
    assert com.receber(sessao, "5511@s.whatsapp.net", o, e) is True
    assert sessao.eventos == []


def test_apagar_comentario_baixa_a_contagem_e_a_previa(sessao):
    o, e = evento_de(comentario_cifrado("um"), id_msg="C1")
    com.receber(sessao, GRUPO, o, e)
    o, e = evento_de(comentario_cifrado("dois"), id_msg="C2", ts=1700000100)
    com.receber(sessao, GRUPO, o, e)
    sessao.eventos.clear()
    aviso = Message(protocolMessage=ProtocolMessage(
        type=ProtocolMessage.REVOKE, key=MessageKey(remoteJID=GRUPO, ID="C2")))
    o, e = evento_de(aviso, id_msg="R1")
    assert com.receber(sessao, GRUPO, o, e) is True
    assert sessao.bd.comentarios_de(GRUPO, "P")[1]["type"] == "revoked"
    assert sessao.eventos[0][1]["replyCount"] == 1
    assert sessao.eventos[1][1]["preview"]["msgKey"].split("_")[2] == "C1"


def test_apagar_mensagem_comum_nao_e_comentario(sessao):
    aviso = Message(protocolMessage=ProtocolMessage(
        type=ProtocolMessage.REVOKE, key=MessageKey(remoteJID=GRUPO, ID="QUALQUER")))
    o, e = evento_de(aviso)
    assert com.receber(sessao, GRUPO, o, e) is False
    o, e = evento_de(Message(conversation="oi"))
    assert com.receber(sessao, GRUPO, o, e) is False
    assert sessao.eventos == []
