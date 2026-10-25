"""Servidor-ponte: fala a API do WPPConnect Server (HTTP + Socket.IO) e, por
tras, usa o neonize. Fase 0: login (QR e codigo), estado, host-device, enviar
texto e receber texto. O cliente do WinZapp aponta para ca pela opcao
"servidor personalizado" (wpp_custom_api).

Uso:  .venv\\Scripts\\python server.py [--porta 6300] [--pasta dados]
"""
import argparse
import asyncio
import base64
import json
import io
import logging
import os
import threading
import time

import segno
import socketio
from aiohttp import web

import canais
import administracao
import autenticacao
import chamadas
import ciclo_sessao
import entradas
import eventos_grupos
import eventos_contatos
import etiquetas
import comunidades
import contatos
import conversas
import conversas_alias
import conversas_restantes
import grupos
import grupos_extra
import historico_progresso
import msgmap
import pareamento
import negocio
import perfil
import presenca
import regras_de_mensagem
import respostas
import restantes
from redacao import redigir
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


def consertar_decifrar_voto(cliente):
    """O neonize 0.5.2 esqueceu `DecryptPollVote` na lista das funcoes cujo
    retorno (um ponteiro de bytes do Go) vira `bytes`: sem isso, decifrar um
    voto levanta TypeError. Poe o mesmo tratamento das outras. Roda uma vez."""
    import ctypes
    go = next((getattr(cliente, a) for a in dir(cliente) if a.endswith("__client")), None)
    funcao = getattr(go, "DecryptPollVote", None)
    if funcao is None or getattr(funcao, "errcheck", None) is not None:
        return False

    def consumir(resultado, func, argumentos):
        if not resultado:
            return b""
        try:
            conteudo = resultado.contents
            if not conteudo.ptr or conteudo.size == 0:
                return b""
            return ctypes.string_at(conteudo.ptr, conteudo.size)
        finally:
            go.FreeBytesStruct(resultado)
    funcao.errcheck = consumir
    return True


def jid_texto(jid) -> str:
    """JID do neonize -> 'usuario@servidor' (vazio se nao ha)."""
    if jid is None or not getattr(jid, "User", ""):
        return ""
    return f"{jid.User}@{jid.Server}"


