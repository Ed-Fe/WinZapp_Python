"""Chamadas da ponte: o que o whatsmeow consegue fazer com uma ligacao.

Ele recebe a oferta e o fim de uma chamada (CallOffer, CallTerminate) e sabe
recusar uma chamada que esta tocando (RejectCall). Ele NAO atende, nao liga e
nao carrega audio nem video (nao ha midia WebRTC/VoIP nele), entao as outras
rotas de `call/*` respondem 501 em vez de fingir que deram certo.

Os formatos dos eventos sao os que o cliente le em core/websocket_client.py
(`incomingcall` e `callstate`); os da recusa, os de main_window/calls.py
(`/call/reject` com {"callId"} ou {} e resposta com `via`/`call.state`).

As funcoes puras ficam no topo (testadas em test_chamadas.py); `registrar` liga
as rotas e `registrar_eventos` liga os eventos do neonize a uma sessao.
"""
import asyncio
import logging
import time

from aiohttp import web

import mensagens_de_sistema
import msgmap
from redacao import redigir

log = logging.getLogger("ponte")

# Um toque dura no maximo ~2 minutos; o que passou muito disso nao esta mais
# tocando e so ocuparia memoria.
VALIDADE_DO_TOQUE = 600

# Rotas que o whatsmeow nao tem como cumprir (nao ha como atender, discar,
# manter nem inspecionar uma chamada). `call/group/offer` nao esta no
# COBERTURA.md mas o servidor do WPPConnect a define, e o motivo e o mesmo.
ROTAS_IMPOSSIVEIS = (
    "call/accept", "call/audio/enable", "call/diagnostics", "call/end",
    "call/ensure-ended", "call/offer", "call/group/offer", "call/status",
    "call/upgrade-video",
)
MOTIVO_IMPOSSIVEL = "calls cannot be placed, answered or carried through neonize (whatsmeow)"


def atributo(no, nome) -> str:
    """O primeiro atributo `nome` do no ou de qualquer descendente ('' se nao ha).
    `no` e um Node do neonize (Tag, Attrs[name/text], Nodes)."""
    for a in no.Attrs:
        if a.name == nome and a.text:
            return a.text
    for filho in no.Nodes:
        achado = atributo(filho, nome)
        if achado:
            return achado
    return ""


def tem_tag(no, tag) -> bool:
    """Se o no ou algum descendente tem a tag (a oferta de video traz <video/>)."""
    if no.Tag == tag:
        return True
    return any(tem_tag(filho, tag) for filho in no.Nodes)


def _jid_texto(jid) -> str:
    if jid is None or not getattr(jid, "User", ""):
        return ""
    return msgmap.jid_wpp(f"{jid.User}@{jid.Server}")


def quem_ligou(meta) -> str:
    """O id de quem ligou, no formato do WPPConnect. Prefere o numero (@c.us) ao
    @lid quando o whatsmeow entrega os dois; o cliente sabe resolver os dois."""
    criador = _jid_texto(meta.callCreator)
    alternativo = _jid_texto(meta.callCreatorAlt)
    if criador.endswith("@lid") and alternativo and not alternativo.endswith("@lid"):
        return alternativo
    return criador or alternativo


def evento_de_oferta(e, agora) -> dict:
    """CallOffer do neonize -> o `data` do `incomingcall` (e do `callstate`)."""
    meta = e.basicCallMeta
    grupo = msgmap.jid_wpp(atributo(e.data, "group-jid"))
    return {
        "event": "offer", "state": "INCOMING_RING",
        "peerJid": quem_ligou(meta), "id": meta.callID,
        "isVideo": tem_tag(e.data, "video"),
        "isGroup": bool(grupo), "groupJid": grupo,
        "timestamp": msgmap.segundos(meta.timestamp), "observedAt": int(agora),
        # oferta que ficou na fila enquanto o app estava fora: o cliente a ignora
        "offerReceivedWhileOffline": bool(atributo(e.data, "offline")),
    }


def estado_do_fim(motivo) -> tuple:
    """Motivo do CallTerminate -> (event, state) que o cliente entende. Qualquer
    estado fora do toque ja faz o cliente calar o alerta; o refinamento so muda
    o que ele registra."""
    m = str(motivo or "").lower()
    if "timeout" in m:
        return "timeout", "NOT_ANSWERED"
    if "reject" in m or "decline" in m:
        return "ended", "REJECTED"
    if "accept" in m:
        return "ended", "HANDLED_REMOTELY"
    return "ended", "ENDED"


