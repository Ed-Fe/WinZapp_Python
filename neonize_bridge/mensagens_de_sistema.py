"""Mensagens de sistema da conversa: avisos de grupo (`gp2`) e registros de
chamada (`call_log`).

O WhatsApp Web guarda os dois como mensagens da conversa e o WPPConnect os
entrega pelo `received-message`; o whatsmeow os entrega como eventos
(`GroupInfoEv`, `CallTerminateEv`), nao como mensagem. Aqui o evento vira a
mensagem no formato do WPPConnect, que o cliente le em
core/websocket_client.py (`_normalize_wpp_message`, ramos `gp2` e `call_log`):

- `gp2`: `subtype` (add, leave, remove, promote, demote, subject, description,
  restrict, announce, ephemeral), `recipients` (ids dos afetados), `author`
  (quem fez), `body` (nome/descricao novos, ou "on"/"off" nos ajustes) e
  `value` (booleano dos ajustes). O texto do aviso e o cliente que monta.
- `call_log`: `callOutcome`/`finalCallOutcome` (o enum `CallOutcome` do
  WhatsApp Web), `isVideoCall`, `callDuration`, `callCreator`. O id da mensagem
  e o id da chamada: o cliente a relê por `message-by-id` com esse id
  (core/call_log.py `call_log_candidate_ids`).

O id e deterministico (hash do evento) para que o mesmo evento entregue duas
vezes nao vire duas mensagens. Nenhuma das duas conta como nao lida: o cliente
as trata como evento de sistema (`is_countable_message`) e aqui `ao_chegar` as
conta como se fossem minhas, que e o jeito de subir a data da conversa sem
somar nao lida.

As funcoes puras ficam no topo (test_mensagens_de_sistema.py) e nao importam o
neonize. Nao foi provado contra uma conta real.
"""
import hashlib
import logging

import msgmap
from redacao import redigir

log = logging.getLogger("ponte")

# Um aviso mais velho que isso (fila de eventos entregue ao reconectar) vai ao
# cliente marcado como historico: ele o guarda na ordem certa, sem tratar como
# novo (websocket_client.on_messages_upsert, `isMdHistoryMsg`).
AO_VIVO = 60


def _tem(msg, campo) -> bool:
    """O campo de mensagem (sub-protobuf) veio preenchido no evento?"""
    if msg is None:
        return False
    try:
        return bool(msg.HasField(campo))
    except (AttributeError, ValueError):
        return getattr(msg, campo, None) is not None


def _texto(jid) -> str:
    if jid is None or not getattr(jid, "User", ""):
        return ""
    return f"{jid.User}@{jid.Server}"


def id_do_aviso(*partes) -> str:
    """Id estavel de um aviso: o mesmo evento da sempre o mesmo id. Sem '_' (o
    cliente parte o id serializado por ele) e com cara de id de mensagem."""
    bruto = "\x1f".join(str(p) for p in partes)
    return "WZ" + hashlib.sha1(bruto.encode("utf-8")).hexdigest()[:22].upper()


def _base(conversa_w, id_msg, ts, tipo, autor="") -> dict:
    serial = f"false_{conversa_w}_{id_msg}"
    if autor and conversa_w.endswith("@g.us"):
        serial += f"_{autor}"       # como o msgmap.montar faz nas mensagens de grupo
    return {
        "id": {"fromMe": False, "remote": conversa_w, "id": id_msg, "_serialized": serial},
        "fromMe": False, "from": conversa_w, "to": conversa_w,
        "t": ts, "timestamp": ts, "isStatus": False, "notifyName": "",
        "type": tipo, "body": "",
    }