class Sessao(conversas_alias.AliasDeConversa):
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
        self.ciclo = ciclo_sessao.Ciclo()   # estado + geracao (ciclo_sessao.py)
        self._trava_do_ciclo = threading.RLock()    # iniciar/parar um de cada vez
        self.ao_desvincular = None  # chamado quando o celular remove o aparelho
        self.urlcode = None
        self.eu = ""                # JID proprio (usuario@s.whatsapp.net)
        self.lid = ""
        os.makedirs(pasta, exist_ok=True)
        self.bd = Armazem(os.path.join(pasta, "ponte.db"))
        feitas = self.bd.aplicar_revogacoes()
        if feitas:
            log.info("%d mensagem(ns) apagada(s) para todos aplicada(s) ao armazenamento", feitas)

    @property
    def status(self):
        """CLOSED INITIALIZING QRCODE CONNECTED notLogged"""
        return self.ciclo.status

    @status.setter
    def status(self, valor):
        self.ciclo.status = valor

    # ------------------------------------------------------------- reacoes
    def serial_do_alvo(self, chat_w, grupo, alvo_id, reagiu_eu, key_de_mim, participante):
        """Id serializado (formato WPPConnect) da mensagem alvo de uma reacao.
        Usa a mensagem guardada; senao deduz: key.fromMe vem de quem reagiu."""
        guardada = self.bd.mensagem_wpp(alvo_id)
        if guardada is not None:
            return guardada["id"]["_serialized"]
        minha = key_de_mim if reagiu_eu else not key_de_mim
        if grupo:
            minha = bool(participante) and participante == msgmap.jid_wpp(self.eu)
        serial = f"{'true' if minha else 'false'}_{chat_w}_{alvo_id}"
        if grupo and not minha and participante:
            serial += f"_{participante}"
        return serial

    def _reacao_recebida(self, chat, o, e):
        """True se a mensagem era uma reacao (guarda e avisa o app)."""
        dados = msgmap.reacao(e.Message)
        if dados is None:
            return False
        alvo_id, emoji, key_de_mim, participante = dados
        chat_w = msgmap.jid_wpp(chat)
        grupo = chat_w.endswith("@g.us")
        reagiu_eu = bool(o.IsFromMe)
        remetente_w = msgmap.jid_wpp(jid_texto(o.Sender))
        quem = "eu" if reagiu_eu else remetente_w
        ts = msgmap.segundos(e.Info.Timestamp)
        self.bd.reagir(alvo_id, quem, emoji, ts)
        serial_reacao = f"{'true' if reagiu_eu else 'false'}_{chat_w}_{e.Info.ID}"
        if grupo:
            serial_reacao += f"_{remetente_w}"
        self.emitir("onreactionmessage", {"session": self.nome, "response": {
            "id": serial_reacao,
            "msgId": self.serial_do_alvo(chat_w, grupo, alvo_id, reagiu_eu,
                                         key_de_mim, participante),
            "reactionText": emoji, "text": emoji, "timestamp": ts}})
        return True

    # --------------------------------------------------------------- status
    def status_wpp(self):
        """Resposta de GET /statuses: os status das ultimas 24 h guardados,
        os meus e os dos contatos agrupados por quem publicou."""
        limite = time.time() - 86400
        meus, por_autor = [], {}
        for w in self.bd.mensagens("status@broadcast", 1000):
            if w["t"] < limite or w.get("type") in ("revoked", "protocol"):
                continue
            if w.get("fromMe"):
                meus.append(w)
                continue
            autor = w.get("author") or w.get("from")
            if autor and autor != "status@broadcast":
                por_autor.setdefault(autor, []).append(w)
        # myStatusReady diz ao cliente que uma lista vazia e a palavra final do
        # WhatsApp (ele apaga as historias em cache que nao estao nela). O
        # armazem so tem o que a ponte viu: vazio nao prova que nao ha historias.
        return {"myStatus": meus, "myStatusReady": bool(meus),
                "contacts": [{"jid": a, "msgs": ms} for a, ms in por_autor.items()]}

    def apagar_status(self, id_status):
        """Apaga um status meu para todos."""
        if self.cliente is None or self.status != "CONNECTED":
            return False
        from neonize.utils import build_jid
        eu_u, _, eu_srv = self.eu.partition("@")
        try:
            self.cliente.revoke_message(build_jid("status", "broadcast"),
                                        build_jid(eu_u.split(":")[0], eu_srv), id_status)
        except Exception:
            log.exception("apagar status")
            return False
        self.bd.mudar_mensagem(id_status, type="revoked", body="")
        return True

    # -------------------------------------------------------------- fixadas
    def _fixacao_recebida(self, chat, e):
        """True se a mensagem era o aviso de fixar ou soltar uma mensagem."""
        dados = msgmap.fixacao(e.Message)
        if dados is None:
            return False
        alvo, fixar, duracao = dados
        self.bd.fixar_mensagem(alvo, chat, fixar, msgmap.segundos(e.Info.Timestamp), duracao)
        return True

    def fixar_mensagem(self, serial, fixar, duracao=7 * 86400):
        """Fixa ou solta uma mensagem na conversa (visivel a todos)."""
        de_mim, conversa, mid, autor = self.partes_do_id(serial)
        if self.cliente is None or self.status != "CONNECTED":
            return False
        from neonize.proto.waCommon.WACommon_pb2 import MessageKey
        from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import (
            Message, MessageContextInfo, PinInChatMessage)
        from neonize.utils import build_jid
        chave = MessageKey(remoteJID=conversa, fromMe=de_mim, ID=mid)
        if conversa.endswith("@g.us") and not de_mim and autor:
            chave.participant = autor
        mensagem = Message(pinInChatMessage=PinInChatMessage(
            key=chave,
            type=PinInChatMessage.PIN_FOR_ALL if fixar else PinInChatMessage.UNPIN_FOR_ALL,
            senderTimestampMS=int(time.time() * 1000)))
        if fixar:
            mensagem.messageContextInfo.CopyFrom(MessageContextInfo(
                messageAddOnDurationInSecs=duracao,
                messageAddOnExpiryType=MessageContextInfo.STATIC))
        u, _, srv = conversa.partition("@")
        try:
            self.cliente.send_message(build_jid(u, srv), mensagem)
        except Exception:
            log.exception("fixar mensagem")
            return False
        self.bd.fixar_mensagem(mid, conversa, fixar, int(time.time()), duracao if fixar else 0)
        return True

    # -------------------------------------------------------------- edicao
    def marcar_editada(self, alvo, chat_w, id_edicao, de_mim, **campos):
        """Poe o texto novo na mensagem guardada, com a marca de editada
        (latestEditMsgKey, que o app le), e devolve a mensagem."""
        self.bd.mudar_mensagem(
            alvo, **campos,
            latestEditMsgKey={"id": id_edicao, "remote": chat_w, "fromMe": bool(de_mim)},
            latestEditSenderTimestampMs=int(time.time() * 1000))
        return self.bd.mensagem_wpp(alvo)

    def _edicao_recebida(self, chat, o, e):
        """True se a mensagem era o aviso de que alguem editou uma mensagem."""
        dados = msgmap.edicao(e.Message)
        if dados is None:
            return False
        alvo, nova = dados
        original = self.bd.mensagem_wpp(alvo)
        if original is None:           # mensagem de antes da ponte: nada a trocar
            return True
        w = msgmap.montar(
            conversa=chat, de_mim=bool(original.get("fromMe")), id=alvo,
            remetente=self.eu if original.get("fromMe") else jid_texto(o.Sender),
            ts=original["t"], mensagem=nova)
        if w is None:
            return True
        campos = {"body": w.get("body", "")} if w.get("type") == "chat" \
            else {"caption": w.get("caption", "")}
        editada = self.marcar_editada(alvo, msgmap.jid_wpp(chat), e.Info.ID,
                                      o.IsFromMe, **campos)
        self.emitir("received-message", {"session": self.nome, "response": editada})
        return True

    def editar(self, serial, texto, mencoes=()):
        """Edita uma mensagem minha (o WhatsApp so aceita por ~15 minutos)."""
        de_mim, conversa, mid, _ = self.partes_do_id(serial)
        original = self.bd.mensagem_wpp(mid)
        if not de_mim or original is None or self.cliente is None \
                or self.status != "CONNECTED":
            return False
        from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import (
            ContextInfo, ExtendedTextMessage, Message)
        from neonize.utils import build_jid
        u, _, srv = conversa.partition("@")
        ficha = self.bd.ficha(mid)
        if ficha and original.get("type") != "chat":        # legenda de midia
            nova = Message.FromString(ficha)
            campo = msgmap.campo_de_midia(nova)
            if not campo:
                return False
            getattr(nova, campo).caption = texto
            campos = {"caption": texto}
        else:
            if mencoes:
                ctx = ContextInfo()
                ctx.mentionedJID.extend(msgmap.jid_whatsmeow(j) for j in mencoes)
                nova = Message(extendedTextMessage=ExtendedTextMessage(
                    text=texto, contextInfo=ctx))
            else:
                nova = Message(conversation=texto)
            campos = {"body": texto}
        try:
            self.cliente.edit_message(build_jid(u, srv), mid, nova)
        except Exception:
            log.exception("editar mensagem")
            return False
        self.marcar_editada(mid, msgmap.jid_wpp(conversa), mid, True, **campos)
        return True

    def mensagem_para_encaminhar(self, serial, manter_voz=False):
        """A mensagem guardada, pronta para ser enviada como encaminhada, ou
        None se o tipo ainda nao e suportado."""
        from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import (
            ContextInfo, ExtendedTextMessage, Message)
        _, _, mid, _ = self.partes_do_id(serial)
        original = self.bd.mensagem_wpp(mid)
        if original is None:
            return None
        ficha = self.bd.ficha(mid)
        if ficha:
            msg = Message.FromString(ficha)
            campo = msgmap.campo_de_midia(msg)
            if not campo:
                return None
            sub = getattr(msg, campo)
            if hasattr(sub, "caption") and original.get("caption"):
                sub.caption = original["caption"]
            if campo == "audioMessage" and not manter_voz:
                sub.PTT = False
            sub.contextInfo.isForwarded = True
            sub.contextInfo.forwardingScore = 1
            return msg
        if original.get("type") == "chat" and original.get("body"):
            return Message(extendedTextMessage=ExtendedTextMessage(
                text=original["body"],
                contextInfo=ContextInfo(isForwarded=True, forwardingScore=1)))
        return None

    # ------------------------------------------------------------ enquetes
    def _voto_recebido(self, o, e):
        """True se a mensagem era um voto em enquete (decifra e guarda). O
        voto vem cifrado com o segredo da enquete, que o neonize guarda quando
        a pergunta passa por ele: votos de enquetes anteriores nao se leem."""
        alvo = msgmap.voto(e.Message)
        if alvo is None:
            return False
        original = self.bd.mensagem_wpp(alvo) or {}
        nomes = [x["name"] for x in original.get("pollOptions") or []]
        try:
            consertar_decifrar_voto(self.cliente)
            dado = self.cliente.decrypt_poll_vote(e)
        except Exception as erro:
            log.info("voto que nao consegui decifrar (%s: %s)", type(erro).__name__,
                     str(erro)[:100])
            return True
        quem = "eu" if o.IsFromMe else msgmap.jid_wpp(jid_texto(o.Sender))
        self.bd.votar(alvo, quem, msgmap.indices_do_voto(nomes, dado.selectedOptions),
                      msgmap.segundos(e.Info.Timestamp))
        return True

    def enquete_wpp(self, serial):
        """A enquete e seus votos, ou None se a mensagem nao e uma enquete."""
        _, _, mid, _ = self.partes_do_id(serial)
        original = self.bd.mensagem_wpp(mid)
        if not original or original.get("type") != "poll_creation":
            return None
        opcoes = [{"name": x["name"], "localId": x.get("localId", i)}
                  for i, x in enumerate(original.get("pollOptions") or [])]
        return mid, opcoes, self.bd.votos_de(mid)

    def votos_wpp(self, serial):
        """Entrada de /poll-votes: {voters, options:[{name, localId, votes, mine}]}."""
        achada = self.enquete_wpp(serial)
        if achada is None:
            return None
        _, opcoes, votos = achada
        contagem = {}
        minhas = set()
        votantes = 0
        for quem, ids in votos:
            if not ids:
                continue
            votantes += 1
            for i in ids:
                contagem[i] = contagem.get(i, 0) + 1
                if quem == "eu":
                    minhas.add(i)
        return {"voters": votantes, "options": [
            {**op, "votes": contagem.get(op["localId"], 0),
             "mine": op["localId"] in minhas} for op in opcoes]}

    def votantes_wpp(self, serial):
        """Resposta de /poll-voters: quem votou em que."""
        achada = self.enquete_wpp(serial)
        if achada is None:
            return {"ok": False, "error": "poll_voters_unavailable"}
        _, opcoes, votos = achada
        eu = msgmap.jid_wpp(self.eu)
        votantes = []
        for quem, ids in votos:
            if not ids:
                continue
            sender = eu if quem == "eu" else quem
            nome = self.bd.contato_de(msgmap.jid_whatsmeow(sender)) if quem != "eu" else {}
            votantes.append({"sender": sender, "mine": quem == "eu",
                             "name": nome.get("name") or nome.get("pushname") or "",
                             "localIds": ids})
        return {"ok": True, "options": opcoes, "voters": votantes}

    def votar_em_enquete(self, serial, local_ids):
        """Vota (ou, sem ids, tira o voto) numa enquete."""
        achada = self.enquete_wpp(serial)
        if achada is None or self.cliente is None or self.status != "CONNECTED":
            return False
        mid, opcoes, _ = achada
        nomes_por_id = {op["localId"]: op["name"] for op in opcoes}
        if not all(i in nomes_por_id for i in local_ids):
            return False
        from neonize.proto.Neonize_pb2 import MessageInfo, MessageSource
        from neonize.utils import build_jid
        de_mim, conversa, _, autor = self.partes_do_id(serial)
        grupo = conversa.endswith("@g.us")
        dono = self.eu if de_mim else (autor or conversa)

        def jid(texto):
            u, _, srv = texto.partition("@")
            return build_jid(u.split(":")[0], srv)
        vazio = build_jid("", "")
        origem = MessageSource(
            Chat=jid(conversa), Sender=jid(dono), IsFromMe=de_mim, IsGroup=grupo,
            SenderAlt=vazio, RecipientAlt=vazio, BroadcastListOwner=vazio)
        info = MessageInfo(MessageSource=origem, ID=mid, ServerID=0, Type="", Pushname="",
                           Timestamp=0, Category="", Multicast=False, MediaType="", Edit="")
        try:
            self.cliente.send_message(jid(conversa), self.cliente.build_poll_vote(
                info, [nomes_por_id[i] for i in local_ids]))
        except Exception:
            log.exception("votar em enquete")
            return False
        self.bd.votar(mid, "eu", sorted(local_ids), int(time.time()))
        return True

    def reagir(self, serial, emoji):
        """Reage (ou, com emoji vazio, tira a reacao) a uma mensagem."""
        if self.cliente is None or self.status != "CONNECTED":
            return False
        from neonize.utils import build_jid
        de_mim, conversa, mid, autor = self.partes_do_id(serial)
        autor_jid = self.eu if de_mim else (autor or conversa)
        cu, _, csrv = conversa.partition("@")
        au, _, asrv = autor_jid.partition("@")
        try:
            msg = self.cliente.build_reaction(
                build_jid(cu, csrv), build_jid(au.split(":")[0], asrv), mid, emoji)
            self.cliente.send_message(build_jid(cu, csrv), msg)
        except Exception:
            log.exception("reagir")
            return False
        self.bd.reagir(mid, "eu", emoji, int(time.time()))
        return True

    def reacoes_wpp(self, serial):
        """Resposta de GET /reactions/<id>: agrupada por emoji, no formato do
        WPPConnect (senders com senderUserJid e reactionText)."""
        _, _, mid, _ = self.partes_do_id(serial)
        eu = msgmap.jid_wpp(self.eu)
        grupos, minha = {}, ""
        for quem, emoji in self.bd.reacoes_de(mid):
            jid = eu if quem == "eu" else quem
            if quem == "eu":
                minha = emoji
            grupos.setdefault(emoji, []).append(
                {"senderUserJid": jid, "reactionText": emoji})
        itens = [{"aggregateEmoji": emoji, "hasReactionByMe": any(
                      x["senderUserJid"] == eu for x in senders),
                  "senders": senders} for emoji, senders in grupos.items()]
        return {"reactionByMe": minha, "reactions": itens}

    # ------------------------------------------------------------ eventos
    def emitir(self, evento, dados):
        # so o socket desta sessao (a sala do connect); sem room, todos os
        # sockets conectados receberiam os eventos de todas as contas
        asyncio.run_coroutine_threadsafe(
            self.sio.emit(evento, dados, room=self.nome), self.laco)

    # --------------------------------------------------------- ciclo de vida
    def _thread_viva(self):
        return bool(self.thread and self.thread.is_alive())

    def iniciar(self, telefone=None):
        """Sobe a sessao. Bloqueia ate a thread antiga terminar (so quando ha
        uma): chame fora do laco do aiohttp. True se uma thread nova subiu."""
        with self._trava_do_ciclo:
            decisao = ciclo_sessao.decidir_inicio(
                self._thread_viva(), self.status, telefone)
            if decisao == ciclo_sessao.IGNORAR:
                return False
            if decisao == ciclo_sessao.REINICIAR:
                # pareamento parado, ou uma sessao fechada cuja thread ainda
                # termina: o connect() do neonize so volta depois do stop()
                self.parar()
                self.thread.join(ciclo_sessao.PRAZO_DA_THREAD_ANTIGA)
                if self._thread_viva():
                    log.warning("a thread antiga da sessao %s nao terminou; nao inicio outra",
                                redigir(self.nome))
                    return False
            geracao = self.ciclo.abrir()
            self.thread = threading.Thread(
                target=self._rodar, args=(telefone, geracao), daemon=True, name="neonize")
            self.thread.start()
            return True

    def _aplicar(self, geracao, evento):
        """Aplica um evento ao estado se `geracao` ainda e a vigente (None se nao)."""
        return self.ciclo.aplicar(geracao, evento)

    def _parar_em_segundo_plano(self, cliente):
        """stop() fora da thread do evento: o Go pode estar esperando este
        manipulador voltar, e o Stop espera o Go."""
        def parar():
            try:
                cliente.stop()
            except Exception:
                log.exception("stop")
        threading.Thread(target=parar, daemon=True, name="neonize-stop").start()

    def _ao_conectar(self, geracao, c, telefone):
        if not self.ciclo.vigente(geracao):
            return
        try:
            me = c.get_me()
            self.eu = jid_texto(me.JID)
            self.lid = jid_texto(me.LID)
        except Exception:
            log.exception("get_me")
        if self._aplicar(geracao, ciclo_sessao.CONECTOU) is None:
            return
        self.urlcode = None
        log.info("conectado")
        if not (telefone and self.simular_codigo):   # a simulacao avisa depois do codigo
            self.emitir("session-logged", {"session": self.nome, "status": True})
        threading.Thread(target=self._carregar_nomes, daemon=True).start()
        threading.Thread(target=self._conferir_estado, daemon=True).start()
        threading.Thread(target=self._conferir_sempre, daemon=True).start()
        threading.Thread(target=self._copia_sempre, daemon=True).start()

    def _ao_ser_desvinculada(self, geracao, c):
        """O celular removeu o aparelho (LoggedOutEv, ou ConnectFailure de logout)."""
        mudou = self._aplicar(geracao, ciclo_sessao.SAIU)
        if mudou is None or mudou[0] == ciclo_sessao.NOT_LOGGED:
            return          # evento de uma sessao antiga, ou o segundo aviso do mesmo logout
        log.info("conta desvinculada pelo celular")
        self.eu = self.lid = ""
        self.urlcode = None
        # sem stop() o Go reconecta sem credencial e fica gerando QR sem ninguem olhando
        self._parar_em_segundo_plano(c)
        if self.ao_desvincular is not None:
            self.ao_desvincular()
        for atraso, evento, dados in ciclo_sessao.avisos_de_logout(self.nome):
            if atraso:
                temporizador = threading.Timer(atraso, self.emitir, args=(evento, dados))
                temporizador.daemon = True
                temporizador.start()
            else:
                self.emitir(evento, dados)

    def _ao_cair(self, geracao, evento, o_que):
        """Queda que o whatsmeow tenta consertar sozinho: o cliente ganha a
        carencia dele (INITIALIZING) em vez de ler CONNECTED com o socket morto."""
        mudou = self._aplicar(geracao, evento)
        if mudou is not None and mudou[0] != mudou[1]:
            log.info("%s: %s -> %s", o_que, mudou[0], mudou[1])
        else:
            log.info(o_que)

    def _ao_falhar(self, geracao, c, motivo):
        """ConnectFailureEv: logout, falha terminal (banimento, versao antiga) ou passageira."""
        tipo = ciclo_sessao.classificar_falha(motivo)
        log.warning("falha de conexao do WhatsApp, motivo %s (%s)", motivo, tipo)
        if tipo == ciclo_sessao.SAIU:
            self._ao_ser_desvinculada(geracao, c)
        elif tipo == ciclo_sessao.FALHA_TERMINAL:
            self._ao_falha_terminal(geracao, c, f"falha de conexao, motivo {motivo}")
        else:
            self._ao_cair(geracao, ciclo_sessao.CAIU, "falha passageira do servidor")

    def _ao_falha_terminal(self, geracao, c, o_que):
        """Nao adianta tentar de novo (banimento, versao antiga, outro aparelho
        tomou a conexao): a sessao fecha em vez de ficar em INITIALIZING para sempre."""
        if self._aplicar(geracao, ciclo_sessao.FALHA_TERMINAL) is None:
            return
        log.error("sessao encerrada: %s", o_que)
        self._parar_em_segundo_plano(c)

    def _rodar(self, telefone, geracao):
        import neonize.events as nev
        from neonize.client import NewClient
        os.makedirs(self.pasta, exist_ok=True)
        # Um NewClient novo por inicio, nunca reaproveitado: iniciar() ja esperou
        # a thread anterior terminar (o connect() so volta depois do stop()).
        # Trocar `self.cliente` solta o antigo, e o neonize chama Stop(uuid) quando
        # um NewClient e coletado (weakref.finalize); por isso a troca acontece
        # aqui, antes do connect() novo, e nunca com a thread antiga ainda de pe.
        cli = self.cliente = NewClient(os.path.join(self.pasta, "sessao.db"),
                                       props=props_do_aparelho(HISTORICO_DIAS))
        chamadas.registrar_eventos(cli, self)
        presenca.registrar_eventos(cli, self)
        eventos_grupos.registrar_eventos(cli, self)
        eventos_contatos.registrar_eventos(cli, self)

        # Cada evento do ciclo de vida e registrado UMA vez (o neonize guarda um
        # manipulador por tipo; test_um_manipulador_por_evento.py).
        @cli.event(nev.ConnectedEv)
        def _conectou(c, e):
            self._ao_conectar(geracao, c, telefone)

        @cli.event(nev.LoggedOutEv)
        def _saiu(c, e):
            self._ao_ser_desvinculada(geracao, c)

        @cli.event(nev.DisconnectedEv)
        def _caiu(c, e):
            self._ao_cair(geracao, ciclo_sessao.CAIU, "desconectado")

        @cli.event(nev.ConnectFailureEv)
        def _falhou(c, e):
            self._ao_falhar(geracao, c, int(e.Reason))

        @cli.event(nev.ClientOutdatedEv)
        def _desatualizado(c, e):
            self._ao_falha_terminal(geracao, c, "versao do cliente recusada pelo WhatsApp")

        @cli.event(nev.TemporaryBanEv)
        def _banido(c, e):
            self._ao_falha_terminal(geracao, c, "banimento temporario")

        @cli.event(nev.StreamReplacedEv)
        def _substituido(c, e):
            self._ao_falha_terminal(geracao, c, "outro aparelho tomou a conexao")

        @cli.event(nev.StreamErrorEv)
        def _erro_de_fluxo(c, e):
            self._ao_cair(geracao, ciclo_sessao.CAIU, "erro de fluxo " + str(e.Code)[:40])

        @cli.event(nev.KeepAliveTimeoutEv)
        def _sem_resposta(c, e):
            self._ao_cair(geracao, ciclo_sessao.CAIU, "keep-alive sem resposta")

        @cli.event(nev.KeepAliveRestoredEv)
        def _respondeu(c, e):
            self._ao_cair(geracao, ciclo_sessao.VOLTOU, "keep-alive restaurado")

        @cli.event(nev.MessageEv)
        def _mensagem(c, e):
            try:
                o = e.Info.MessageSource
                chat = jid_texto(o.Chat)
                if self._reacao_recebida(chat, o, e):
                    return
                if self._voto_recebido(o, e):
                    return
                if self._edicao_recebida(chat, o, e):
                    return
                if self._fixacao_recebida(chat, e):
                    return
                w = msgmap.montar(
                    conversa=chat, de_mim=bool(o.IsFromMe), id=e.Info.ID,
                    remetente=jid_texto(o.Sender), ts=e.Info.Timestamp,
                    mensagem=e.Message, nome_de_quem=e.Info.Pushname)
                if w is None:
                    return
                if w.get("type") == "revoked" and w.get("protocolMessageKey"):
                    # apagada para todos por quem enviou: a original tambem vira apagada
                    self.bd.mudar_mensagem(w["protocolMessageKey"]["id"], type="revoked", body="")
                self.bd.mensagem(chat, w, msgmap.ficha_midia(e.Message))
                self.bd.ao_chegar(chat, w["t"], w["fromMe"])
                if e.Info.Pushname and not o.IsFromMe:
                    self.bd.contato(jid_texto(o.Sender), pushname=e.Info.Pushname)
                    eventos_contatos.anunciar_nome(c, self, o.Sender, e.Info.Pushname)
                self.emitir("received-message", {"session": self.nome, "response": w})
                # o WhatsApp Web marca como lida a conversa aberta com a janela em foco
                if not o.IsFromMe and self.online and self.aberta == chat:
                    threading.Thread(target=self.marcar_lidas, args=(chat,),
                                     daemon=True).start()
            except Exception:
                log.exception("mensagem recebida")

        @cli.event(nev.GroupInfoEv)
        def _grupo(c, e):
            try:
                if e.Name and e.Name.Name:
                    self.bd.conversa(jid_texto(e.JID), name=e.Name.Name)
            except Exception:
                log.exception("grupo mudou")
            eventos_grupos.grupo_mudou(c, self, e)    # groups.update (ver eventos_grupos)

        def chat_local(c, jid_obj):
            """O JID com que a conversa esta guardada (o numero ou o LID)."""
            jid = jid_texto(jid_obj)
            if not self.bd.existe_conversa(jid) and jid.endswith("@s.whatsapp.net"):
                try:
                    lid = jid_texto(c.get_lid_from_pn(jid_obj))
                    if lid and self.bd.existe_conversa(lid):
                        return lid
                except Exception:
                    pass
            return jid

        def avisar_lida(jid, antes, novo):
            chat_w = msgmap.jid_wpp(jid)
            self.emitir("chats-update", {"session": self.nome, "data": [{
                "remoteJid": chat_w, "id": chat_w, "unreadCount": novo,
                "previousUnreadCount": antes}]})

        def lida_em_outro_aparelho(c, e):
            """Recibo de leitura que vem de mim mesmo: li essas mensagens no celular."""
            jid = chat_local(c, e.MessageSource.Chat)
            antes = self.bd.nao_lidas(jid)
            ids = set(e.MessageIDs)
            ultima = self.bd.ultima_recebida(jid)
            novo = 0 if (ultima and ultima in ids) else max(0, antes - len(ids))
            if novo == antes:
                return
            self.bd.conversa(jid, unread=novo)
            log.info("lidas no celular: %d mensagem(ns), nao lidas %d -> %d", len(ids), antes, novo)
            avisar_lida(jid, antes, novo)

        def ouvida_em_outro_aparelho(c, e):
            """Ouvi uma voz recebida no celular: o app mostra 'Reproduzida' (onack 4)."""
            jid = chat_local(c, e.MessageSource.Chat)
            chat_w = msgmap.jid_wpp(jid)
            for mid in e.MessageIDs:
                self.bd.mudar_mensagem(mid, played=True)
                self.emitir("onack", {
                    "session": self.nome, "ack": 4, "to": chat_w, "from": chat_w,
                    "id": {"fromMe": False, "remote": chat_w, "id": mid,
                           "_serialized": f"false_{chat_w}_{mid}"}})
            log.info("%d voz(es) ouvida(s) no celular -> app", len(e.MessageIDs))

        @cli.event(nev.ChatMarkAsReadEv)
        def _lida_no_celular(c, e):
            """Conversa lida (ou marcada como nao lida) no celular: o app acompanha."""
            try:
                acao = e.Action
                jid = jid_texto(e.JID)
                if not self.bd.existe_conversa(jid) and jid.endswith("@s.whatsapp.net"):
                    try:        # a conversa pode estar guardada pelo endereco LID
                        lid = jid_texto(c.get_lid_from_pn(e.JID))
                        if lid and self.bd.existe_conversa(lid):
                            jid = lid
                    except Exception:
                        pass
                antes = self.bd.nao_lidas(jid)
                if acao.read:
                    ate = msgmap.segundos(acao.messageRange.lastMessageTimestamp)                         if acao.HasField("messageRange") else 0
                    ultima = self.bd.ultima_bruta(jid)
                    if ate and ultima is not None and ultima["t"] > ate + 5:
                        log.info("lida no celular, mas ha mensagem mais nova: mantida")
                        return
                    self.bd.conversa(jid, unread=0)
                    novo = 0
                else:
                    self.bd.conversa(jid, unread=max(antes, 1))
                    novo = -1
                log.info("conversa marcada como %s no celular -> app",
                         "lida" if acao.read else "nao lida")
                chat_w = msgmap.jid_wpp(jid)
                self.emitir("chats-update", {"session": self.nome, "data": [{
                    "remoteJid": chat_w, "id": chat_w, "unreadCount": novo,
                    "previousUnreadCount": antes}]})
            except Exception:
                log.exception("conversa lida no celular")

        @cli.event(nev.ChatArchiveEv)
        def _arquivada_no_celular(c, e):
            try:
                jid = chat_local(c, e.JID)
                arq = bool(e.Action.archived)
                self.bd.conversa(jid, archived=int(arq))
                chat_w = msgmap.jid_wpp(jid)
                log.info("conversa %s no celular -> app", "arquivada" if arq else "desarquivada")
                self.emitir("chats-update", {"session": self.nome, "data": [{
                    "remoteJid": chat_w, "id": chat_w, "archive": arq}]})
            except Exception:
                log.exception("arquivar no celular")

        @cli.event(nev.ChatPinEv)
        def _fixada_no_celular(c, e):
            try:
                jid = chat_local(c, e.JID)
                fix = bool(e.Action.pinned)
                self.bd.conversa(jid, pinned=int(time.time()) if fix else 0)
                chat_w = msgmap.jid_wpp(jid)
                log.info("conversa %s no celular -> app", "fixada" if fix else "desafixada")
                self.emitir("chats-update", {"session": self.nome, "data": [{
                    "remoteJid": chat_w, "id": chat_w,
                    "pin": int(time.time()) if fix else False}]})
            except Exception:
                log.exception("fixar no celular")

        @cli.event(nev.MessageStarEv)
        def _estrela_no_celular(c, e):
            try:
                self.bd.mudar_mensagem(e.MessageID, star=bool(e.Action.starred))
                log.info("mensagem %s no celular", "favoritada" if e.Action.starred else "desfavoritada")
            except Exception:
                log.exception("estrela no celular")

        @cli.event(nev.MessageDeleteForMeEv)
        def _apagada_no_celular(c, e):
            try:
                if e.FromFullSync:
                    return
                self.bd.apagar_mensagem(e.MessageID)
                log.info("mensagem apagada no celular")
            except Exception:
                log.exception("apagar no celular")

        @cli.event(nev.ChatLockEv)
        def _trancada_no_celular(c, e):
            try:
                jid = chat_local(c, e.JID)
                self.bd.conversa(jid, locked=int(bool(e.Action.locked)))
                log.info("conversa %s no celular", "trancada" if e.Action.locked else "destrancada")
            except Exception:
                log.exception("trancar no celular")

        @cli.event(nev.ChatMuteEv)
        def _silenciada_no_celular(c, e):
            try:
                jid = chat_local(c, e.JID)
                fim = 0
                if e.Action.muted:
                    fim = msgmap.segundos(e.Action.muteEndTimestamp)                         if e.Action.muteEndTimestamp > 0 else 2**31 - 1
                self.bd.conversa(jid, muted=int(fim))
                log.info("conversa %s no celular", "silenciada" if e.Action.muted else "com som de novo")
            except Exception:
                log.exception("silenciar no celular")

        @cli.event(nev.ReceiptEv)
        def _recibo(c, e):
            try:
                from neonize.proto.Neonize_pb2 import Receipt
                tipo = Receipt.DESCRIPTOR.fields_by_name["Type"].enum_type \
                    .values_by_number[e.Type].name
                ack = {"DELIVERED": 2, "READ": 3, "PLAYED": 4}.get(tipo)
                chat = msgmap.jid_wpp(jid_texto(e.MessageSource.Chat))
                meu = bool(e.MessageSource.IsFromMe)
                log.info("recibo %s%s (%d mensagem(ns)) em %s -> ack %s", tipo,
                         " [meu]" if meu else "", len(e.MessageIDs), redigir(chat), ack)
                # recibo de leitura que vem de mim: li no celular (ou em outro aparelho)
                if tipo == "READ_SELF" or (tipo == "READ" and meu):
                    lida_em_outro_aparelho(c, e)
                    return
                if tipo == "PLAYED_SELF" or (tipo == "PLAYED" and meu):
                    ouvida_em_outro_aparelho(c, e)
                    return
                if ack is None:
                    return
                if not meu:
                    try:
                        quem = msgmap.jid_wpp(jid_texto(e.MessageSource.Sender))
                        etapa = {"DELIVERED": "delivered", "READ": "read",
                                 "PLAYED": "played"}[tipo]
                        for mid in e.MessageIDs:
                            self.bd.recibo(mid, quem, etapa, msgmap.segundos(e.Timestamp))
                    except Exception:
                        log.exception("guardar recibo")
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
            if self._aplicar(geracao, ciclo_sessao.QR) is None:
                return
            self.urlcode = codigo
            self.emitir("qrCode", {"session": self.nome, "urlcode": codigo,
                                   "data": qr_data_url(codigo)})

        if telefone:
            def _codigo():
                from neonize.utils.enum import ClientName, ClientType
                # espera a conexao decidir: ja pareada (CONNECTED) nao precisa de codigo
                for _ in range(40):
                    if self.status in ("QRCODE", "CONNECTED") or not self.ciclo.vigente(geracao):
                        break
                    time.sleep(0.5)
                if not self.ciclo.vigente(geracao):
                    return                      # a sessao foi fechada ou reiniciada
                if self.status == "CONNECTED":
                    if self.simular_codigo:     # so no modo de teste (--sessao-unica)
                        self.emitir("phoneCode", {"session": self.nome,
                                                  "data": "TEST-0000"})
                        time.sleep(2)
                        self.emitir("session-logged", {"session": self.nome,
                                                       "status": True})
                    return
                info_do_erro = {"name": "LinkCodeError", "message": "falha ao gerar o codigo",
                                "rateLimited": False}
                for tentativa in range(5):
                    if not self.ciclo.vigente(geracao):
                        return
                    try:
                        cod = cli.PairPhone("".join(ch for ch in telefone if ch.isdigit()),
                                            True, ClientName.WINDOWS, ClientType.EDGE)
                        self.emitir("phoneCode", {"session": self.nome, "data": cod})
                        self._vigiar_codigo(geracao)
                        return
                    except Exception as erro:
                        log.warning("codigo de pareamento, tentativa %d: %s",
                                    tentativa + 1, type(erro).__name__)
                        info_do_erro = pareamento.erro_do_codigo(erro)
                        if info_do_erro["rateLimited"]:
                            break           # a quota e do numero: repetir a mantem viva
                        time.sleep(3)
                self.emitir("phoneCodeError", {"session": self.nome, **info_do_erro})
            threading.Thread(target=_codigo, daemon=True).start()

        try:
            cli.connect()      # bloqueia ate o stop()
        except Exception:
            log.exception("connect")
        finally:
            self._aplicar(geracao, ciclo_sessao.FIM_DA_THREAD)

    def _vigiar_codigo(self, geracao, vida=ciclo_sessao.VIDA_DO_CODIGO, passo=1.0):
        """Depois do phoneCode: se o codigo vence sem parear, avisa o cliente
        (LinkCodeExpired) para o usuario nao ficar com um codigo morto. Nao
        gera outro codigo: sem ninguem pedindo isso gasta a cota por numero
        (docs/traps/pairing-flow.md). Roda na thread do codigo."""
        fim = time.monotonic() + vida
        while time.monotonic() < fim:
            if not self.ciclo.vigente(geracao) or self.status != "QRCODE":
                return                  # pareou, fechou ou reiniciou: nada a avisar
            time.sleep(min(passo, max(0.0, fim - time.monotonic())))
        if self.ciclo.vigente(geracao) and self.status == "QRCODE":
            self.emitir("phoneCodeError", ciclo_sessao.aviso_de_codigo_expirado(self.nome))

    def _guardar_historico(self, dados):
        progresso = historico_progresso.progresso_de(self, time.monotonic())
        progresso.comecou(time.monotonic())
        percentual = None
        try:
            percentual = self._guardar_bloco_do_historico(dados)
        finally:
            progresso.terminou(time.monotonic(), percentual)

    def _guardar_bloco_do_historico(self, dados):
        """Guarda um bloco do historico; devolve o progresso (0-100) que ele traz."""
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
                # nao sei dizer a unidade (ordem ou hora de fixar); o app so olha
                # se e maior que zero. A ponte tambem nao impoe o limite de 3
                # conversas fixadas: o WhatsApp recusa a quarta
                campos["pinned"] = int(c.pinned)
            if c.HasField("muteEndTime"):
                campos["muted"] = int(c.muteEndTime)
            if c.HasField("locked"):
                campos["locked"] = int(bool(c.locked))
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
                    if m.starred:
                        w["star"] = True
                    linhas.append((jid, w, msgmap.ficha_midia(m.message)))
            self.bd.mensagens_em_lote(linhas)
            total += len(linhas)
        log.info("historico: %d conversas, %d mensagens (%s%%)",
                 len(dados.conversations), total, dados.progress)
        return dados.progress

    COLECOES = ("regular_high", "regular_low", "regular",
                "critical_unblock_low", "critical_block")

    def _conferir_estado(self):
        """Confere as colecoes do estado do app (lida/nao lida, fixar, silenciar...).
        Uma colecao fora de sincronia ("mismatching LTHash") faz as mudancas do
        celular pararem de chegar e as daqui serem recusadas."""
        time.sleep(5)       # deixa a sincronia inicial terminar
        for nome in self.COLECOES:
            self.reparar_colecao(nome, forcar=False)

    def _conferir_sempre(self):
        """De minuto em minuto busca as mudancas do estado do app que o celular
        fez e o aviso ao vivo deixou passar (ler uma conversa, por exemplo)."""
        if getattr(self, "_busca_ativa", False):     # um laco so, mesmo reconectando
            return
        self._busca_ativa = True
        try:
            while self.status == "CONNECTED":
                time.sleep(60)
                self.buscar_mudancas()
        finally:
            self._busca_ativa = False

    INTERVALO_DA_COPIA = 300        # segundos entre dois pedidos de copia ao celular

    def pedir_copia_do_estado(self, nomes=("regular_low", "regular_high")):
        """Pede ao celular a copia do estado. Mudancas que ele fez mas nao subiu
        como atualizacao (acontece com arquivar) so chegam assim. O iPhone so
        responde com o WhatsApp aberto na tela; sem resposta, tenta de novo
        na proxima rodada. Devolve quantas mudancas novas chegaram."""
        if self.cliente is None or self.status != "CONNECTED":
            return 0
        antes = self.bd.contagem_de_estados()
        for nome in nomes:
            try:
                self.cliente.request_app_state_recovery(nome)
            except Exception as erro:
                log.info("pedido de copia do estado %s: %s", nome, erro)
                continue
            for _ in range(8):          # espera ate ~40 s pela resposta
                time.sleep(5)
                try:
                    self.cliente.fetch_app_state(nome, full_sync=False)
                except Exception as erro:
                    if "LTHash" not in str(erro):
                        log.info("estado %s: %s", nome, erro)
                        break
        depois = self.bd.contagem_de_estados()
        log.info("copia do estado pedida: arquivadas %d -> %d, fixadas %d -> %d",
                 antes[0], depois[0], antes[1], depois[1])
        return abs(depois[0] - antes[0]) + abs(depois[1] - antes[1])

    def _copia_sempre(self):
        if getattr(self, "_copia_ativa", False):
            return
        self._copia_ativa = True
        try:
            while self.status == "CONNECTED":
                time.sleep(self.INTERVALO_DA_COPIA)
                self.pedir_copia_do_estado()
        finally:
            self._copia_ativa = False

    def conferir_ajustes(self):
        """Le do armazem do whatsmeow (que os avisos de estado do celular mantem em
        dia) se cada conversa esta arquivada, fixada e silenciada, e acompanha o
        que mudou, do mesmo jeito que os avisos ao vivo. Mudancas que o celular
        fez e nao chegaram como aviso (ou que chegaram antes de o app olhar)
        aparecem aqui."""
        c = self.cliente
        if c is None or self.status != "CONNECTED" or not hasattr(c, "chat_settings"):
            return 0
        from neonize.utils import build_jid
        mudou = 0
        for conv in self.bd.conversas():
            jid = conv["jid"]
            usuario, _, servidor = jid.partition("@")
            if servidor in ("broadcast", "newsletter") or not usuario:
                continue
            candidatos = [build_jid(usuario, servidor)]
            if servidor == "s.whatsapp.net":
                try:
                    lid = c.get_lid_from_pn(candidatos[0])
                    if lid.User:
                        candidatos.append(lid)
                except Exception:
                    pass
            dados = None
            for alvo in candidatos:
                try:
                    achado = c.chat_settings.get_chat_settings(alvo)
                except Exception:
                    continue
                if achado.Found:
                    dados = achado
                    break
            if dados is None:
                continue
            arquivada, fixada = bool(dados.Archived), bool(dados.Pinned)
            campos, avisos = {}, {}
            if arquivada != bool(conv["archived"]):
                campos["archived"] = int(arquivada)
                avisos["archive"] = arquivada
            if fixada != (conv["pinned"] > 0):
                campos["pinned"] = int(time.time()) if fixada else 0
                avisos["pin"] = campos["pinned"] if fixada else False
            fim = int(dados.MutedUntil) if dados.MutedUntil > 0 else 0
            if fim > 4e9:
                fim = 2**31 - 1
            if (fim > 0) != (conv["muted"] > 0):
                campos["muted"] = fim
            if not campos:
                continue
            self.bd.conversa(jid, **campos)
            mudou += 1
            if avisos:
                chat_w = msgmap.jid_wpp(jid)
                self.emitir("chats-update", {"session": self.nome, "data": [{
                    "remoteJid": chat_w, "id": chat_w, **avisos}]})
        if mudou:
            log.info("ajustes lidos do armazem: %d conversa(s) mudaram", mudou)
        return mudou

    def buscar_mudancas(self):
        """fetch_app_state de regular_low/high: os avisos novos viram eventos."""
        agora = time.monotonic()
        if agora - getattr(self, "_ultima_busca", 0) < 15 or self.status != "CONNECTED":
            return
        self._ultima_busca = agora
        try:
            self.conferir_ajustes()
        except Exception:
            log.exception("conferir ajustes")
        for nome in ("regular_low", "regular_high"):
            try:
                self.cliente.fetch_app_state(nome, full_sync=False)
            except Exception as erro:
                if "LTHash" in str(erro):
                    self.reparar_colecao(nome, forcar=False)
                else:
                    log.info("busca do estado %s: %s", nome, erro)

    def reparar_colecao(self, nome, forcar=True):
        c = self.cliente
        if c is None or self.status != "CONNECTED":
            return False
        try:
            c.fetch_app_state(nome, full_sync=forcar)
            log.info("estado %s conferido%s", nome, " (baixado de novo)" if forcar else "")
            return True
        except Exception as erro:
            if "LTHash" not in str(erro):
                log.warning("estado %s: %s", nome, erro)
                return False
            if not forcar:
                log.warning("estado %s fora de sincronia; baixando de novo", nome)
                return self.reparar_colecao(nome, forcar=True)
            return self._recuperar_colecao(nome)

    def _recuperar_colecao(self, nome):
        """Pede ao celular uma copia nova do estado (o iPhone so responde com o
        WhatsApp aberto na tela)."""
        log.warning("estado %s: pedindo ao celular uma copia nova", nome)
        try:
            self.cliente.request_app_state_recovery(nome)
        except Exception as erro:
            log.warning("nao consegui pedir a copia do estado: %s", erro)
            return False
        for _ in range(18):
            time.sleep(5)
            try:
                self.cliente.fetch_app_state(nome)
                log.info("estado %s recuperado com a copia do celular", nome)
                return True
            except Exception as erro:
                if "LTHash" not in str(erro):
                    log.warning("estado %s: %s", nome, erro)
                    return False
        log.warning("o celular nao respondeu ao pedido do estado %s: abra o WhatsApp no celular", nome)
        return False

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

    def estado_lida(self, conversa, lida):
        """Marca a conversa como lida (ou nao lida) no celular e nos outros
        aparelhos, pelo aviso de estado markChatAsRead."""
        from neonize.proto.Neonize_pb2 import MutationInfo, PatchInfo
        from neonize.proto.waCommon.WACommon_pb2 import MessageKey
        from neonize.proto.waSyncAction.WAWebProtobufSyncAction_pb2 import (
            MarkChatAsReadAction, SyncActionMessage, SyncActionMessageRange,
            SyncActionValue)
        ultima = self.bd.ultima_bruta(conversa)
        faixa = SyncActionMessageRange()
        if ultima is not None:
            faixa.lastMessageTimestamp = int(ultima["t"])
            chave = MessageKey(remoteJID=conversa, fromMe=bool(ultima.get("fromMe")),
                               ID=ultima["id"]["id"])
            if conversa.endswith("@g.us") and not ultima.get("fromMe") and ultima.get("author"):
                chave.participant = msgmap.jid_whatsmeow(ultima["author"])
            faixa.messages.append(SyncActionMessage(key=chave, timestamp=int(ultima["t"])))
        patch = PatchInfo(
            Timestamp=time.time_ns(), Type=PatchInfo.REGULAR_LOW,
            Mutations=[MutationInfo(
                Index=["markChatAsRead", conversa], Version=3,
                Value=SyncActionValue(markChatAsReadAction=MarkChatAsReadAction(
                    read=bool(lida), messageRange=faixa)))])
        try:
            try:
                self.cliente.send_app_state(patch)
            except Exception as erro:
                if "LTHash" not in str(erro) and "409" not in str(erro):
                    raise
                # colecao fora de sincronia: baixa de novo e tenta mais uma vez
                if not self.reparar_colecao("regular_low"):
                    raise
                self.cliente.send_app_state(patch)
            log.info("conversa marcada como %s no celular", "lida" if lida else "nao lida")
            return True
        except Exception:
            log.exception("aviso de estado (lida) nao aceito")
            return False

    def enviar_estado(self, indice, versao, tipo, valor, conversa, extra=()):
        """Aviso de estado generico (arquivar, fixar...): True se o WhatsApp aceitou."""
        from neonize.proto.Neonize_pb2 import MutationInfo, PatchInfo
        if self.cliente is None or self.status != "CONNECTED":
            return False
        patch = PatchInfo(Timestamp=time.time_ns(), Type=tipo, Mutations=[
            MutationInfo(Index=[indice, conversa, *extra], Version=versao, Value=valor)])
        colecao = {PatchInfo.REGULAR_LOW: "regular_low",
                   PatchInfo.REGULAR_HIGH: "regular_high"}.get(tipo, "regular_low")
        try:
            try:
                self.cliente.send_app_state(patch)
            except Exception as erro:
                if "LTHash" not in str(erro) and "409" not in str(erro):
                    raise
                if not self.reparar_colecao(colecao):
                    raise
                self.cliente.send_app_state(patch)
            return True
        except Exception:
            log.exception("aviso de estado %s nao aceito", indice)
            return False

    def arquivar(self, conversa, arquivar):
        from neonize.proto.Neonize_pb2 import PatchInfo
        from neonize.proto.waSyncAction.WAWebProtobufSyncAction_pb2 import (
            ArchiveChatAction, SyncActionMessageRange, SyncActionValue)
        ultima = self.bd.ultima_bruta(conversa)
        quando = int(ultima["t"]) if ultima else int(time.time())
        ok_ = self.enviar_estado(
            "archive", 3, PatchInfo.REGULAR_LOW,
            SyncActionValue(archiveChatAction=ArchiveChatAction(
                archived=bool(arquivar),
                messageRange=SyncActionMessageRange(lastMessageTimestamp=quando))),
            conversa)
        if ok_:
            self.bd.conversa(conversa, archived=int(bool(arquivar)))
        return ok_

    def fixar(self, conversa, fixar):
        from neonize.proto.Neonize_pb2 import PatchInfo
        from neonize.proto.waSyncAction.WAWebProtobufSyncAction_pb2 import (
            PinAction, SyncActionValue)
        ok_ = self.enviar_estado(
            "pin_v1", 5, PatchInfo.REGULAR_LOW,
            SyncActionValue(pinAction=PinAction(pinned=bool(fixar))), conversa)
        if ok_:
            self.bd.conversa(conversa, pinned=int(time.time()) if fixar else 0)
        return ok_

    @staticmethod
    def partes_do_id(serial):
        """'true_<conversa>_<ID>[_autor]' -> (de_mim, conversa whatsmeow, ID, autor whatsmeow ou '')."""
        p = serial.split("_")
        if len(p) < 3:
            return False, "", serial, ""
        return p[0] == "true", msgmap.jid_whatsmeow(p[1]), p[2], \
            msgmap.jid_whatsmeow(p[3]) if len(p) > 3 else ""

    def _indice_da_mensagem(self, serial):
        de_mim, conversa, mid, autor = self.partes_do_id(serial)
        participante = autor if (conversa.endswith("@g.us") and not de_mim and autor) else "0"
        return conversa, mid, ("1" if de_mim else "0", participante)

    def favoritar(self, serial, estrela):
        from neonize.proto.Neonize_pb2 import PatchInfo
        from neonize.proto.waSyncAction.WAWebProtobufSyncAction_pb2 import StarAction, SyncActionValue
        conversa, mid, (minha, participante) = self._indice_da_mensagem(serial)
        ok_ = self.enviar_estado(
            "star", 2, PatchInfo.REGULAR_HIGH,
            SyncActionValue(starAction=StarAction(starred=bool(estrela))),
            conversa, extra=(mid, minha, participante))
        if ok_:
            self.bd.mudar_mensagem(mid, star=bool(estrela))
        return ok_

    def silenciar(self, conversa, segundos):
        """segundos: 0 = tirar o silencio; -1 = para sempre."""
        from neonize.proto.Neonize_pb2 import PatchInfo
        from neonize.proto.waSyncAction.WAWebProtobufSyncAction_pb2 import MuteAction, SyncActionValue
        acao = MuteAction(muted=segundos != 0)
        if segundos != 0:
            acao.muteEndTimestamp = -1 if segundos < 0 else int((time.time() + segundos) * 1000)
        ok_ = self.enviar_estado("mute", 2, PatchInfo.REGULAR_HIGH,
                                 SyncActionValue(muteAction=acao), conversa)
        if ok_:
            fim = 0 if segundos == 0 else (2**31 - 1 if segundos < 0 else int(time.time() + segundos))
            self.bd.conversa(conversa, muted=fim)
        return ok_

    def apagar_para_mim(self, serial):
        from neonize.proto.Neonize_pb2 import PatchInfo
        from neonize.proto.waSyncAction.WAWebProtobufSyncAction_pb2 import (
            DeleteMessageForMeAction, SyncActionValue)
        conversa, mid, (minha, participante) = self._indice_da_mensagem(serial)
        original = self.bd.mensagem_wpp(mid) or {}
        ok_ = self.enviar_estado(
            "deleteMessageForMe", 3, PatchInfo.REGULAR_HIGH,
            SyncActionValue(deleteMessageForMeAction=DeleteMessageForMeAction(
                deleteMedia=True, messageTimestamp=int(original.get("t", time.time())))),
            conversa, extra=(mid, minha, participante))
        if ok_:
            self.bd.apagar_mensagem(mid)
        return ok_

    def apagar_para_todos(self, serial):
        de_mim, conversa, mid, autor = self.partes_do_id(serial)
        if self.cliente is None or self.status != "CONNECTED":
            return False
        quem = regras_de_mensagem.autor_da_revogacao(
            de_mim, conversa.endswith("@g.us"), self.eu, autor,
            self.bd.mensagem_wpp(mid) or {})
        if not quem:
            return False
        from neonize.utils import build_jid
        u, _, srv = conversa.partition("@")
        try:
            eu_u, _, eu_srv = quem.partition("@")
            self.cliente.revoke_message(build_jid(u, srv), build_jid(eu_u, eu_srv), mid)
        except Exception:
            log.exception("revogar")
            return False
        self.bd.mudar_mensagem(mid, type="revoked", body="")
        return True

    def marcar_ouvida(self, serial):
        from neonize.utils import build_jid
        from neonize.utils.enum import ReceiptType
        de_mim, conversa, mid, autor = self.partes_do_id(serial)
        if de_mim or self.cliente is None or self.status != "CONNECTED":
            return False
        original = self.bd.mensagem_wpp(mid) or {}
        quem = autor or msgmap.jid_whatsmeow(original.get("author") or original.get("from") or conversa)
        cu, _, csrv = conversa.partition("@")
        qu, _, qsrv = quem.partition("@")
        try:
            self.cliente.mark_read(mid, chat=build_jid(cu, csrv), sender=build_jid(qu, qsrv),
                                   receipt=ReceiptType.PLAYED)
        except Exception:
            log.exception("mark_played")
            return False
        self.bd.mudar_mensagem(mid, played=True)
        return True

    def marcar_nao_lida(self, conversa):
        self.bd.conversa(conversa, unread=1)
        if self.cliente is not None and self.status == "CONNECTED":
            self.estado_lida(conversa, False)

    def marcar_lidas(self, conversa, forcar=False):
        """Quantas mensagens receberam o recibo de leitura (ver ler_e_confirmar)."""
        return self.ler_e_confirmar(conversa, forcar)[0]

    def ler_e_confirmar(self, conversa, forcar=False):
        """Manda o recibo de leitura das mensagens recebidas e ainda nao lidas
        e marca a conversa como lida tambem no celular. Devolve (quantas, ok):
        ok e False se algum recibo nao saiu (send-seen so confirma o que saiu).
        Um aviso de estado recusado (a conversa continua nao lida no celular)
        fica no log: os recibos, que o contato ve, e que contam aqui."""
        if self.cliente is None or self.status != "CONNECTED":
            return 0, False
        tinha_nao_lidas = self.bd.nao_lidas(conversa) > 0
        a_ler = self.bd.recebidas_a_ler(conversa)
        if tinha_nao_lidas or forcar:     # forcar: o app pediu (o celular pode ainda ter o contador)
            self.estado_lida(conversa, True)
        if not a_ler:
            return 0, True
        from neonize.utils import build_jid
        from neonize.utils.enum import ReceiptType

        def jid_de(texto):
            u, _, srv = msgmap.jid_whatsmeow(texto).partition("@")
            return build_jid(u, srv)

        por_autor = {}
        for mid, autor, ts in a_ler:
            por_autor.setdefault(autor, []).append(mid)
        enviados = True
        for autor, ids in por_autor.items():
            try:
                self.cliente.mark_read(*ids, chat=jid_de(conversa), sender=jid_de(autor),
                                       receipt=ReceiptType.READ)
            except Exception:
                log.exception("mark_read")
                enviados = False
        log.info("lidas %d mensagem(ns) em %s", len(a_ler), redigir(conversa))
        return len(a_ler), enviados

    def parar(self, status=None):
        """Fecha a sessao: disconnect() fecha o socket e stop() cancela o
        contexto do Go, o unico jeito de o connect() da thread voltar. A thread
        termina sozinha; iniciar() espera por ela. Idempotente."""
        with self._trava_do_ciclo:
            self.ciclo.encerrar(status)
            c = self.cliente
            if c is not None:
                for acao in (c.disconnect, c.stop):
                    try:
                        acao()
                    except Exception:
                        log.exception("parar sessao")

    def sair(self):
        c = self.cliente
        if c is not None:
            try:
                c.logout()
            except Exception:
                log.exception("logout")
        with self._trava_do_ciclo:
            self.eu = self.lid = ""
            self.urlcode = None
            self.parar(ciclo_sessao.CLOSED)