def evento_de_fim(e, agora) -> dict:
    """CallTerminate do neonize -> o `data` do `incomingcall` e do `callstate`."""
    meta = e.basicCallMeta
    evento, estado = estado_do_fim(e.reason)
    grupo = msgmap.jid_wpp(atributo(e.data, "group-jid"))
    return {
        "event": evento, "state": estado,
        "peerJid": quem_ligou(meta), "id": meta.callID,
        "isVideo": tem_tag(e.data, "video"),
        "isGroup": bool(grupo), "groupJid": grupo,
        "outgoing": False,
        "timestamp": msgmap.segundos(meta.timestamp), "observedAt": int(agora),
    }


def refinar_fim(dados, aceitou) -> dict:
    """Um fim sem motivo ("ENDED") de uma chamada que ALGUEM ATENDEU antes e uma
    chamada atendida em outro aparelho (o celular): nao e a ligacao que o autor
    cancelou, e o cliente conta essa como perdida. `aceitou` e o que `Pendentes`
    guardou do CallAccept: (instante do servidor, instante local) ou None.
    Devolve `dados` com o estado e a duracao (segundos da conversa) acertados;
    qualquer outro fim, inclusive o que traz motivo, fica como veio."""
    if dados["state"] != "ENDED" or not aceitou:
        return dados
    return {**dados, "state": "HANDLED_REMOTELY",
            "callDuration": duracao_da_chamada(aceitou, dados["timestamp"], dados["observedAt"])}


def duracao_da_chamada(aceitou, terminou_servidor, terminou_local) -> int:
    """Segundos entre o CallAccept e o CallTerminate: pelos instantes do
    servidor quando os dois existem (o relogio daqui pode estar errado), senao
    pelos locais. Nunca negativa."""
    servidor, local = aceitou
    if servidor and terminou_servidor:
        return max(0, int(terminou_servidor) - int(servidor))
    return max(0, int(terminou_local) - int(local))


def conversa_da_chamada(bd, meta, grupo) -> str:
    """A chave (formato whatsmeow) da conversa onde o registro da chamada vai: o
    grupo, ou o par. O par pode estar guardado pelo numero ou pelo LID; vale a
    forma que ja tem conversa, e na falta dela o numero."""
    if grupo:
        return msgmap.jid_whatsmeow(grupo)
    formas = [msgmap.jid_whatsmeow(j) for j in (_jid_texto(meta.callCreator),
                                                _jid_texto(meta.callCreatorAlt)) if j]
    formas.sort(key=lambda j: j.endswith("@lid"))          # o numero primeiro
    for forma in formas:
        if bd.existe_conversa(forma):
            return forma
    return formas[0] if formas else ""


def registrar_fim(s, e, dados, tocava, agora):
    """O fim de uma chamada recebida vira o registro `call_log` na conversa (o
    que o WhatsApp Web mostra como "Ligacao de voz perdida"). `dados` e o
    `evento_de_fim`; `tocava` diz se a oferta ainda estava tocando aqui. Nao
    registra a chamada que eu mesmo fiz (de outro aparelho): nao da para saber
    com quem foi so pelo evento. False se nada foi registrado."""
    desfecho = mensagens_de_sistema.desfecho_da_chamada(dados["state"], tocava)
    criador = dados["peerJid"]
    meus = {msgmap.jid_wpp(getattr(s, "eu", "")), msgmap.jid_wpp(getattr(s, "lid", ""))} - {""}
    if desfecho is None or not dados["id"] or not criador or criador in meus:
        return False
    chat = conversa_da_chamada(s.bd, e.basicCallMeta, dados["groupJid"])
    if not chat:
        return False
    w = mensagens_de_sistema.registro_de_chamada(
        id_chamada=dados["id"], conversa_w=msgmap.jid_wpp(chat), criador_w=criador,
        desfecho=desfecho, video=dados["isVideo"],
        ts=dados["timestamp"] or int(agora), duracao=dados.get("callDuration", 0))
    return mensagens_de_sistema.guardar_e_emitir(s, chat, w, agora)


