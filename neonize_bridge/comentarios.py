"""Comentarios dos avisos de comunidade: a cifra, o segredo da mensagem e o
recebimento. As rotas ficam em comentarios_rotas.py.

Num grupo de avisos de comunidade, quem comenta uma mensagem manda uma
`EncCommentMessage`: o comentario (um `waE2E.Message` serializado) cifrado com
AES-GCM por uma chave derivada do `messageSecret` da mensagem comentada. A
derivacao e a mesma do whatsmeow (msgsecret.go, `generateMsgSecretKey`):

    chave = HKDF-SHA256(ikm=segredo, sem sal,
                        info = ID original + remetente original (sem
                               dispositivo) + remetente do comentario (sem
                               dispositivo) + "Enc Comment", 32 bytes)

AES-GCM com IV de 12 bytes, SEM dados adicionais (so voto de enquete e
resposta de evento levam `ID\\0remetente`) e o tag no fim do texto cifrado.

O whatsmeow ja guarda o `messageSecret` de toda mensagem de grupo que passa por
ele, em `whatsmeow_message_secrets` do sessao.db: aqui so se le esse banco, em
modo somente leitura (`segredo_da_mensagem`). Sem o segredo o comentario vira
`ciphertext`, que o cliente mostra como "nao consegui ler".

ATENCAO: nada daqui foi provado contra o WhatsApp de verdade. A cifra tem
vetores fixos (test_comentarios.py), calculados com o `crypto/hkdf` e o
`crypto/cipher` do Go sobre a mesma logica do msgsecret.go; o formato do
comentario enviado e a aceitacao dele pelo servidor nao tem prova nenhuma.

Nada aqui registra JID, numero, nome ou texto de mensagem em log.
"""
import logging
import os
import pathlib
import sqlite3
import time

import msgmap
import recibos_de_grupo

log = logging.getLogger("ponte")

USO_COMENTARIO = "Enc Comment"
TAMANHO_DO_IV = 12
ARQUIVO_DA_SESSAO = "sessao.db"

TIPO_COMENTARIO = "comment"
TIPO_APAGADO = "revoked"
TIPO_ILEGIVEL = "ciphertext"

SERVIDORES_DE_PESSOA = ("s.whatsapp.net", "lid")


class ComentarioIlegivel(Exception):
    """O comentario nao abriu com nenhuma das combinacoes de remetente."""


# ------------------------------------------------------------------- cifra
def sem_dispositivo(jid: str) -> str:
    """'u:12@s.whatsapp.net' / 'u.0:12@lid' -> 'u@s.whatsapp.net' (`ToNonAD`:
    sem agente nem dispositivo). Uma forma do WPPConnect (@c.us) vira a do
    whatsmeow."""
    usuario, _, servidor = str(jid or "").partition("@")
    usuario = usuario.split(":")[0].split(".")[0]
    if servidor in ("c.us", ""):
        servidor = "s.whatsapp.net"
    return f"{usuario}@{servidor}" if usuario else ""


def chave_do_comentario(segredo: bytes, id_original: str, remetente_original: str,
                        remetente_do_comentario: str) -> bytes:
    """A chave AES-256 de um comentario (generateMsgSecretKey, "Enc Comment")."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    info = (id_original + sem_dispositivo(remetente_original)
            + sem_dispositivo(remetente_do_comentario) + USO_COMENTARIO).encode()
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=info).derive(segredo)


def cifrar_comentario(segredo, id_original, remetente_original, remetente_do_comentario,
                      texto_plano: bytes, iv: bytes = None):
    """(texto cifrado com o tag, iv) de um comentario ja serializado."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    iv = iv if iv is not None else os.urandom(TAMANHO_DO_IV)
    chave = chave_do_comentario(segredo, id_original, remetente_original,
                                remetente_do_comentario)
    return AESGCM(chave).encrypt(iv, bytes(texto_plano), None), iv