# ---------------------------------------------------------------- HTTP
# Sem access_log: ele grava cada URL, e nela vao o segredo da ponte, o token de
# cada sessao e telefones (/api/<token>/get-messages/5511...@c.us). O ponte.log
# e o arquivo que o usuario manda num relatorio de erro.
OPCOES_DO_SERVIDOR = {"host": "127.0.0.1", "print": None, "access_log": None}


def criar_app(pasta, fixa=""):
    # Sem cors_allowed_origins="*": so a mesma origem (o cliente do WinZapp nao manda Origin).
    sio = socketio.AsyncServer(async_mode="aiohttp", logger=False, engineio_logger=False)
    segredo = autenticacao.carregar_segredo(pasta)
    app = web.Application(client_max_size=autenticacao.TAMANHO_MAXIMO_DO_CORPO,
                          middlewares=[autenticacao.criar_middleware(segredo)])
    sio.attach(app)
    app["sio"] = sio                        # os testes olham quem esta conectado
    estado = {"sessoes": {}, "fixa": fixa}
    arquivo_ativas = os.path.join(pasta, "ativas.json")

    def ler_ativas():
        try:
            with open(arquivo_ativas, encoding="utf-8") as f:
                return [str(n) for n in json.load(f)]
        except (OSError, ValueError):
            return []

    def marcar_ativa(nome, ativa):
        """Lembra quais contas estavam ligadas, para a ponte retomar sozinha
        quando reiniciar (o app so chama start-session ao abrir)."""
        lista = [n for n in ler_ativas() if n != nome]
        if ativa:
            lista.append(nome)
        os.makedirs(pasta, exist_ok=True)
        with open(arquivo_ativas, "w", encoding="utf-8") as f:
            json.dump(lista, f)

    def nova_sessao(nome):
        """A Sessao de `nome`, guardada no estado. Se o celular a desvincular,
        deixa de ser retomada quando a ponte reiniciar: sem credencial, ela so
        geraria QR sem ninguem olhando."""
        s = estado["sessoes"][nome] = Sessao(
            nome, os.path.join(pasta, nome), sio, asyncio.get_running_loop())
        s.ao_desvincular = lambda: marcar_ativa(nome, False)
        return s

    async def retomar(_app):
        if estado["fixa"]:
            return
        for nome in ler_ativas():
            if os.path.exists(os.path.join(pasta, nome, "sessao.db")):
                s = nova_sessao(nome)
                await asyncio.get_running_loop().run_in_executor(None, s.iniciar)
                log.info("sessao retomada ao iniciar: %s", redigir(nome))
    app.on_startup.append(retomar)

    def chave_da(req):
        return estado["fixa"] or req.match_info["tok"].split(":", 1)[0]

    def sessao(req):
        tok = req.match_info["tok"]
        nome = estado["fixa"] or tok.split(":", 1)[0]
        if not entradas.nome_de_sessao_valido(nome):
            raise web.HTTPBadRequest(reason="invalid session name")
        s = estado["sessoes"].get(nome)
        if s is None:
            s = nova_sessao(nome)
        if estado["fixa"]:      # os eventos levam o nome que o cliente usa
            s.nome = tok.split(":", 1)[0]
            s.simular_codigo = True
        return s

    def ok(**kw):
        return web.json_response(kw)

    @sio.event
    async def connect(sid, environ, auth):
        # Qualquer erro aqui recusa a conexao: o python-socketio ja registrou o
        # socket antes de chamar este manipulador, e uma excecao qualquer o
        # deixaria conectado sem credencial.
        try:
            token = auth.get("token") if isinstance(auth, dict) else None
            cred = token if isinstance(token, str) and token else environ.get("HTTP_APIKEY", "")
            if not autenticacao.credencial_valida(segredo, cred):
                raise socketio.exceptions.ConnectionRefusedError("sem credencial")
            # a sala e o nome do token ja conferido, nunca o `auth.session`, que o
            # cliente escreve sozinho; Sessao.nome e o mesmo nome (tambem em --sessao-unica)
            nome = autenticacao.nome_da_credencial(cred)
            await sio.enter_room(sid, nome)
        except socketio.exceptions.ConnectionRefusedError:
            raise
        except Exception:
            raise socketio.exceptions.ConnectionRefusedError("sem credencial")
        log.info("socket conectado (%s)", redigir(nome))

    async def gerar_token(req):
        nome = req.match_info["session"]
        if not entradas.nome_de_sessao_valido(nome):
            return web.json_response({"status": "error", "message": "invalid session name"},
                                     status=400)
        tok = autenticacao.token_da_sessao(segredo, nome)
        return web.json_response({"status": "success", "session": nome,
                                  "token": tok, "full": f"{nome}:{tok}"})

    async def start_session(req):
        s = sessao(req)
        try:
            corpo = await req.json()
        except Exception:
            corpo = {}
        # iniciar() espera a thread antiga terminar: nunca dentro do laco do aiohttp
        await asyncio.get_running_loop().run_in_executor(None, s.iniciar, corpo.get("phone"))
        marcar_ativa(chave_da(req), True)
        if s.status == "CONNECTED":     # ja logada: o cliente espera os avisos
            if corpo.get("phone") and s.simular_codigo:
                await sio.emit("phoneCode", {"session": s.nome, "data": "TEST-0000"},
                               room=s.nome)
                await asyncio.sleep(2)
            await sio.emit("session-logged", {"session": s.nome, "status": True},
                           room=s.nome)
        return ok(status=s.status, qrcode=None, urlcode=s.urlcode)

    async def status_session(req):
        s = sessao(req)
        return ok(status=s.status, qrcode=qr_data_url(s.urlcode) if s.urlcode else None,
                  urlcode=s.urlcode, session=s.nome)

    async def check_connection(req):
        s = sessao(req)
        conectado = False
        if s.status == "CONNECTED" and s.cliente is not None:
            # o estado em cache nao basta: apos uma queda de rede continua CONNECTED
            try:
                conectado = bool(await asyncio.get_running_loop().run_in_executor(
                    None, lambda: s.cliente.is_connected))
            except Exception:
                log.exception("check-connection-session")
        return ok(status=conectado, message="Connected" if conectado else "Disconnected")

    async def close_session(req):
        await asyncio.get_running_loop().run_in_executor(None, sessao(req).parar)
        marcar_ativa(chave_da(req), False)
        return ok(status=True, message="Session successfully closed")

    async def logout_session(req):
        await asyncio.get_running_loop().run_in_executor(None, sessao(req).sair)
        marcar_ativa(chave_da(req), False)
        return ok(status=True, message="Session successfully closed")

    async def host_device(req):
        s = sessao(req)
        http, corpo = ciclo_sessao.resposta_host_device(s.status, s.eu, s.lid)
        return web.json_response(corpo, status=http)

    async def vazio_ok(req):
        return ok(status="success", response={})

    def id_wpp(jid):
        usuario, _, servidor = msgmap.jid_wpp(jid).partition("@")
        return {"server": servidor, "user": usuario,
                "_serialized": f"{usuario}@{servidor}"}

    async def list_chats(req):
        s = sessao(req)
        threading.Thread(target=s.buscar_mudancas, daemon=True).start()
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
                "muteExpiration": c["muted"], "isLocked": bool(c.get("locked")), "msgs": None,
            })
        return web.json_response(saida)

    async def all_contacts(req):
        s = sessao(req)
        meus = {msgmap.jid_wpp(j) for j in (s.eu, s.lid) if j}
        return ok(status="success", response=[
            contatos.registro_da_agenda(c, id_wpp(c["jid"]), meus) for c in s.bd.contatos()])

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
        # a conversa pode estar guardada pelo numero ou pelo LID (ou pelos dois)
        chaves = await em_thread(s.chaves_da_conversa, jid) or [msgmap.jid_whatsmeow(jid)]
        itens = s.bd.mensagens(chaves, int(q.get("count", 20)), antes)
        return ok(status="success", response=s.bd.para_resposta(itens))

    async def send_message(req):
        s = sessao(req)
        corpo = await req.json()
        if s.status != "CONNECTED" or s.cliente is None:
            return respostas.desconectada()
        from neonize.utils import build_jid
        fone = corpo["phone"]
        if isinstance(fone, list):      # o WinZapp manda uma lista de destinos
            fone = fone[0]
        if "@" in fone:
            u, _, srv = msgmap.jid_whatsmeow(fone).partition("@")
            destino = build_jid(u, srv)
        else:
            destino = build_jid(fone)
        conteudo = corpo["message"]
        previa = msgmap.previa_de_link(corpo.get("options"))
        if previa:      # o cartao do link que o cliente ja montou
            from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import ExtendedTextMessage, Message
            conteudo = Message(extendedTextMessage=ExtendedTextMessage(text=conteudo, **previa))
        try:
            resp = await asyncio.get_running_loop().run_in_executor(
                None, lambda: s.cliente.send_message(destino, conteudo))
        except Exception as erro:
            log.exception("send-message")
            return web.json_response({"status": "error", "message": str(erro)}, status=500)
        return guardar_enviada(s, destino, resp)

    async def saude(req):
        # o cliente (wpp_update_health.wait_for_api) espera {"message": "OK"}, como o Node
        return web.json_response({"message": "OK"})

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
        achado = s.bd.contato_de(lid, *contatos.jids_equivalentes(fone))
        return web.json_response(contatos.resposta_pn_lid(fone, lid, achado))

    async def blocklist(req):
        """Os bloqueados, no formato do WPPConnect. Uma falha NAO vira lista vazia:
        o cliente troca a lista que guardou pela resposta e a grava."""
        s = sessao(req)
        if s.cliente is None or s.status != "CONNECTED":
            return web.json_response({"status": "error", "message": "Disconnected"}, status=503)

        def consultar():
            itens = []
            for j in s.cliente.get_blocklist().JIDs:
                fone = ""
                if j.Server == "lid":      # o cliente le `phone` como telefone
                    try:
                        fone = jid_texto(s.cliente.get_pn_from_lid(j))
                    except Exception:
                        fone = ""
                itens.append(contatos.entrada_de_bloqueio(jid_texto(j), fone))
            return itens
        try:
            itens = await asyncio.get_running_loop().run_in_executor(None, consultar)
        except Exception as erro:
            log.warning("blocklist: %s", type(erro).__name__)
            return web.json_response({"status": "error", "message": "blocklist unavailable"}, status=502)
        return ok(status="success", response=itens)

    async def history_status(req):
        """history-sync-status e unblock-history-sync: o historico so e dado por
        completo quando o progresso chega a 100 ou passa um tempo sem bloco
        novo (historico_progresso.py)."""
        s = sessao(req)
        contagens = s.bd.contagens()
        estado = historico_progresso.progresso_de(s, time.monotonic()).estado(
            time.monotonic(), contagens["chat"])
        return ok(status="success", response={
            "backendWorkerBridgeReady": True, **estado,
            "unprocessed": estado["unprocessedChunks"], "restarted": False, "removed": [],
            "onDemandEnabled": False, "chunkStatus": {}, "storeCounts": contagens})

    async def send_seen(req):
        s = sessao(req)
        corpo = await req.json()
        fone = corpo.get("phone")
        fone = fone[0] if isinstance(fone, list) else fone
        erro = sem_conexao(s)
        if erro is not None:
            return erro
        # o app so aceita a confirmacao neste formato: response.data = [true], e
        # so desfaz a leitura local quando ela falta. Ele tenta os enderecos da
        # conversa (numero, LID) um a um: o que a ponte nao conhece e [false]
        chaves = await em_thread(s.chaves_da_conversa, fone) if fone else []
        if not chaves:
            return ok(status="success", response={"data": [False]})
        if corpo.get("unread"):        # "marcar como nao lida"
            await em_thread(s.marcar_nao_lida, chaves[0])
            confirmado = True
        else:
            confirmado = True
            for chave in chaves:
                _, enviados = await em_thread(s.ler_e_confirmar, chave, True)
                confirmado = confirmado and enviados
        return ok(status="success", response={"data": [confirmado]})

    async def subscribe_presence(req):
        """Assina a presenca de uma pessoa. So confirma o que o WhatsApp aceitou: o
        cliente marca o alvo como assinado ao ver `success`. Nao grava `s.aberta`
        (o cliente assina muitos contatos, nao so a conversa aberta)."""
        s = sessao(req)
        corpo = await req.json()
        fone = corpo.get("phone")
        fone = fone[0] if isinstance(fone, list) else fone
        alvo = contatos.alvo_de_assinatura(fone)
        if not alvo:
            return web.json_response({"status": "error", "message": "invalid contact"}, status=400)
        if s.cliente is None or s.status != "CONNECTED":
            return web.json_response({"status": "error", "message": "Disconnected"}, status=503)
        from neonize.utils import build_jid
        usuario, _, servidor = alvo.partition("@")
        try:
            await asyncio.get_running_loop().run_in_executor(
                None, s.cliente.subscribe_presence, build_jid(usuario, servidor))
        except Exception as erro:
            log.info("subscribe_presence recusado (%s) para %s", type(erro).__name__, redigir(alvo))
            return web.json_response({"status": "error", "message": "subscribe refused"}, status=502)
        return ok(status="success", response={})

    async def set_online(req):
        s = sessao(req)
        corpo = await req.json()
        s.online = bool(corpo.get("isOnline"))
        if s.cliente is not None and s.status == "CONNECTED":
            from neonize.utils.enum import Presence
            presenca = Presence.AVAILABLE if s.online else Presence.UNAVAILABLE
            try:    # sem isto os contatos nunca veem a conta online/offline
                await asyncio.get_running_loop().run_in_executor(
                    None, s.cliente.send_presence, presenca)
            except Exception as erro:
                log.info("set-online-presence recusado (%s)", type(erro).__name__)
                return web.json_response({"status": "error", "message": "presence refused"}, status=502)
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
            return respostas.desconectada()
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
        serial = w["id"]["_serialized"] if w else f"true_{msgmap.jid_wpp(chat)}_{resp.ID}"
        chat_w = msgmap.jid_wpp(chat)
        # o WPPConnect avisa "enviada" (ack 1) logo depois do envio; o app so
        # registra a mensagem depois da resposta HTTP, por isso o pequeno atraso
        asyncio.get_running_loop().call_later(0.6, lambda: asyncio.ensure_future(
            sio.emit("onack", {"session": s.nome, "ack": 1, "to": chat_w, "from": chat_w,
                               "id": {"fromMe": True, "remote": chat_w, "id": resp.ID,
                                      "_serialized": serial}}, room=s.nome)))
        return ok(status="success", response=[{"id": serial, "ack": 1}])

    def contexto_citado(s, citado, ctx=None):
        """ContextInfo que cita a mensagem `citado` (id serializado), ou None se
        ela nao esta guardada."""
        from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import ContextInfo
        partes = citado.split("_")
        id_citado = partes[2] if len(partes) > 2 else partes[-1]
        original = s.bd.mensagem_wpp(id_citado)
        if original is None:
            return None
        ctx = ctx if ctx is not None else ContextInfo()
        ctx.stanzaID = id_citado
        ctx.participant = msgmap.participante_citado(original, s.eu)
        ctx.quotedMessage.CopyFrom(msgmap.citada(original))
        return ctx

    def enviar_midia(s, destino, tipo, arquivo, legenda, nome, mime, ptt, ctx=None):
        c = s.cliente
        if tipo == "audio":
            # o neonize detecta so "audio/ogg"; o WhatsApp quer "audio/ogg; codecs=opus"
            # (sem isso a voz pode nem aparecer em alguns aparelhos)
            m = c.build_audio_message(arquivo, ptt=ptt)
            if (m.audioMessage.mimetype or "").startswith("audio/ogg"):
                m.audioMessage.mimetype = "audio/ogg; codecs=opus"
            if ctx is not None:
                m.audioMessage.contextInfo.CopyFrom(ctx)
            return c.send_message(destino, m)
        if ctx is not None:     # com citacao: monta a mensagem, poe o contexto e envia
            if tipo == "image":
                m, campo = c.build_image_message(arquivo, caption=legenda or None), "imageMessage"
            elif tipo == "video":
                m, campo = c.build_video_message(arquivo, caption=legenda or None), "videoMessage"
            elif tipo == "audio":
                m, campo = c.build_audio_message(arquivo, ptt=ptt), "audioMessage"
            else:
                m, campo = c.build_document_message(
                    arquivo, caption=legenda or None, filename=nome or None,
                    mimetype=mime or None), "documentMessage"
            getattr(m, campo).contextInfo.CopyFrom(ctx)
            return c.send_message(destino, m)
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
            return respostas.desconectada()
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
            ctx = contexto_citado(s, um("quotedMessageId")) if um("quotedMessageId") else None
            if um("quotedMessageId") and ctx is None:
                log.info("mensagem citada nao guardada: envio recusado")
                return respostas.citacao_ausente()
            resp = await asyncio.get_running_loop().run_in_executor(
                None, enviar_midia, s, destino, um("type", "document"), caminho,
                um("caption"), um("filename"), mime, um("isPtt") == "true", ctx)
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
            return respostas.desconectada()
        dados = b64.b64decode(corpo["base64Ptt"].split(",", 1)[-1])
        destino = destino_de(corpo["phone"])
        ctx = contexto_citado(s, corpo["quotedMessageId"]) if corpo.get("quotedMessageId") else None
        try:
            resp = await asyncio.get_running_loop().run_in_executor(
                None, enviar_midia, s, destino, "audio", dados, "", "", "", True, ctx)
        except Exception as erro:
            log.exception("send-voice-base64")
            return web.json_response({"status": "error", "message": str(erro)}, status=500)
        return guardar_enviada(s, destino, resp)

    async def publicar_status(req, montar_e_enviar):
        """Publica um status: `montar_e_enviar(s, destino, corpo)` devolve o envio."""
        s = sessao(req)
        corpo = await req.json()
        erro = sem_conexao(s)
        if erro is not None:
            return erro
        destino = destino_de("status@broadcast")
        try:
            resp = await em_thread(montar_e_enviar, s, destino, corpo)
        except Exception as falha:
            log.exception("publicar status")
            return web.json_response({"status": "error", "message": str(falha)}, status=500)
        chat = jid_texto(destino)
        w = msgmap.montar(conversa=chat, de_mim=True, id=resp.ID, remetente=s.eu,
                          ts=resp.Timestamp or time.time(), mensagem=resp.Message)
        serial = f"true_status@broadcast_{resp.ID}"
        if w is not None:
            s.bd.mensagem(chat, w, msgmap.ficha_midia(resp.Message))
            serial = w["id"]["_serialized"]
        # o app confere sendMsgResult.messageSendResult de cada item
        return ok(status="success", response=[{
            "id": serial, "ack": 1, "sendMsgResult": {"messageSendResult": "OK"}}])

    def arquivo_do_corpo(corpo):
        """Bytes do campo path (base64, nunca um caminho do disco)."""
        return entradas.bytes_do_corpo(corpo)

    def status_de_texto(s, destino, corpo):
        from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import ExtendedTextMessage, Message
        opcoes = corpo.get("options") or {}
        return s.cliente.send_message(destino, Message(extendedTextMessage=ExtendedTextMessage(
            text=corpo["text"], textArgb=0xFFFFFFFF,
            backgroundArgb=msgmap.cor_argb(opcoes.get("backgroundColor")),
            font=int(opcoes.get("font") or 0))))

    def status_de_imagem(s, destino, corpo):
        return enviar_midia(s, destino, "image", arquivo_do_corpo(corpo),
                            corpo.get("caption") or "", "", "", False)

    def status_de_video(s, destino, corpo):
        return enviar_midia(s, destino, "video", arquivo_do_corpo(corpo),
                            corpo.get("caption") or "", "", "", False)

    def status_de_voz(s, destino, corpo):
        import base64 as b64
        dados = b64.b64decode(corpo["base64Ptt"].split(",", 1)[-1])
        return enviar_midia(s, destino, "audio", dados, "", "", "", True)

    async def send_text_storie(req):
        return await publicar_status(req, status_de_texto)

    async def send_image_storie(req):
        return await publicar_status(req, status_de_imagem)

    async def send_video_storie(req):
        return await publicar_status(req, status_de_video)

    async def send_status_voice(req):
        return await publicar_status(req, status_de_voz)

    async def statuses(req):
        return ok(status="success", response=sessao(req).status_wpp())

    async def delete_status(req):
        s = sessao(req)
        if not ciclo_sessao.status_apagavel(s.bd.mensagem_wpp(req.match_info["id"])):
            # expirado, ja apagado ou nunca meu: nao ha o que apagar (o cliente le o `code`)
            return web.json_response({"status": "error", "code": "not_found",
                                      "retrySafe": False}, status=404)
        erro = sem_conexao(s)
        if erro is not None:
            return erro
        feito = await em_thread(s.apagar_status, req.match_info["id"])
        if not feito:
            return web.json_response({"status": "error", "code": "status_delete_failed"},
                                     status=500)
        return ok(status="success")

    def jid_de_texto(texto):
        from neonize.utils import build_jid
        u, _, srv = msgmap.jid_whatsmeow(texto).partition("@")
        return build_jid(u, srv)

    def grupo_para_wpp(g):
        """GroupInfo do neonize -> o dicionario que o WinZapp le em group-info."""
        participantes = []
        for p in g.Participants:
            fone = jid_texto(p.PhoneNumber)
            jid = fone or jid_texto(p.JID) or jid_texto(p.LID)
            participantes.append({
                "id": jid, "lid": jid_texto(p.LID),
                "admin": "superadmin" if p.IsSuperAdmin else ("admin" if p.IsAdmin else None),
                "isAdmin": bool(p.IsAdmin or p.IsSuperAdmin),
                "isSuperAdmin": bool(p.IsSuperAdmin)})
        dono = jid_texto(g.OwnerPN) or jid_texto(g.OwnerJID)
        return {
            "id": jid_texto(g.JID), "name": g.GroupName.Name, "subject": g.GroupName.Name,
            "desc": g.GroupTopic.Topic, "announce": bool(g.GroupAnnounce.IsAnnounce),
            "restrict": bool(g.GroupLocked.isLocked), "owner": dono,
            "creation": msgmap.segundos(g.GroupCreated),
            "participants": participantes}

    async def group_info(req):
        s = sessao(req)
        if s.cliente is None or s.status != "CONNECTED":
            return web.json_response({"status": "error", "message": "Disconnected"}, status=400)
        jid = jid_de_texto(req.match_info["jid"])
        try:
            g = await asyncio.get_running_loop().run_in_executor(
                None, s.cliente.get_group_info, jid)
        except Exception as erro:
            log.info("group-info falhou: %s", type(erro).__name__)
            return web.json_response({"status": "error", "message": str(erro)}, status=404)
        info = grupo_para_wpp(g)
        if info["name"]:
            s.bd.conversa(info["id"], name=info["name"])
        return ok(status="success", response=info)

    async def acao_de_grupo(req, funcao):
        s = sessao(req)
        corpo = await req.json()
        if s.cliente is None or s.status != "CONNECTED":
            return web.json_response({"status": "error", "message": "Disconnected"}, status=400)
        try:
            await asyncio.get_running_loop().run_in_executor(
                None, funcao, s.cliente, jid_de_texto(corpo["groupId"]), corpo)
        except Exception as erro:
            log.exception("grupo")
            return web.json_response({"status": "error", "message": str(erro)}, status=500)
        return ok(status="success", response={})

    async def leave_group(req):
        return await acao_de_grupo(req, lambda c, jid, corpo: c.leave_group(jid))

    async def group_subject(req):
        return await acao_de_grupo(req, lambda c, jid, corpo: c.set_group_name(jid, corpo["title"]))

    async def contact_profile(req):
        """Perfil de um contato (nome, apelido, conta comercial e, para LID, o numero)."""
        s = sessao(req)
        jid = msgmap.jid_whatsmeow(req.match_info["jid"].replace("~", ""))
        usuario, _, servidor = jid.partition("@")
        lid = jid if servidor == "lid" else ""
        fone = "" if servidor == "lid" else jid
        verificado = ""
        if s.cliente is not None and s.status == "CONNECTED" and servidor in ("lid", "s.whatsapp.net"):
            from neonize.utils import build_jid
            laco = asyncio.get_running_loop()
            if lid:
                try:
                    pn = await laco.run_in_executor(
                        None, s.cliente.get_pn_from_lid, build_jid(usuario, "lid"))
                    fone = jid_texto(pn)
                except Exception:
                    fone = ""
            else:   # o apelido pode ter ficado guardado so sob o LID
                try:
                    lid = jid_texto(await laco.run_in_executor(
                        None, s.cliente.get_lid_from_pn, build_jid(usuario, servidor)))
                except Exception:
                    lid = ""
            if fone:    # nome verificado de conta comercial, da loja local do neonize
                try:
                    u, _, srv = fone.partition("@")
                    info = await laco.run_in_executor(
                        None, s.cliente.contact.get_contact, build_jid(u, srv))
                    verificado = (info.BusinessName or "").strip()
                except Exception:
                    verificado = ""
        achado = s.bd.contato_de(jid, lid, *contatos.jids_equivalentes(fone))
        return ok(status="success", response=contatos.corpo_do_contato(jid, fone, achado, verificado))

    r.add_get("/api/{tok}/contact/{jid}", contact_profile)
    r.add_get("/api/{tok}/group-info/{jid}", group_info)
    r.add_post("/api/{tok}/leave-group", leave_group)
    r.add_post("/api/{tok}/group-subject", group_subject)
    grupos.registrar(r, sessao=sessao, ok=ok, jid_de_texto=jid_de_texto,
                     jid_texto=jid_texto, qr_data_url=qr_data_url)
    negocio.registrar(r, sessao=sessao, ok=ok)
    canais.registrar(r, sessao=sessao, ok=ok, jid_de_texto=jid_de_texto,
                     jid_texto=jid_texto)
    administracao.registrar(r, sessao=sessao, ok=ok, estado=estado, pasta=pasta,
                            marcar_ativa=marcar_ativa, qr_data_url=qr_data_url)
    chamadas.registrar(r, sessao=sessao, ok=ok)
    presenca.registrar(r, sessao=sessao, ok=ok)
    etiquetas.registrar(r, sessao=sessao, ok=ok, jid_de_texto=jid_de_texto,
                        jid_texto=jid_texto)
    comunidades.registrar(r, sessao=sessao, ok=ok, jid_de_texto=jid_de_texto)
    grupos_extra.registrar(r, sessao=sessao, ok=ok, jid_de_texto=jid_de_texto,
                           jid_texto=jid_texto, qr_data_url=qr_data_url)
    perfil.registrar(r, sessao=sessao, ok=ok, jid_de_texto=jid_de_texto,
                     jid_texto=jid_texto)
    contatos.registrar(r, sessao=sessao, ok=ok, jid_de_texto=jid_de_texto,
                       jid_texto=jid_texto, destino_de=destino_de,
                       guardar_enviada=guardar_enviada, contexto_citado=contexto_citado)
    restantes.registrar(r, sessao=sessao, ok=ok)
    conversas.registrar(r, sessao=sessao, ok=ok, id_wpp=id_wpp, destino_de=destino_de,
                        guardar_enviada=guardar_enviada, enviar_midia=enviar_midia,
                        contexto_citado=contexto_citado)
    conversas_restantes.registrar(r, sessao=sessao, ok=ok, destino_de=destino_de,
                                  contexto_citado=contexto_citado,
                                  publicar_status=publicar_status,
                                  status_de_texto=status_de_texto)
    async def texto_com_contexto(req, com_mencoes):
        """send-reply e send-mentioned: texto citando uma mensagem e/ou mencionando gente."""
        from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import (
            ContextInfo, ExtendedTextMessage, Message)
        s = sessao(req)
        corpo = await req.json()
        if s.status != "CONNECTED" or s.cliente is None:
            return respostas.desconectada()
        destino = destino_de(corpo["phone"])
        ctx = ContextInfo()
        citado = corpo.get("messageId")
        if citado and contexto_citado(s, citado, ctx) is None:
            return respostas.citacao_ausente()
        if com_mencoes:
            ctx.mentionedJID.extend(msgmap.jid_whatsmeow(j) for j in corpo.get("mentioned", []))
        mensagem = Message(extendedTextMessage=ExtendedTextMessage(
            text=corpo["message"], contextInfo=ctx,
            **(msgmap.previa_de_link(corpo.get("options")) or {})))
        try:
            resp = await asyncio.get_running_loop().run_in_executor(
                None, s.cliente.send_message, destino, mensagem)
        except Exception as erro:
            log.exception("send-reply")
            return web.json_response({"status": "error", "message": str(erro)}, status=500)
        return guardar_enviada(s, destino, resp)

    async def send_reply(req):
        return await texto_com_contexto(req, False)

    async def send_mentioned(req):
        return await texto_com_contexto(req, True)

    r.add_post("/api/{tok}/send-text-storie", send_text_storie)
    r.add_post("/api/{tok}/send-image-storie", send_image_storie)
    r.add_post("/api/{tok}/send-video-storie", send_video_storie)
    r.add_post("/api/{tok}/send-status-voice-base64", send_status_voice)
    r.add_get("/api/{tok}/statuses", statuses)
    r.add_post("/api/{tok}/status/{id}/delete", delete_status)
    r.add_post("/api/{tok}/send-reply", send_reply)
    r.add_post("/api/{tok}/send-mentioned", send_mentioned)
    r.add_post("/api/{tok}/send-file", send_file)
    r.add_post("/api/{tok}/send-voice-base64", send_voice_base64)
    r.add_post("/api/{tok}/get-media-by-message/{msgid}", get_media)
    async def archive_chat(req):
        s = sessao(req)
        corpo = await req.json()
        fone = corpo.get("phone")
        fone = fone[0] if isinstance(fone, list) else fone
        if s.status != "CONNECTED" or s.cliente is None:
            return respostas.desconectada()
        feito = await no_estado_da_conversa(s, fone, s.arquivar, bool(corpo.get("value", True)))
        if not feito:
            return web.json_response({"status": "error", "message": "nao aceito"}, status=500)
        return ok(status="success", response={})

    async def pin_chat(req):
        s = sessao(req)
        corpo = await req.json()
        fone = corpo.get("phone")
        fone = fone[0] if isinstance(fone, list) else fone
        if s.status != "CONNECTED" or s.cliente is None:
            return respostas.desconectada()
        fixar = str(corpo.get("state", "true")).lower() == "true"
        feito = await no_estado_da_conversa(s, fone, s.fixar, fixar)
        if not feito:
            return web.json_response({"status": "error", "message": "nao aceito"}, status=500)
        return ok(status="success", response={})

    async def em_thread(funcao, *args):
        return await asyncio.get_running_loop().run_in_executor(None, funcao, *args)

    def sem_conexao(s):
        return respostas.sem_conexao(s)

    async def no_estado_da_conversa(s, fone, acao, valor):
        """Arquivar, fixar ou silenciar: `acao(chave, valor)` na chave com que a
        conversa esta guardada (o numero ou o LID; sem linha, a dada) e, se o
        WhatsApp aceitou, o mesmo estado nas outras chaves dela. Sem isso a
        acao criava uma linha vazia sob o endereco errado e o list-chats
        seguinte voltava como estava."""
        jid = msgmap.jid_whatsmeow(fone)
        chaves = await em_thread(s.chaves_da_conversa, jid)
        alvo = chaves[0] if chaves else jid
        feito = await em_thread(acao, alvo, valor)
        if feito:
            for outra in chaves[1:]:
                s.bd.copiar_estado(alvo, outra)
        return feito
    async def star_message(req):
        s = sessao(req)
        corpo = await req.json()
        erro = sem_conexao(s)
        if erro is not None:
            return erro
        feito = await em_thread(s.favoritar, corpo["messageId"], bool(corpo.get("star", True)))
        if not feito:
            return web.json_response({"status": "error", "message": "nao aceito"}, status=500)
        return ok(status="success", response={"star": bool(corpo.get("star", True))})

    async def message_by_id(req):
        s = sessao(req)
        serial = req.match_info["msgid"]
        _, _, mid, _ = s.partes_do_id(serial)
        original = s.bd.mensagem_wpp(mid)
        if original is None:
            return web.json_response({"status": "error", "message": "message not found"}, status=404)
        # {data: mensagem}, como o servidor real (main_window/calls.py so le .data)
        return ok(status="success", response={"data": s.bd.para_resposta([original])[0]})

    async def send_mute(req):
        s = sessao(req)
        corpo = await req.json()
        erro = sem_conexao(s)
        if erro is not None:
            return erro
        quantidade = float(corpo.get("time") or 0)
        unidade = {"minutes": 60, "hours": 3600, "days": 86400, "weeks": 604800,
                   "months": 2592000, "years": 31536000}.get(str(corpo.get("type", "hours")), 3600)
        segundos = int(quantidade * unidade)
        if segundos >= 8766 * 3600:
            segundos = -1                       # "para sempre"
        fone = corpo.get("phone")
        fone = fone[0] if isinstance(fone, list) else fone
        feito = await no_estado_da_conversa(s, fone, s.silenciar, segundos)
        if not feito:
            return web.json_response({"status": "error", "message": "nao aceito"}, status=500)
        return ok(status="success", response={"isMuted": segundos != 0})

    async def send_poll_message(req):
        s = sessao(req)
        corpo = await req.json()
        erro = sem_conexao(s)
        if erro is not None:
            return erro
        from neonize.utils.enum import VoteType
        fone = corpo["phone"]
        fone = fone[0] if isinstance(fone, list) else fone
        if corpo.get("isGroup") and "@" not in fone:
            fone += "@g.us"
        destino = destino_de(fone)
        multipla = int((corpo.get("options") or {}).get("selectableCount", 1)) != 1

        def enviar():
            criada = s.cliente.build_poll_vote_creation(
                corpo["name"], list(corpo["choices"]),
                VoteType.MULTIPLE if multipla else VoteType.SINGLE)
            return s.cliente.send_message(destino, criada)
        try:
            resp = await em_thread(enviar)
        except Exception as erro_envio:
            log.exception("send-poll-message")
            return web.json_response({"status": "error", "message": str(erro_envio)},
                                     status=500)
        return guardar_enviada(s, destino, resp)

    async def poll_votes(req):
        s = sessao(req)
        corpo = await req.json()
        resposta = {}
        for serial in corpo.get("messageIds") or []:
            votos = s.votos_wpp(serial)
            if votos is not None:
                resposta[serial] = votos
        return ok(status="success", response=resposta)

    async def poll_voters(req):
        s = sessao(req)
        corpo = await req.json()
        resposta = s.votantes_wpp(corpo.get("messageId") or "")
        if not resposta.get("ok"):
            return web.json_response({"status": "error", "code": resposta["error"]}, status=503)
        return ok(status="success", response=resposta)

    async def vote_poll(req):
        s = sessao(req)
        corpo = await req.json()
        erro = sem_conexao(s)
        if erro is not None:
            return erro
        feito = await em_thread(s.votar_em_enquete, corpo["messageId"],
                                [int(i) for i in corpo.get("localIds") or []])
        if not feito:
            return web.json_response({"status": "error", "code": "poll_vote_failed"}, status=503)
        return ok(status="success")

    async def edit_message(req):
        s = sessao(req)
        corpo = await req.json()
        erro = sem_conexao(s)
        if erro is not None:
            return erro
        # recusas que acontecem antes de enviar algo: o cliente so desfaz o texto
        # otimista quando o corpo as diz (respostas.edicao_recusada)
        de_mim, _, mid, _ = s.partes_do_id(corpo["id"])
        motivo = regras_de_mensagem.motivo_da_recusa(de_mim, s.bd.mensagem_wpp(mid), time.time())
        if motivo:
            return respostas.edicao_recusada(motivo)
        mencoes = (corpo.get("options") or {}).get("mentionedJidList") or []
        feito = await em_thread(s.editar, corpo["id"], corpo["newText"], mencoes)
        if not feito:
            return web.json_response({"status": "error", "message": "nao aceito"}, status=500)
        return ok(status="success", response={"edited": True})

    async def forward_messages(req):
        s = sessao(req)
        corpo = await req.json()
        erro = sem_conexao(s)
        if erro is not None:
            return erro
        fone = corpo["phone"]
        fone = fone[0] if isinstance(fone, list) else fone
        destino = destino_de(fone)
        ids = corpo["messageId"]
        ids = ids if isinstance(ids, list) else [ids]
        enviadas, ids_novos = [], []
        for serial in ids:
            msg = await em_thread(s.mensagem_para_encaminhar, serial,
                                  bool(corpo.get("keepVoice")))
            if msg is None:
                return web.json_response(
                    {"status": "error", "message": "tipo de mensagem ainda nao encaminhavel"},
                    status=501)
            try:
                resp = await em_thread(s.cliente.send_message, destino, msg)
            except Exception as erro_envio:
                log.exception("encaminhar")
                return web.json_response({"status": "error", "message": str(erro_envio)},
                                         status=500)
            enviadas.append(guardar_enviada(s, destino, resp))
            ids_novos.append(resp.ID)
        # Sem eco: o servidor real avisa o encaminhamento por received-message e
        # o cliente conta com isso para atualizar a conversa de destino, mas aqui
        # o aviso e arriscado (o cliente casa o eco por tipo e pode trocar o id de
        # um envio pendente do mesmo tipo na mesma conversa). Sem ele, a conversa
        # de destino e a previa so mudam no proximo list-chats do cliente; os ids
        # novos vao na resposta para o cliente poder atualizar a conversa ao
        # receber o sucesso (ele ainda nao o faz).
        return ok(status="success", response=[{"forwarded": len(enviadas),
                                               "messageIds": ids_novos}])

    async def older_history(req):
        """O neonize nao tem o pedido de historico sob demanda (o do 'clique
        aqui para ver mensagens mais antigas'): diz que o resto so existe no
        celular, e o app para de pedir. O historico que chega e o do pareamento."""
        return ok(status="success", response={
            "requested": False, "primaryHasMore": True, "phoneOnly": True})

    async def react_message(req):
        s = sessao(req)
        corpo = await req.json()
        erro = sem_conexao(s)
        if erro is not None:
            return erro
        feito = await em_thread(s.reagir, corpo["msgId"], corpo.get("reaction") or "")
        if not feito:
            return web.json_response({"status": "error", "message": "nao aceito"}, status=500)
        return ok(status="success", response={"sendMsgResult": "ok"})

    async def reactions(req):
        s = sessao(req)
        return ok(status="success", response=s.reacoes_wpp(req.match_info["id"]))

    async def send_capabilities(req):
        return ok(status="success", response={"compatible": True})

    async def pinned_messages(req):
        """Mensagens fixadas nas conversas pedidas (a ponte guarda os avisos de
        fixar e soltar que passam por ela). A lista e PARCIAL: so o que a ponte
        viu passar (o aviso de fixar que chegou ou que ela mesma mandou); o que
        foi fixado antes do pareamento ou com a ponte desligada nao aparece, e o
        cliente (_apply_pinned_message_flags) trata a lista como completa e
        limpa o pinInChat do resto. Nao ha conserto barato: o neonize nao lista
        as fixadas de uma conversa."""
        s = sessao(req)
        corpo = await req.json()
        chats = [msgmap.jid_whatsmeow(c) for c in corpo.get("chatIds") or []]
        fixadas = s.bd.fixadas_de(chats, time.time())
        return ok(status="success", response=[{**w, "pinInChat": True} for w in fixadas])

    async def pin_message(req):
        s = sessao(req)
        corpo = await req.json()
        erro = sem_conexao(s)
        if erro is not None:
            return erro
        fixar = bool(corpo.get("pin", True))
        feito = await em_thread(s.fixar_mensagem, corpo["messageId"], fixar)
        if not feito:
            return web.json_response({"status": "error", "message": "nao aceito"}, status=500)
        return ok(status="success", response={"messageId": corpo["messageId"], "pinned": fixar})

    async def mark_played(req):
        s = sessao(req)
        corpo = await req.json()
        erro = sem_conexao(s)
        if erro is not None:
            return erro
        feito = await em_thread(s.marcar_ouvida, corpo["messageId"])
        return ok(status="success", response={"played": bool(feito)})

    async def delete_message(req):
        s = sessao(req)
        corpo = await req.json()
        erro = sem_conexao(s)
        if erro is not None:
            return erro
        ids = corpo["messageId"]
        ids = ids if isinstance(ids, list) else [ids]
        funcao = s.apagar_para_mim if corpo.get("onlyLocal") else s.apagar_para_todos
        feitos = [await em_thread(funcao, i) for i in ids]
        if not all(feitos):
            return web.json_response({"status": "error", "message": "nao aceito"}, status=500)
        return ok(status="success", response={"deleted": len(feitos)})

    r.add_post("/api/{tok}/star-message", star_message)
    r.add_get("/api/{tok}/message-by-id/{msgid}", message_by_id)
    r.add_post("/api/{tok}/send-mute", send_mute)
    r.add_post("/api/{tok}/mark-played", mark_played)
    r.add_post("/api/{tok}/react-message", react_message)
    r.add_post("/api/{tok}/edit-message", edit_message)
    r.add_post("/api/{tok}/forward-messages", forward_messages)
    r.add_post("/api/{tok}/request-older-messages/{jid}", older_history)
    r.add_get("/api/{tok}/older-history-state/{jid}", older_history)
    r.add_post("/api/{tok}/send-poll-message", send_poll_message)
    r.add_post("/api/{tok}/poll-votes", poll_votes)
    r.add_post("/api/{tok}/poll-voters", poll_voters)
    r.add_post("/api/{tok}/vote-poll", vote_poll)
    r.add_get("/api/{tok}/send-capabilities", send_capabilities)
    r.add_post("/api/{tok}/pinned-messages", pinned_messages)
    r.add_post("/api/{tok}/pin-message", pin_message)
    r.add_get("/api/{tok}/reactions/{id}", reactions)
    r.add_post("/api/{tok}/delete-message", delete_message)
    async def recuperar_estado(req):
        s = sessao(req)
        mudou = await em_thread(s.pedir_copia_do_estado)
        return ok(status="success", response={"mudancas": mudou})

    r.add_post("/api/{tok}/recuperar-estado", recuperar_estado)
    r.add_post("/api/{tok}/archive-chat", archive_chat)
    r.add_post("/api/{tok}/pin-chat", pin_chat)
    r.add_post("/api/{tok}/send-seen", send_seen)
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
    ap.add_argument("--log-estado", action="store_true",
                    help="liga o registro tecnico do lado em Go, filtrado para o estado do app")
    ap.add_argument("--pasta", default=os.path.join(os.path.dirname(__file__), "dados"))
    a = ap.parse_args()
    global HISTORICO_DIAS
    HISTORICO_DIAS = a.historico_dias
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    if a.log_estado:
        import re
        from neonize.utils import log as log_neonize
        log_neonize.setLevel(logging.DEBUG)
        for nome in ("whatsmeow", "Whatsmeow"):
            logging.getLogger(nome).setLevel(logging.DEBUG)
        padrao = re.compile(r"app ?state|patch|snapshot|key ?share|sync ?key|lthash|mutation|appstate|<receipt|<notification|<message|read-self", re.I)

        class SoEstado(logging.Filter):
            def filter(self, rec):
                return rec.levelno >= logging.WARNING or rec.name == "ponte"                     or bool(padrao.search(rec.getMessage()))
        for h in logging.getLogger().handlers:
            h.addFilter(SoEstado())
    web.run_app(criar_app(a.pasta, a.sessao_unica), port=a.porta, **OPCOES_DO_SERVIDOR)


if __name__ == "__main__":
    main()