class Pendentes:
    """Chamadas que estao tocando: id -> o JID de quem ligou, EXATAMENTE como veio
    na oferta (o RejectCall exige isso)."""

    def __init__(self):
        self._toques = {}
        self._aceitas = {}

    def aceitar(self, id_chamada, ts_servidor, agora):
        """O CallAccept: alguem (nao este cliente, que nao atende) atendeu."""
        self._aceitas = {i: t for i, t in self._aceitas.items()
                         if agora - t[1] < VALIDADE_DO_TOQUE}
        self._aceitas[id_chamada] = (ts_servidor, agora)

    def aceitou(self, id_chamada):
        """(instante do servidor, instante local) do CallAccept, ou None."""
        return self._aceitas.get(id_chamada)

    def lembrar(self, id_chamada, criador, agora):
        self._toques = {i: t for i, t in self._toques.items()
                        if agora - t[1] < VALIDADE_DO_TOQUE}
        self._toques[id_chamada] = (criador, agora)

    def esquecer(self, id_chamada):
        self._toques.pop(id_chamada, None)
        self._aceitas.pop(id_chamada, None)

    def criador_de(self, id_chamada):
        achado = self._toques.get(id_chamada)
        return achado[0] if achado else None

    def a_recusar(self, corpo) -> list:
        """Os ids a recusar para o corpo de `call/reject`: o `callId` pedido, ou
        (corpo vazio, o app so sabe que ha uma chamada tocando) todas as que
        tocam. Lista vazia se o pedido nao bate com nenhuma."""
        pedido = str((corpo or {}).get("callId") or "")
        if pedido:
            return [pedido] if pedido in self._toques else []
        return list(self._toques)


def resposta_da_recusa(ids) -> dict:
    """O `response` de `call/reject`: o cliente so le `via` e `call.state`."""
    return {"via": "neonize", "call": {"state": "REJECTED"}, "rejected": len(ids)}


def registrar_eventos(cli, s):
    """Liga a oferta e o fim de chamada do cliente neonize `cli` a sessao `s`
    (precisa de `s.nome` e `s.emitir`). Chamado na thread do neonize."""
    import neonize.events as nev

    s.chamadas_tocando = Pendentes()

    def avisar(dados):
        # incomingcall abre/fecha o alerta; callstate acompanha a chamada em curso
        # (o WPPConnect emite os dois para cada mudanca, ver createSessionUtil.ts)
        s.emitir("incomingcall", {"session": s.nome, "data": dados})
        s.emitir("callstate", {"session": s.nome, "data": dados})

    @cli.event(nev.CallOfferEv)
    def _oferta(c, e):
        try:
            dados = evento_de_oferta(e, time.time())
            if not dados["id"]:
                return
            s.chamadas_tocando.lembrar(dados["id"], e.basicCallMeta.callCreator, time.time())
            log.info("chamada recebida (video=%s grupo=%s)", dados["isVideo"], dados["isGroup"])
            avisar(dados)
        except Exception:
            log.exception("oferta de chamada")

    @cli.event(nev.CallAcceptEv)
    def _atendida(c, e):
        try:
            if e.basicCallMeta.callID:
                s.chamadas_tocando.aceitar(
                    e.basicCallMeta.callID, msgmap.segundos(e.basicCallMeta.timestamp), time.time())
        except Exception:
            log.exception("chamada atendida")

    @cli.event(nev.CallTerminateEv)
    def _fim(c, e):
        try:
            dados = evento_de_fim(e, time.time())
            dados = refinar_fim(dados, s.chamadas_tocando.aceitou(dados["id"]))
            tocava = s.chamadas_tocando.criador_de(dados["id"]) is not None
            s.chamadas_tocando.esquecer(dados["id"])
            log.info("chamada terminou (%s)", dados["state"])
            avisar(dados)
        except Exception:
            log.exception("fim de chamada")
            return
        try:
            registrar_fim(s, e, dados, tocava, time.time())
        except Exception:
            log.exception("registro da chamada %s", redigir(dados["id"]))


def registrar(r, *, sessao, ok):
    """Liga as rotas de chamada ao roteador `r` do aiohttp."""

    def erro(mensagem, status=500):
        return web.json_response({"status": "error", "message": str(mensagem)}, status=status)

    async def recusar(req):
        s = sessao(req)
        try:
            corpo = await req.json()
        except Exception:
            corpo = {}
        if s.cliente is None or s.status != "CONNECTED":
            return erro("Disconnected", 400)
        tocando = getattr(s, "chamadas_tocando", None)
        ids = tocando.a_recusar(corpo) if tocando is not None else []
        if not ids:
            return erro("call not found", 404)
        laco = asyncio.get_running_loop()
        try:
            for id_chamada in ids:
                await laco.run_in_executor(
                    None, s.cliente.reject_call, tocando.criador_de(id_chamada), id_chamada)
                tocando.esquecer(id_chamada)
        except Exception as e:
            log.exception("recusar chamada")
            return erro(e)
        return ok(status="success", response=resposta_da_recusa(ids))

    async def impossivel(req):
        return erro(MOTIVO_IMPOSSIVEL, 501)

    r.add_post("/api/{tok}/call/reject", recusar)
    r.add_post("/api/{tok}/reject-call", recusar)
    for rota in ROTAS_IMPOSSIVEIS:
        r.add_post("/api/{tok}/" + rota, impossivel)
        r.add_get("/api/{tok}/" + rota, impossivel)
