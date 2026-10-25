"""Rotas de comentarios dos avisos de comunidade: GET e POST de
/message-comments/:messageId. Os formatos sao os que o cliente do WinZapp le
(core/community_comments.py; o servidor de referencia e
communityCommentsController.ts). A cifra, o segredo e o recebimento ficam em
comentarios.py.

    GET  -> 200 {status:'success', response:[{id, parentMsgId, chatId, author,
            authorName, fromMe, timestamp, type, body}]}
            400 not_community_announcement | 503 comments_unavailable
    POST {text} -> 201 {status:'success', response:{messageSendResult:'OK'}};
            qualquer outra resposta o cliente trata como "nao confirmado"
            (502 comment_unconfirmed quando o envio pode ter saido).

So grupos que o WhatsApp diz serem o grupo de avisos de uma comunidade
(`IsDefaultSubGroup`) tem comentarios; os outros respondem 400.

ENVIO NAO VALIDADO: a `EncCommentMessage` sai por `send_message` do neonize e o
whatsmeow classifica o stanza como `type="text"` (getTypeFromMessage, sem caso
para comentario cifrado); o WhatsApp Web usa outro tipo. Se o servidor recusar ou
descartar o stanza, nada acontece alem de o comentario nao chegar. Isso so se
prova com uma conta de verdade.
"""
import asyncio
import logging
import os
import time

from aiohttp import web

import comentarios as com
import msgmap

log = logging.getLogger("ponte")

#: quanto tempo a ponte confia no que o WhatsApp disse sobre um grupo ser de avisos
VALIDADE_DO_GRUPO = 600


def partes_do_serial(serial: str):
    """'fromMe_grupo_ID[_autor]' -> (de_mim, grupo, ID, autor whatsmeow ou '') ou
    None se nao for o id de uma mensagem de grupo."""
    partes = str(serial or "").split("_")
    if len(partes) < 3 or partes[0] not in ("true", "false") or not partes[2] \
            or not partes[1].endswith("@g.us"):
        return None
    autor = msgmap.jid_whatsmeow(partes[3]) if len(partes) > 3 and partes[3] else ""
    return partes[0] == "true", partes[1], partes[2], autor


def de_outra_conversa(serial: str) -> bool:
    """O id e de uma mensagem (fromMe_conversa_ID...), mas de uma conversa que
    nao e grupo."""
    partes = str(serial or "").split("_")
    return (len(partes) >= 3 and partes[0] in ("true", "false") and bool(partes[2])
            and "@" in partes[1] and not partes[1].endswith("@g.us"))


def e_aviso_de_comunidade(info) -> bool:
    """`GroupInfo` do neonize -> o grupo e o de avisos de uma comunidade."""
    return bool(info.GroupIsDefaultSub.IsDefaultSubGroup)