def decifrar_comentario(segredo, id_original, remetentes_originais, remetentes_do_comentario,
                        iv: bytes, carga: bytes) -> bytes:
    """O comentario aberto. O WhatsApp esta trocando numero por LID, entao
    quem comentou e quem escreveu a mensagem original podem aparecer em uma
    forma ou na outra: tenta cada combinacao (o `decryptMsgSecret` do whatsmeow
    tenta o remetente da chave e o guardado). Levanta ComentarioIlegivel."""
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    if len(iv or b"") != TAMANHO_DO_IV or not carga or not segredo:
        raise ComentarioIlegivel("formato")
    vistas = set()
    for original in remetentes_originais:
        for comentarista in remetentes_do_comentario:
            par = (sem_dispositivo(original), sem_dispositivo(comentarista))
            if not all(par) or par in vistas:
                continue
            vistas.add(par)
            chave = chave_do_comentario(segredo, id_original, *par)
            try:
                return AESGCM(chave).decrypt(bytes(iv), bytes(carga), None)
            except InvalidTag:
                continue
    raise ComentarioIlegivel("tag")


def remetente_original_da_chave(chave_de_mim: bool, participante: str, remetente_do_comentario: str):
    """Quem escreveu a mensagem comentada, como o whatsmeow o deduz da chave
    (getOrigSenderFromKey, caso de grupo): `fromMe` na chave quer dizer que o
    autor da mensagem e quem comentou; senao e o `participant` da chave, que
    tem de ser uma pessoa. '' se nao der para saber."""
    if chave_de_mim:
        return sem_dispositivo(remetente_do_comentario)
    participante = sem_dispositivo(participante)
    if participante.partition("@")[2] in SERVIDORES_DE_PESSOA:
        return participante
    return ""


# ---------------------------------------------------------------- segredo
_CONSULTA_COM_MAPA = """
SELECT key, sender_jid FROM whatsmeow_message_secrets
WHERE message_id=?3 AND (chat_jid=?1 OR chat_jid=(
    CASE
        WHEN ?1 LIKE '%@lid'
            THEN (SELECT pn || '@s.whatsapp.net' FROM whatsmeow_lid_map WHERE lid=replace(?1, '@lid', ''))
        WHEN ?1 LIKE '%@s.whatsapp.net'
            THEN (SELECT lid || '@lid' FROM whatsmeow_lid_map WHERE pn=replace(?1, '@s.whatsapp.net', ''))
    END
)) AND (sender_jid=?2 OR sender_jid=(
    CASE
        WHEN ?2 LIKE '%@lid'
            THEN (SELECT pn || '@s.whatsapp.net' FROM whatsmeow_lid_map WHERE lid=replace(?2, '@lid', ''))
        WHEN ?2 LIKE '%@s.whatsapp.net'
            THEN (SELECT lid || '@lid' FROM whatsmeow_lid_map WHERE pn=replace(?2, '@s.whatsapp.net', ''))
    END
)) LIMIT 1
"""
# a mesma consulta sem o mapa numero<->LID (um sessao.db sem a tabela)
_CONSULTA_SIMPLES = """
SELECT key, sender_jid FROM whatsmeow_message_secrets
WHERE message_id=?3 AND chat_jid=?1 AND sender_jid=?2 LIMIT 1
"""


def segredo_da_mensagem(caminho: str, chat: str, remetente: str, id_msg: str):
    """(messageSecret, remetente guardado) de uma mensagem, lido do sessao.db do
    whatsmeow (`GetMessageSecret`: casa o chat e o remetente tambem pela forma
    LID/numero, e devolve o remetente como foi guardado), ou None. Somente
    leitura: a ponte nunca escreve nesse banco."""
    if not (caminho and chat and remetente and id_msg and os.path.exists(caminho)):
        return None
    uri = pathlib.Path(caminho).resolve().as_uri() + "?mode=ro"
    parametros = (sem_dispositivo(chat), sem_dispositivo(remetente), id_msg)
    try:
        con = sqlite3.connect(uri, uri=True, timeout=3)
    except sqlite3.Error:
        log.warning("comentarios: o banco do whatsmeow nao abriu")
        return None
    try:
        try:
            linha = con.execute(_CONSULTA_COM_MAPA, parametros).fetchone()
        except sqlite3.OperationalError:
            linha = con.execute(_CONSULTA_SIMPLES, parametros).fetchone()
    except sqlite3.Error:
        log.warning("comentarios: o segredo da mensagem nao pode ser lido")
        return None
    finally:
        con.close()
    if not linha or not linha[0]:
        return None
    return bytes(linha[0]), str(linha[1] or "")


