"""Presenca da ponte: quem esta online, a ultima vez que foi visto e quem esta
digitando ou gravando audio.

O WhatsApp so entrega presenca por evento (e so de quem a conta assinou com
`subscribe-presence`), entao a ponte guarda o ultimo evento de cada pessoa numa
memoria pequena e responde `last-seen/:phone` e `chat-is-online/:phone` a partir
dela. O neonize traz dois eventos: `PresenceEv` (From, Unavailable, LastSeen) e
`ChatPresenceEv` (MessageSource, State, Media). Os formatos emitidos sao os que o
cliente le em core/websocket_client.py (`onpresencechanged`) e os da resposta,
os de core/contact_presence.py (`response_snapshot`).

Privacidade: quando a pessoa esconde o visto por ultimo o whatsmeow entrega
`LastSeen` zerado. Isso nao distingue "offline" de "online escondido", entao a
rota `last-seen` responde `unsupported` (o cliente nao insiste) em vez de afirmar
"offline". Nada aqui registra numero, JID ou nome no log.

As funcoes puras ficam no topo (testadas em test_presenca.py); `registrar` liga a
rota de `chat-is-online` e `registrar_eventos` liga os eventos do neonize a uma
sessao. Nada aqui foi provado contra uma conta real.
"""
import logging
import time

from aiohttp import web

import msgmap

log = logging.getLogger("ponte")

# Quanto tempo cada fato vale. O "online" some sozinho (o aviso de "offline" nao
# e garantido); o "offline" com visto por ultimo envelhece devagar; digitar e
# gravar duram so uns segundos (o aviso de "parou" tambem pode nao chegar).
VALIDADE_ONLINE = 300
VALIDADE_OFFLINE = 3600
VALIDADE_ATIVIDADE = 25
MAXIMO_DE_PESSOAS = 2000

# ChatPresence.State e ChatPresenceMedia do proto do neonize.
ESTADO_DIGITANDO, ESTADO_PAUSADO = 1, 2
MIDIA_AUDIO = 2


def chave(jid) -> str:
    """O id que a rota recebe ('5511999', '5511999@c.us', 'x:12@s.whatsapp.net',
    'x@lid') -> a chave da memoria ('5511999@c.us'). '' se vazio."""
    texto = str(jid or "")
    if not texto:
        return ""
    if "@" not in texto:
        texto += "@c.us"
    return msgmap.jid_wpp(texto)


def chaves_da_pessoa(principal, alternativo="") -> list:
    """As chaves sob as quais uma pessoa fica guardada: o JID do evento e, se o
    whatsmeow/ponte souberam o par (numero <-> LID), o outro. Sem repeticao."""
    achadas = []
    for jid in (principal, alternativo):
        k = chave(jid)
        if k and k not in achadas:
            achadas.append(k)
    return achadas


def visto_de(ts):
    """LastSeen do PresenceEv -> segundos, ou None quando vem zerado/negativo
    (a pessoa esconde o visto por ultimo)."""
    t = msgmap.segundos(ts)
    return t if t > 0 else None


def estado_da_atividade(estado, midia) -> str:
    """(State, Media) do ChatPresenceEv -> 'composing' | 'recording' | 'paused'
    (os valores que o cliente entende); '' se o estado nao e conhecido."""
    if estado == ESTADO_DIGITANDO:
        return "recording" if midia == MIDIA_AUDIO else "composing"
    if estado == ESTADO_PAUSADO:
        return "paused"
    return ""


