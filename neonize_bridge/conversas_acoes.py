"""Rotas de acao sobre conversas e mensagens da ponte: limpar e apagar conversa
(uma ou todas), arquivar todas, marcar como nao lida, estado de digitando,
baixar midia e recibos de uma mensagem enviada.

Limpar e apagar conversa usam o aviso de estado do app (clearChat / deleteChat,
os mesmos indices e versao que o WhatsApp Web e o Baileys usam), como arquivar
e fixar ja fazem em server.py: o celular e os outros aparelhos acompanham. O
armazem da ponte so muda depois que o WhatsApp aceita o aviso.

Nada aqui foi testado contra uma conta real: o neonize nao instala fora do
Windows. Os formatos de resposta de clear-chat e message-ack sao os que o
cliente le (core/utils.py clear_chat_applied, core/message_ack.py); os das
demais rotas o cliente nao usa.

As funcoes puras ficam no topo (testadas em test_conversas_acoes.py);
`registrar` liga as rotas ao servidor.
"""
import base64
import logging
import time

import msgmap
import respostas
from conversas_listagem import booleano, jid_da_conversa

log = logging.getLogger("ponte")

#: etapa de recibo -> campo da resposta de message-ack
CAMPOS_DO_RECIBO = {"delivered": "deliveredAt", "read": "readAt", "played": "playedAt"}

#: valor de `chatstate` do WPPConnect -> (presenca, midia) do neonize
ESTADOS_DE_DIGITACAO = {
    0: ("CHAT_PRESENCE_COMPOSING", "CHAT_PRESENCE_MEDIA_TEXT"),    # digitando
    1: ("CHAT_PRESENCE_COMPOSING", "CHAT_PRESENCE_MEDIA_AUDIO"),   # gravando audio
    2: ("CHAT_PRESENCE_PAUSED", "CHAT_PRESENCE_MEDIA_TEXT"),       # parou
}


def manter_estrelas(corpo):
    """clear-chat: `keepStarred` ausente mantem as estreladas (como o
    WhatsApp faz por padrao)."""
    valor = corpo.get("keepStarred")
    return valor if isinstance(valor, bool) else booleano(valor, True)


def telefones(corpo):
    """`phone` como lista (o cliente manda uma lista; outros mandam texto)."""
    fone = corpo.get("phone")
    lista = fone if isinstance(fone, list) else [fone]
    lista = [str(f) for f in lista if f]
    if not lista:
        raise ValueError("phone is required")
    return lista


def indice_de_limpar(manter):
    """Resto do indice do aviso clearChat: [apagar estreladas, apagar midia]."""
    return ("0" if manter else "1", "1")


def estado_de_digitacao(valor):
    """chatstate (0, 1 ou 2, tambem como texto) -> (presenca, midia), ou ValueError."""
    try:
        return ESTADOS_DE_DIGITACAO[int(valor)]
    except (KeyError, TypeError, ValueError):
        raise ValueError("chatstate must be 0 (typing), 1 (recording) or 2 (paused)") from None


def estado_da_digitacao(ativo, gravando):
    """typing/recording (value, qual dos dois) -> o chatstate de ESTADOS_DE_DIGITACAO."""
    if not ativo:
        return 2
    return 1 if gravando else 0


def participantes_do_recibo(linhas):
    """[(quem, etapa, ts)] -> os `participants` de message-ack: um por pessoa,
    com deliveredAt/readAt/playedAt (segundos) das etapas que ela atingiu.
    Quem leu ou ouviu tambem recebeu: sem recibo de entrega, a entrega leva a
    hora da etapa seguinte."""
    por_pessoa = {}
    for quem, etapa, ts in linhas:
        campo = CAMPOS_DO_RECIBO.get(etapa)
        if campo is None:
            continue
        pessoa = por_pessoa.setdefault(quem, {"id": quem})
        if not pessoa.get(campo) or (ts and ts < pessoa[campo]):
            pessoa[campo] = int(ts or 0) or 1
    for pessoa in por_pessoa.values():
        if "deliveredAt" not in pessoa:
            seguinte = pessoa.get("readAt") or pessoa.get("playedAt")
            if seguinte:
                pessoa["deliveredAt"] = seguinte
    return list(por_pessoa.values())