# ------------------------------------------------- conteudo e formatos
def texto_do_comentario(mensagem) -> str:
    """O texto de um comentario (um waE2E.Message). Comentario sem texto
    (midia sem legenda) volta vazio."""
    interna = msgmap.desembrulhar(mensagem)
    if interna.conversation:
        return interna.conversation
    if interna.HasField("extendedTextMessage"):
        return interna.extendedTextMessage.text
    for campo in msgmap.MIDIAS:
        if interna.HasField(campo) and hasattr(getattr(interna, campo), "caption"):
            return getattr(interna, campo).caption
    return ""


def serial_da_mensagem(chat: str, id_msg: str, de_mim: bool, autor_wpp: str) -> str:
    """'fromMe_chat_ID[_autor]': o id serializado do WPPConnect numa mensagem de grupo."""
    serial = f"{'true' if de_mim else 'false'}_{chat}_{id_msg}"
    if not de_mim and autor_wpp:
        serial += f"_{autor_wpp}"
    return serial


def comentario_para_wpp(linha: dict, serial_pai: str, nome: str = "") -> dict:
    """Uma linha guardada -> um item de /message-comments (core/community_comments.py)."""
    tipo = linha["type"]
    return {"id": linha["id"], "parentMsgId": serial_pai, "chatId": linha["chat"],
            "author": linha["author"], "authorName": nome,
            "fromMe": bool(linha["from_me"]), "timestamp": int(linha["ts"]),
            "type": tipo, "body": linha["body"] if tipo == TIPO_COMENTARIO else ""}


def evento_de_contagem(sessao: str, serial_pai: str, chat: str, quantidade: int) -> dict:
    return {"session": sessao, "id": serial_pai, "chatId": chat, "replyCount": int(quantidade)}


def evento_de_previa(sessao: str, chat: str, linha) -> dict:
    """`kind: preview`: o comentario que a lista de conversas mostra, ou nenhum."""
    previa = None
    if linha is not None:
        previa = {"type": "comment",
                  "msgKey": serial_da_mensagem(chat, linha["id"], bool(linha["from_me"]),
                                               linha["author"]),
                  "sender": linha["author"], "timestamp": int(linha["ts"])}
    return {"session": sessao, "kind": "preview", "chatId": chat, "preview": previa}


def mensagem_de_comentario(chat: str, de_mim_pai: bool, remetente_pai: str, id_pai: str,
                           carga: bytes, iv: bytes):
    """A `waE2E.Message` com a EncCommentMessage a enviar (EncryptComment)."""
    from neonize.proto.waCommon.WACommon_pb2 import MessageKey
    from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import EncCommentMessage, Message
    return Message(encCommentMessage=EncCommentMessage(
        targetMessageKey=MessageKey(remoteJID=chat, participant=sem_dispositivo(remetente_pai),
                                    fromMe=de_mim_pai, ID=id_pai),
        encPayload=carga, encIV=iv))


def plano_de_texto(texto: str) -> bytes:
    """O comentario de texto serializado (o que vai dentro da cifra)."""
    from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import Message
    return Message(conversation=texto).SerializeToString()


# --------------------------------------------------------------- recepcao
def jid_texto(jid) -> str:
    if jid is None or not getattr(jid, "User", ""):
        return ""
    return f"{jid.User}@{jid.Server}"


def serial_do_pai(s, chat: str, id_pai: str, de_mim: bool, autor_wpp: str) -> str:
    """O id serializado da mensagem comentada: o da mensagem guardada, que e
    o que o cliente tem, ou o deduzido da chave."""
    guardada = s.bd.mensagem_wpp(id_pai)
    if guardada is not None:
        return guardada["id"]["_serialized"]
    return serial_da_mensagem(chat, id_pai, de_mim, autor_wpp)


def _usuarios_proprios(s) -> set:
    return {x.partition("@")[0].split(":")[0] for x in (s.eu, s.lid) if x} - {""}


def anunciar(s, chat: str, id_pai: str, serial_pai: str):
    """Avisa o app da nova contagem de respostas e da previa da conversa."""
    s.emitir("announcement-replies", evento_de_contagem(
        s.nome, serial_pai, chat, s.bd.contar_comentarios(chat, id_pai)))
    s.emitir("announcement-replies", evento_de_previa(
        s.nome, chat, s.bd.ultimo_comentario(chat)))


def registrar_comentario_enviado(s, chat, id_pai, serial_pai, id_comentario, texto):
    """Guarda o comentario que esta conta acabou de enviar (o servidor nao o
    devolve a este aparelho) e avisa o app."""
    if not id_comentario:
        return
    meu = msgmap.jid_wpp(s.lid or s.eu)
    s.bd.guardar_comentario(id_comentario, id_pai, chat, meu, int(time.time()),
                            TIPO_COMENTARIO, texto, True, serial_pai)
    anunciar(s, chat, id_pai, serial_pai)