def registrar(r, *, sessao, ok, jid_de_texto):
    """Liga as rotas de comentarios ao roteador `r` do aiohttp."""

    async def executar(funcao, *args):
        return await asyncio.get_running_loop().run_in_executor(None, funcao, *args)

    def erro(codigo, status, **extra):
        return web.json_response({"status": "error", "code": codigo, **extra}, status=status)

    def indisponivel():
        return erro("comments_unavailable", 503)

    async def aviso_conhecido(s, grupo):
        """True/False se o WhatsApp disse que o grupo e (ou nao e) o de avisos de
        uma comunidade; None se nao deu para saber. Guarda a resposta um tempo."""
        cache = s.__dict__.setdefault("_avisos_de_comunidade", {})
        guardado = cache.get(grupo)
        if guardado and guardado[1] > time.monotonic():
            return guardado[0]
        try:
            info = await executar(s.cliente.get_group_info, jid_de_texto(grupo))
            valor = e_aviso_de_comunidade(info)
        except Exception as e:
            log.warning("message-comments: dados do grupo indisponiveis (%s)", type(e).__name__)
            return None
        cache[grupo] = (valor, time.monotonic() + VALIDADE_DO_GRUPO)
        return valor

    async def verificar(s, grupo, id_pai):
        """None se o grupo pode ter comentarios; senao a resposta de erro."""
        if s.bd.comentarios_de(grupo, id_pai):      # ja chegou comentario: e um aviso
            return None
        conhecido = await aviso_conhecido(s, grupo)
        if conhecido is None:
            return indisponivel()
        if not conhecido:
            return erro("not_community_announcement", 400)
        return None

    def pronta(s):
        return s.cliente is not None and s.status == "CONNECTED"

    def nome_de(s, autor_wpp):
        achado = s.bd.contato_de(msgmap.jid_whatsmeow(autor_wpp)) if autor_wpp else {}
        return achado.get("name") or achado.get("pushname") or ""

    async def get_comments(req):
        s = sessao(req)
        serial = req.match_info["messageId"]
        partes = partes_do_serial(serial)
        if partes is None:
            # o id de uma mensagem de conversa que nao e grupo nunca tem comentarios
            return erro("not_community_announcement" if de_outra_conversa(serial)
                        else "invalid_comment_request", 400)
        _, grupo, id_pai, _ = partes
        if not pronta(s):
            return indisponivel()
        recusa = await verificar(s, grupo, id_pai)
        if recusa is not None:
            return recusa
        linhas = s.bd.comentarios_de(grupo, id_pai)
        return ok(status="success", response=[
            com.comentario_para_wpp(x, serial, nome_de(s, x["author"])) for x in linhas])

    def preparar_envio(s, grupo, id_pai, de_mim_pai, autor_pai, texto):
        """Cifra o comentario (bloqueia: le o banco do whatsmeow). Devolve a
        mensagem a enviar ou o codigo de por que nao da."""
        proprios = [x for x in (s.lid, s.eu) if x]
        caminho = os.path.join(s.pasta, com.ARQUIVO_DA_SESSAO)
        achado = None
        for quem in (proprios if de_mim_pai else [autor_pai]):
            achado = com.segredo_da_mensagem(caminho, grupo, quem, id_pai)
            if achado:
                break
        if not achado or not proprios:
            return None, "comment_secret_unavailable"
        segredo, remetente_guardado = achado
        remetente_pai = remetente_guardado if de_mim_pai and remetente_guardado else autor_pai
        if de_mim_pai and not remetente_pai:
            remetente_pai = proprios[0]
        # EncryptComment: cifra como a conta propria (LID) e com o remetente da
        # mensagem como o banco o guardou
        carga, iv = com.cifrar_comentario(
            segredo, id_pai, remetente_guardado or remetente_pai, proprios[0],
            com.plano_de_texto(texto))
        return com.mensagem_de_comentario(grupo, de_mim_pai, remetente_pai, id_pai,
                                          carga, iv), ""

    async def send_comment(req):
        s = sessao(req)
        serial = req.match_info["messageId"]
        try:
            corpo = await req.json()
        except Exception:
            corpo = None
        texto = corpo.get("text") if isinstance(corpo, dict) else None
        partes = partes_do_serial(serial)
        if partes is None or not isinstance(texto, str) or not texto.strip():
            return erro("invalid_comment_request", 400)
        de_mim_pai, grupo, id_pai, autor_pai = partes
        if not de_mim_pai and not autor_pai:
            return erro("invalid_comment_request", 400)
        if not pronta(s):
            return indisponivel()
        recusa = await verificar(s, grupo, id_pai)
        if recusa is not None:
            return recusa
        try:
            mensagem, motivo = await executar(
                preparar_envio, s, grupo, id_pai, de_mim_pai, autor_pai, texto)
        except Exception as e:
            log.warning("message-comments: nao cifrou (%s)", type(e).__name__)
            return erro("comment_unconfirmed", 502, retrySafe=False)
        if mensagem is None:        # nada foi enviado
            return erro(motivo, 503, retrySafe=True)
        try:
            resposta = await executar(s.cliente.send_message, jid_de_texto(grupo), mensagem)
        except Exception as e:
            log.warning("message-comments: envio sem confirmacao (%s)", type(e).__name__)
            return erro("comment_unconfirmed", 502, retrySafe=False)
        com.registrar_comentario_enviado(
            s, grupo, id_pai, serial, getattr(resposta, "ID", "") or "", texto)
        return web.json_response(
            {"status": "success", "response": {"messageSendResult": "OK"}}, status=201)

    r.add_get("/api/{tok}/message-comments/{messageId}", get_comments)
    r.add_post("/api/{tok}/message-comments/{messageId}", send_comment)