def faixa_da_conversa(s, conversa):
    """A faixa de mensagens do aviso de estado: ate a mais recente guardada
    (o WhatsApp so aplica limpar/apagar ate essa mensagem)."""
    from neonize.proto.waCommon.WACommon_pb2 import MessageKey
    from neonize.proto.waSyncAction.WAWebProtobufSyncAction_pb2 import (
        SyncActionMessage, SyncActionMessageRange)
    ultima = s.bd.ultima_bruta(conversa)
    faixa = SyncActionMessageRange()
    if ultima is None:
        faixa.lastMessageTimestamp = int(time.time())
        return faixa
    faixa.lastMessageTimestamp = int(ultima["t"])
    chave = MessageKey(remoteJID=conversa, fromMe=bool(ultima.get("fromMe")),
                       ID=ultima["id"]["id"])
    if conversa.endswith("@g.us") and not ultima.get("fromMe") and ultima.get("author"):
        chave.participant = msgmap.jid_whatsmeow(ultima["author"])
    faixa.messages.append(SyncActionMessage(key=chave, timestamp=int(ultima["t"])))
    return faixa


def limpar_no_celular(s, conversa, manter):
    """Manda o aviso clearChat; limpa o armazem da ponte se o WhatsApp aceitou."""
    from neonize.proto.Neonize_pb2 import PatchInfo
    from neonize.proto.waSyncAction.WAWebProtobufSyncAction_pb2 import (
        ClearChatAction, SyncActionValue)
    aceito = s.enviar_estado(
        "clearChat", 6, PatchInfo.REGULAR_HIGH,
        SyncActionValue(clearChatAction=ClearChatAction(messageRange=faixa_da_conversa(s, conversa))),
        conversa, extra=indice_de_limpar(manter))
    if aceito:
        s.bd.limpar_conversa(conversa, manter_estrelas=manter)
    return aceito


def apagar_no_celular(s, conversa):
    """Manda o aviso deleteChat; tira a conversa do armazem se o WhatsApp aceitou."""
    from neonize.proto.Neonize_pb2 import PatchInfo
    from neonize.proto.waSyncAction.WAWebProtobufSyncAction_pb2 import (
        DeleteChatAction, SyncActionValue)
    aceito = s.enviar_estado(
        "deleteChat", 6, PatchInfo.REGULAR_HIGH,
        SyncActionValue(deleteChatAction=DeleteChatAction(messageRange=faixa_da_conversa(s, conversa))),
        conversa, extra=("1",))
    if aceito:
        s.bd.apagar_conversa(conversa)
    return aceito