def _abrir(s, chat, enc, remetente, de_mim):
    """(tipo, texto) do comentario: abre com o segredo da mensagem comentada, ou
    'ciphertext' se nao der."""
    chave = enc.targetMessageKey
    original = remetente_original_da_chave(bool(chave.fromMe), chave.participant, remetente)
    if not original:
        return TIPO_ILEGIVEL, ""
    achado = segredo_da_mensagem(os.path.join(s.pasta, ARQUIVO_DA_SESSAO),
                                 chat, original, chave.ID)
    if not achado:
        return TIPO_ILEGIVEL, ""
    segredo, guardado = achado
    originais = [original] + ([guardado] if guardado else [])
    comentaristas = [remetente]
    if de_mim:      # a conta aparece como numero ou como LID, conforme o aparelho
        comentaristas += [x for x in (s.lid, s.eu) if x]
    try:
        aberto = decifrar_comentario(segredo, chave.ID, originais, comentaristas,
                                     bytes(enc.encIV), bytes(enc.encPayload))
        from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import Message
        return TIPO_COMENTARIO, texto_do_comentario(Message.FromString(aberto))
    except Exception as erro:
        log.info("comentario que nao consegui abrir (%s)", type(erro).__name__)
        return TIPO_ILEGIVEL, ""


def receber(s, chat: str, o, e) -> bool:
    """True se a mensagem era um comentario de aviso (guardado e anunciado) ou o
    aviso de que um comentario foi apagado. Chamado pelo manipulador de
    mensagens, como o dos votos de enquete."""
    interna = msgmap.desembrulhar(e.Message)
    if interna.HasField("encCommentMessage"):
        return _comentario_recebido(s, chat, o, e, interna.encCommentMessage)
    if interna.HasField("protocolMessage"):
        p = interna.protocolMessage
        if p.type == 0 and p.key.ID:    # REVOKE
            return _comentario_apagado(s, chat, p.key.ID, o)
    return False


def _comentario_recebido(s, chat, o, e, enc) -> bool:
    chave = enc.targetMessageKey
    if not chat.endswith("@g.us") or not chave.ID:
        return True      # um comentario so existe em grupo; nao e uma mensagem a mostrar
    remetente = jid_texto(o.Sender)
    de_mim = bool(o.IsFromMe)
    tipo, texto = _abrir(s, chat, enc, remetente, de_mim)
    autor = msgmap.jid_wpp(remetente) or (msgmap.jid_wpp(s.eu) if de_mim else "")
    autor_pai = msgmap.jid_wpp(chave.participant)
    pai_e_meu = (autor_pai.partition("@")[0] in _usuarios_proprios(s)) \
        or (bool(chave.fromMe) and de_mim)
    serial_pai = serial_do_pai(s, chat, chave.ID, pai_e_meu, autor_pai)
    s.bd.guardar_comentario(e.Info.ID, chave.ID, chat, autor, msgmap.segundos(e.Info.Timestamp),
                            tipo, texto, de_mim, serial_pai)
    if e.Info.Pushname and not de_mim and remetente:
        s.bd.contato(remetente, pushname=e.Info.Pushname)
    anunciar(s, chat, chave.ID, serial_pai)
    return True


def _comentario_apagado(s, chat, id_comentario, o) -> bool:
    guardado = s.bd.autor_do_comentario(chat, id_comentario)
    if guardado is None:
        return False       # apagou outra coisa: segue o caminho das mensagens
    autor, do_proprio_aparelho = guardado
    if not ((o.IsFromMe and do_proprio_aparelho)
            or recibos_de_grupo.chaves_de(jid_texto(o.Sender)) & recibos_de_grupo.chaves_de(autor)):
        # so o autor apaga o proprio comentario (a remocao por administrador de
        # grupo nao e tratada: o comentario segue visivel); a mensagem e consumida
        log.info("aviso de apagar comentario de outra pessoa ignorado")
        return True
    achado = s.bd.revogar_comentario(chat, id_comentario)
    if achado is None:
        return False       # apagou outra coisa: segue o caminho das mensagens
    anunciar(s, chat, *achado)
    return True