class Presencas:
    """Memoria de presenca em RAM. Tempo sempre vem de fora (`agora`, segundos)."""

    def __init__(self):
        self._pessoas = {}      # chave -> {"online", "visto", "em"}
        self._atividade = {}    # (conversa, quem) -> (estado, em)

    def _podar(self, agora):
        self._atividade = {k: v for k, v in self._atividade.items()
                           if agora - v[1] < VALIDADE_ATIVIDADE}
        excesso = len(self._pessoas) - MAXIMO_DE_PESSOAS
        if excesso > 0:
            antigas = sorted(self._pessoas, key=lambda k: self._pessoas[k]["em"])[:excesso]
            for k in antigas:
                del self._pessoas[k]

    def definir_presenca(self, chaves, online, visto, agora):
        """Guarda o ultimo PresenceEv. `visto`: segundos, ou None se escondido."""
        ficha = {"online": bool(online), "visto": visto or None, "em": agora}
        for k in chaves:
            self._pessoas[k] = ficha
        self._podar(agora)

    def definir_atividade(self, conversa, quem, estado, agora):
        """Guarda digitando/gravando de `quem` em `conversa`; 'paused' limpa."""
        if estado in ("composing", "recording"):
            self._atividade[(conversa, quem)] = (estado, agora)
        else:
            self._atividade.pop((conversa, quem), None)
        self._podar(agora)

    def ler(self, id_pessoa, agora):
        """O que se sabe de uma pessoa agora, ou None se nada vale mais:
        {"estado", "isOnline", "lastSeen"}. Digitar/gravar recente conta como
        online (a pessoa esta com a conversa aberta)."""
        k = chave(id_pessoa)
        if not k:
            return None
        ativ = self._atividade.get((k, k))
        if ativ and agora - ativ[1] < VALIDADE_ATIVIDADE:
            return {"estado": ativ[0], "isOnline": True, "lastSeen": None}
        ficha = self._pessoas.get(k)
        if ficha is None:
            return None
        if ficha["online"]:
            if agora - ficha["em"] >= VALIDADE_ONLINE:
                return None
            return {"estado": "available", "isOnline": True, "lastSeen": None}
        if agora - ficha["em"] >= VALIDADE_OFFLINE:
            return None
        return {"estado": "unavailable", "isOnline": False, "lastSeen": ficha["visto"]}


def evento_de_presenca(id_wpp, online, visto, agora) -> dict:
    """PresenceEv -> o `onpresencechanged` de uma pessoa. `snapshotStatus: ready`
    so quando o dado esta completo (online, ou offline com o visto por ultimo):
    com o visto escondido o cliente ainda nao sabe se foi offline mesmo."""
    evento = {
        "id": id_wpp, "state": "available" if online else "unavailable",
        "isOnline": bool(online), "t": int(agora),
        "isGroup": False, "isUser": True}
    if not online and visto:
        evento["lastSeen"] = visto
    if online or visto:
        evento["snapshotStatus"] = "ready"
    return evento


def evento_de_atividade(conversa_wpp, quem_wpp, estado, agora, grupo) -> dict:
    """ChatPresenceEv -> o `onpresencechanged`. Em grupo o estado vai por
    participante; no privado, no proprio evento (sem afirmar online/offline)."""
    if grupo:
        return {
            "id": conversa_wpp, "state": estado, "t": int(agora), "isOnline": False,
            "isGroup": True, "isUser": False, "shortName": "",
            "participants": [{"id": quem_wpp, "state": estado, "shortName": ""}]}
    evento = {"id": conversa_wpp, "state": estado, "t": int(agora),
              "isGroup": False, "isUser": True}
    if estado in ("composing", "recording"):
        evento["isOnline"] = True
    return evento


def resposta_last_seen(dado) -> dict:
    """Os campos de resposta de `last-seen/:phone` para o que `Presencas.ler`
    devolveu. `unsupported` sem dado ou com o visto escondido."""
    if dado is None or (not dado["isOnline"] and not dado["lastSeen"]):
        return {"response": False, "presence": {"status": "unsupported"}}
    return {"response": dado["lastSeen"] or False, "presence": {
        "status": "ready", "isOnline": dado["isOnline"],
        "lastSeen": dado["lastSeen"], "state": dado["estado"]}}