# ------------------------------------------------------------- avisos de grupo
def avisos_de_grupo(e, id_de_pessoa, agora) -> list:
    """GroupInfoEv -> uma mensagem `gp2` por mudanca (nome, descricao, ajustes e
    cada lista de membros). `id_de_pessoa(jid)` devolve o id do cliente de um
    JID de participante (o numero em vez do LID quando se sabe)."""
    grupo = msgmap.jid_wpp(_texto(getattr(e, "JID", None)))
    if not grupo.endswith("@g.us"):
        return []
    ts = msgmap.segundos(getattr(e, "Timestamp", 0)) or int(agora)
    autor = id_de_pessoa(getattr(e, "Sender", None)) or ""
    avisos = []

    def aviso(subtype, quem=(), body="", valor=None, autor_do_aviso=None):
        quem = list(quem)
        responsavel = autor if autor_do_aviso is None else autor_do_aviso
        w = _base(grupo, id_do_aviso(grupo, ts, subtype, responsavel, sorted(quem), body),
                  ts, "gp2", responsavel)
        w.update(subtype=subtype, recipients=quem, body=body)
        if responsavel:
            w["author"] = responsavel
        if valor is not None:
            w["value"] = valor
        avisos.append(w)

    def ajuste(subtype, ligado):
        aviso(subtype, body="on" if ligado else "off", valor=bool(ligado))

    if _tem(e, "Name") and e.Name.Name.strip():
        aviso("subject", body=e.Name.Name.strip())
    if _tem(e, "Topic"):
        aviso("description", body="" if e.Topic.TopicDeleted else e.Topic.Topic)
    if _tem(e, "Locked"):
        ajuste("restrict", e.Locked.isLocked)       # so admins editam os dados do grupo
    if _tem(e, "Announce"):
        ajuste("announce", e.Announce.IsAnnounce)   # so admins mandam mensagem
    if _tem(e, "Ephemeral"):
        ajuste("ephemeral", e.Ephemeral.IsEphemeral)

    def pessoas(lista):
        return [i for i in (id_de_pessoa(j) for j in getattr(e, lista, ())) if i]

    entraram, sairam = pessoas("Join"), pessoas("Leave")
    if entraram:
        # entrou por link/convite: ninguem o adicionou (o cliente le "entrou")
        aviso("add", entraram, autor_do_aviso="" if getattr(e, "JoinReason", "") else None)
    if sairam:
        if autor and autor not in sairam:
            aviso("remove", sairam)
        else:
            aviso("leave", sairam, autor_do_aviso="")
    if pessoas("Promote"):
        aviso("promote", pessoas("Promote"))
    if pessoas("Demote"):
        aviso("demote", pessoas("Demote"))
    return avisos


# ------------------------------------------------------------ registro de chamada
# estado do `evento_de_fim` (chamadas.py) -> CallOutcome do WhatsApp Web
DESFECHOS = {"NOT_ANSWERED": "Missed", "REJECTED": "Rejected",
             "HANDLED_REMOTELY": "AcceptedElsewhere"}


def desfecho_da_chamada(estado, tocava):
    """O `callOutcome` de uma chamada recebida que terminou, ou None se nao da
    para dizer. Um fim sem motivo ("ENDED") de uma chamada que ainda tocava aqui
    e o autor desligando antes de alguem atender ("Canceled", que o cliente conta
    como perdida, como o proprio WhatsApp). Quando houve um CallAccept, quem
    chama aqui (chamadas.refinar_fim) ja trocou o estado por HANDLED_REMOTELY,
    entao este "Canceled" e so o que sobra sem nenhum sinal de que atenderam.
    Sem ter visto a oferta nao se sabe se alguem atendeu, e nada e registrado."""
    if estado in DESFECHOS:
        return DESFECHOS[estado]
    return "Canceled" if (estado == "ENDED" and tocava) else None


def registro_de_chamada(*, id_chamada, conversa_w, criador_w, desfecho, video, ts, duracao=0) -> dict:
    """Mensagem `call_log` (id = id da chamada). `conversa_w` e o par numa
    chamada comum e o grupo numa de grupo; `criador_w` quem ligou."""
    w = _base(conversa_w, id_chamada, ts, "call_log", criador_w)
    w.update(callOutcome=desfecho, finalCallOutcome=desfecho, isVideoCall=bool(video),
             callDuration=int(duracao or 0), callCreator=criador_w)
    return w


# ------------------------------------------------------------ guardar e emitir
def guardar_e_emitir(s, chat, w, agora) -> bool:
    """Guarda `w` na conversa `chat` (a chave do armazem, no formato do
    whatsmeow) e a emite como o `_mensagem` do server.py faz. False se o id ja
    estava guardado (evento repetido): nada e guardado nem emitido de novo."""
    id_msg = w["id"]["id"]
    if not id_msg or s.bd.mensagem_wpp(id_msg) is not None:
        return False
    s.bd.mensagem(chat, w)
    s.bd.ao_chegar(chat, w["t"], True)      # sobe a data, nao soma nao lida
    saida = dict(w)
    if agora - w["t"] > AO_VIVO:
        saida["isMdHistoryMsg"] = True
    s.emitir("received-message", {"session": s.nome, "response": saida})
    log.info("mensagem de sistema %s na conversa %s", w["type"], redigir(chat))
    return True