def registrar(r, *, sessao, ok, erro, em_thread):
    """Liga as rotas de acao sobre conversas ao roteador `r` do aiohttp."""

    def conectada(s):
        return s.cliente is not None and s.status == "CONNECTED"

    async def corpo_de(req):
        try:
            corpo = await req.json()
            return corpo if isinstance(corpo, dict) else {}, None
        except ValueError:
            return None, erro("invalid JSON body", 400)

    async def por_telefone(req, nome, acao, eco=lambda corpo: {}, espelho=None):
        """`acao(s, jid, corpo)` -> aceito?, para cada telefone do corpo. Responde
        {data: {telefone: True}} como o clear-chat do WPPConnect, mais o que
        `eco(corpo)` devolver. A conversa guardada sob os dois enderecos (numero
        e LID) recebe a acao no primeiro e `espelho(s, outra_chave, corpo)` nos
        demais, depois que o WhatsApp aceitou."""
        s = sessao(req)
        corpo, falha = await corpo_de(req)
        if falha is not None:
            return falha
        try:
            fones = telefones(corpo)
        except ValueError as e:
            return erro(e, 400)
        if not conectada(s):
            return respostas.desconectada()
        grupo = booleano(corpo.get("isGroup"), False)
        resultado = {}
        for fone in fones:
            try:
                jid = jid_da_conversa(fone, grupo)
                chaves = await em_thread(s.chaves_da_conversa, jid)
                alvo = chaves[0] if chaves else jid
                resultado[fone] = bool(await em_thread(acao, s, alvo, corpo))
                if resultado[fone] and espelho is not None:
                    for outra in chaves[1:]:
                        espelho(s, outra, corpo)
            except Exception:
                log.exception(nome)
                resultado[fone] = False
        if not all(resultado.values()):
            return erro("nao aceito", 500)
        return ok(status="success", response={"data": resultado, **eco(corpo)})

    async def clear_chat(req):
        # o cliente confere o `keepStarred` que o servidor aplicou
        return await por_telefone(
            req, "clear-chat",
            lambda s, jid, corpo: limpar_no_celular(s, jid, manter_estrelas(corpo)),
            eco=lambda corpo: {"keepStarred": manter_estrelas(corpo)},
            espelho=lambda s, outra, corpo: s.bd.limpar_conversa(
                outra, manter_estrelas=manter_estrelas(corpo)))

    async def delete_chat(req):
        return await por_telefone(
            req, "delete-chat", lambda s, jid, corpo: apagar_no_celular(s, jid),
            espelho=lambda s, outra, corpo: s.bd.apagar_conversa(outra))

    async def em_todas(req, nome, acao, so_se=lambda c, corpo: True):
        """Aplica `acao(s, jid, corpo)` a cada conversa do armazem; responde
        quantas existiam e quantas o WhatsApp aceitou."""
        s = sessao(req)
        corpo, falha = await corpo_de(req)
        if falha is not None:
            return falha
        if not conectada(s):
            return respostas.desconectada()
        alvos = [c["jid"] for c in s.bd.conversas()
                 if c["jid"] != "status@broadcast" and so_se(c, corpo)]
        feitas = 0
        for jid in alvos:
            try:
                feitas += bool(await em_thread(acao, s, jid, corpo))
            except Exception:
                log.exception(nome)
        return ok(status="success", response={"total": len(alvos), "done": feitas})

    async def clear_all_chats(req):
        return await em_todas(
            req, "clear-all-chats",
            lambda s, jid, corpo: limpar_no_celular(s, jid, manter_estrelas(corpo)))

    async def delete_all_chats(req):
        return await em_todas(
            req, "delete-all-chats", lambda s, jid, corpo: apagar_no_celular(s, jid))

    async def archive_all_chats(req):
        return await em_todas(
            req, "archive-all-chats",
            lambda s, jid, corpo: s.arquivar(jid, booleano(corpo.get("value"), True)),
            so_se=lambda c, corpo: bool(c["archived"]) != booleano(corpo.get("value"), True))

    async def mark_unseen(req):
        s = sessao(req)
        corpo, falha = await corpo_de(req)
        if falha is not None:
            return falha
        try:
            fones = telefones(corpo)
        except ValueError as e:
            return erro(e, 400)
        if not conectada(s):
            return respostas.desconectada()
        grupo = booleano(corpo.get("isGroup"), False)
        for fone in fones:
            jid = jid_da_conversa(fone, grupo)
            await em_thread(s.marcar_nao_lida, await em_thread(s.chat_existente, jid) or jid)
        return ok(status="success", response={"unseen": True})

    async def avisar_presenca(s, corpo, fones, estado, nome):
        """Manda a presenca (digitando, gravando, parou) de `estado` a cada telefone."""
        from neonize.utils import build_jid
        from neonize.utils.enum import ChatPresence, ChatPresenceMedia
        presenca, midia = estado_de_digitacao(estado)
        grupo = booleano(corpo.get("isGroup"), False)
        for fone in fones:
            u, _, srv = jid_da_conversa(fone, grupo).partition("@")
            try:
                await em_thread(s.cliente.send_chat_presence, build_jid(u, srv),
                                ChatPresence[presenca], ChatPresenceMedia[midia])
            except Exception as e:
                log.exception(nome)
                return erro(e)
        return None

    async def chat_state(req):
        s = sessao(req)
        corpo, falha = await corpo_de(req)
        if falha is not None:
            return falha
        try:
            fones = telefones(corpo)
            estado_de_digitacao(corpo.get("chatstate"))
        except ValueError as e:
            return erro(e, 400)
        if not conectada(s):
            return respostas.desconectada()
        falhou = await avisar_presenca(s, corpo, fones, corpo.get("chatstate"), "chat-state")
        if falhou is not None:
            return falhou
        return ok(status="success", response={"chatstate": int(corpo["chatstate"])})

    async def digitando(req, nome, gravando):
        """typing e recording: o cliente manda {phone, value, isGroup}; value
        true avisa 'digitando' (ou 'gravando audio'), false 'parou'."""
        s = sessao(req)
        corpo, falha = await corpo_de(req)
        if falha is not None:
            return falha
        try:
            fones = telefones(corpo)
        except ValueError as e:
            return erro(e, 400)
        if not conectada(s):
            return respostas.desconectada()
        falhou = await avisar_presenca(
            s, corpo, fones, estado_da_digitacao(booleano(corpo.get("value"), True), gravando), nome)
        if falhou is not None:
            return falhou
        return ok(status="success", response={})

    async def typing(req):
        return await digitando(req, "typing", False)

    async def recording(req):
        return await digitando(req, "recording", True)

    async def download_media(req):
        """Baixa e decifra a midia de uma mensagem guardada; base64 no corpo da
        resposta, como get-media-by-message."""
        s = sessao(req)
        corpo, falha = await corpo_de(req)
        if falha is not None:
            return falha
        serial = str(corpo.get("messageId") or "")
        if not serial:
            return erro("messageId is required", 400)
        _, _, mid, _ = s.partes_do_id(serial)
        ficha = s.bd.ficha(mid)
        if ficha is None:
            return erro("media not found", 404)
        if not conectada(s):
            return respostas.desconectada()
        from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import Message
        try:
            dados = await em_thread(s.cliente.download_any, Message.FromString(ficha))
        except Exception as e:
            log.exception("download-media")
            texto = str(e).lower()
            # midia que saiu do servidor do WhatsApp: o app trata 410 como "expirada"
            return erro(e, 410 if ("404" in texto or "410" in texto or "not found" in texto) else 500)
        guardada = s.bd.mensagem_wpp(mid) or {}
        codificada = base64.b64encode(dados).decode()
        return ok(status="success", response={
            "base64": codificada, "mimetype": guardada.get("mimetype", "")})

    async def message_ack(req):
        """Quem recebeu, leu ou ouviu uma mensagem nossa. Responde 404 sem recibos
        guardados: o cliente entao usa o que ele mesmo registrou."""
        s = sessao(req)
        corpo, falha = await corpo_de(req)
        if falha is not None:
            return falha
        de_mim, chat, mid, _ = s.partes_do_id(str(corpo.get("messageId") or ""))
        linhas, esperados = (s.bd.recibos_de(mid) if de_mim else []), 0
        if de_mim and chat.endswith("@g.us"):
            # grupo: so os recibos de quem e destinatario, uma linha por pessoa
            resumo = await em_thread(s.resumo_de_recibos, chat, mid)
            if resumo is not None:
                linhas, esperados = resumo
        participantes = participantes_do_recibo(linhas)
        if not participantes:
            return erro("no receipts recorded for this message", 404)
        return ok(status="success", response={
            "participants": participantes,
            "deliveryRemaining": max(0, esperados - len(participantes))})

    p = "/api/{tok}/"
    r.add_post(p + "clear-chat", clear_chat)
    r.add_post(p + "delete-chat", delete_chat)
    r.add_post(p + "clear-all-chats", clear_all_chats)
    r.add_post(p + "delete-all-chats", delete_all_chats)
    r.add_post(p + "archive-all-chats", archive_all_chats)
    r.add_post(p + "mark-unseen", mark_unseen)
    r.add_post(p + "chat-state", chat_state)
    r.add_post(p + "typing", typing)
    r.add_post(p + "recording", recording)
    r.add_post(p + "download-media", download_media)
    r.add_post(p + "message-ack", message_ack)