def resposta_online(dado) -> dict:
    """Os campos de resposta de `chat-is-online/:phone` (o WPPConnect devolve so
    um booleano). Sem dado, False."""
    resposta = {"response": bool(dado and dado["isOnline"])}
    if dado is None:
        resposta["presence"] = {"status": "unsupported"}
    return resposta


def _jid_texto(jid) -> str:
    if jid is None or not getattr(jid, "User", ""):
        return ""
    return f"{jid.User}@{jid.Server}"


def registrar_eventos(cli, s):
    """Liga PresenceEv e ChatPresenceEv do cliente neonize `cli` a sessao `s`
    (precisa de `s.nome` e `s.emitir`). Chamado na thread do neonize."""
    import neonize.events as nev

    s.presencas = Presencas()

    def par(c, jid):
        """O outro JID da mesma pessoa (numero <-> LID), ou '' se nao se sabe."""
        try:
            if jid.Server == "lid":
                return _jid_texto(c.get_pn_from_lid(jid))
            if jid.Server == "s.whatsapp.net":
                return _jid_texto(c.get_lid_from_pn(jid))
        except Exception:
            pass
        return ""

    def preferido(c, jid):
        """(o id a mostrar ao app, as chaves de memoria): o numero antes do LID."""
        principal = _jid_texto(jid)
        outro = par(c, jid)
        mostrar = principal
        if principal.endswith("@lid") and outro and not outro.endswith("@lid"):
            mostrar = outro
        return msgmap.jid_wpp(mostrar), chaves_da_pessoa(principal, outro)

    @cli.event(nev.PresenceEv)
    def _presenca(c, e):
        try:
            agora = time.time()
            id_wpp, chaves = preferido(c, e.From)
            if not chaves:
                return
            visto = visto_de(e.LastSeen)
            s.presencas.definir_presenca(chaves, not e.Unavailable, visto, agora)
            log.info("presenca recebida (online=%s visto=%s)", not e.Unavailable, bool(visto))
            s.emitir("onpresencechanged", {
                "session": s.nome,
                **evento_de_presenca(id_wpp, not e.Unavailable, visto, agora)})
        except Exception:
            log.exception("presenca")

    @cli.event(nev.ChatPresenceEv)
    def _atividade(c, e):
        try:
            origem = e.MessageSource
            if origem.IsFromMe:
                return
            estado = estado_da_atividade(e.State, e.Media)
            if not estado:
                return
            agora = time.time()
            grupo = bool(origem.IsGroup)
            conversa_wpp, chaves_conversa = preferido(c, origem.Chat)
            if not chaves_conversa:
                return
            if grupo:
                quem_wpp, chaves_quem = preferido(c, origem.Sender)
                if not quem_wpp:
                    return
                for k in chaves_quem:
                    s.presencas.definir_atividade(chaves_conversa[0], k, estado, agora)
            else:
                quem_wpp = conversa_wpp
                for k in chaves_conversa:
                    s.presencas.definir_atividade(k, k, estado, agora)
            log.info("atividade recebida (%s grupo=%s)", estado, grupo)
            s.emitir("onpresencechanged", {
                "session": s.nome,
                **evento_de_atividade(conversa_wpp, quem_wpp, estado, agora, grupo)})
        except Exception:
            log.exception("atividade")


def registrar(r, *, sessao, ok):
    """Liga `chat-is-online/:phone` ao roteador `r` do aiohttp."""
    from perfil import id_de_pessoa_valido

    async def chat_is_online(req):
        if not id_de_pessoa_valido(req.match_info["phone"]):
            return web.json_response(
                {"status": "error", "response": "Invalid contact id"}, status=400)
        memoria = getattr(sessao(req), "presencas", None)
        dado = memoria.ler(req.match_info["phone"], time.time()) if memoria else None
        return ok(status="success", **resposta_online(dado))

    r.add_get("/api/{tok}/chat-is-online/{phone}", chat_is_online)
