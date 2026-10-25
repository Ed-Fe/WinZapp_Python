"""Mudancas de grupo ao vivo: nome, descricao, ajustes e membros.

O WhatsApp avisa por dois eventos do neonize: `GroupInfoEv` (alguem mudou algo
num grupo em que a conta esta: Name, Topic, Locked, Announce, Join, Leave,
Promote, Demote...) e `JoinedGroupEv` (a conta entrou ou criou um grupo, com o
`GroupInfo` inteiro). Os dois viram o Socket.IO `groups.update`, que o cliente
le em core/websocket_client.py (`on_groups_update`): {"session", "data": [..]},
cada item com `id` (ou `remoteJid`) e `subject`/`name`.

Hoje o cliente so age sobre `id` + `subject` (um nome novo atualiza a lista de
conversas). Os demais campos seguem a forma do Baileys/WPPConnect (`desc`,
`announce`, `restrict`, `participants` + `action`) e o cliente os ignora; um item
sem `subject` e descartado la, entao emiti-los e inofensivo e deixa o dado pronto.

As funcoes puras ficam no topo (testadas em test_eventos_grupos.py) e nao
importam o neonize; `registrar_eventos` liga os eventos a uma sessao. Os
manipuladores NAO foram provados contra uma conta real.
"""
import logging

import msgmap
from redacao import redigir

log = logging.getLogger("ponte")

# GroupInfoEv.<lista> -> action do groups.update (os do Baileys).
ACOES = (("Join", "add"), ("Leave", "remove"), ("Promote", "promote"), ("Demote", "demote"))


def _tem(msg, campo) -> bool:
    """O campo de mensagem (sub-protobuf) veio preenchido no evento?"""
    if msg is None:
        return False
    try:
        return bool(msg.HasField(campo))
    except (AttributeError, ValueError):
        return getattr(msg, campo, None) is not None


def jid_texto(jid) -> str:
    """JID do neonize -> 'usuario@servidor' ('' se vazio)."""
    if jid is None or not getattr(jid, "User", ""):
        return ""
    return f"{jid.User}@{jid.Server}"


def atualizacao_de_campos(id_grupo, nome=None, topico=None, travado=None, avisos=None):
    """Nome, descricao e ajustes -> UM item do `groups.update` (como o Baileys
    junta os campos), ou None se nada mudou. `nome` e texto; `topico` e
    (texto, apagado); `travado` = so admins editam os dados do grupo
    (`restrict`); `avisos` = so admins mandam mensagem (`announce`)."""
    item = {}
    if nome is not None and nome.strip():
        item["subject"] = nome.strip()
    if topico is not None:
        item["desc"] = "" if topico[1] else topico[0]
    if travado is not None:
        item["restrict"] = bool(travado)
    if avisos is not None:
        item["announce"] = bool(avisos)
    if not item:
        return None
    return {"id": id_grupo, **item}


def atualizacoes_de_membros(id_grupo, listas) -> list:
    """{acao: [ids]} -> um item por acao com gente, na ordem de ACOES."""
    return [{"id": id_grupo, "participants": list(listas[acao]), "action": acao}
            for _, acao in ACOES if listas.get(acao)]


def atualizacoes_do_evento(e, id_de_pessoa) -> list:
    """GroupInfoEv -> itens do `groups.update`. `id_de_pessoa(jid)` devolve o id
    do cliente para um JID de participante (numero em vez de LID quando se sabe)."""
    id_grupo = msgmap.jid_wpp(jid_texto(getattr(e, "JID", None)))
    if not id_grupo.endswith("@g.us"):
        return []
    nome = topico = travado = avisos = None
    if _tem(e, "Name"):
        nome = e.Name.Name
    if _tem(e, "Topic"):
        topico = (e.Topic.Topic, bool(e.Topic.TopicDeleted))
    if _tem(e, "Locked"):
        travado = e.Locked.isLocked
    if _tem(e, "Announce"):
        avisos = e.Announce.IsAnnounce
    itens = []
    campos = atualizacao_de_campos(id_grupo, nome, topico, travado, avisos)
    if campos:
        itens.append(campos)
    listas = {}
    for lista, acao in ACOES:
        listas[acao] = [i for i in (id_de_pessoa(j) for j in getattr(e, lista, ())) if i]
    return itens + atualizacoes_de_membros(id_grupo, listas)


def atualizacoes_de_entrada(e) -> list:
    """JoinedGroupEv (a conta entrou/criou um grupo) -> itens do `groups.update`
    com o nome, a descricao e os ajustes do `GroupInfo`."""
    info = getattr(e, "GroupInfo", None)
    id_grupo = msgmap.jid_wpp(jid_texto(getattr(info, "JID", None)))
    if not id_grupo.endswith("@g.us"):
        return []
    item = atualizacao_de_campos(
        id_grupo,
        nome=info.GroupName.Name if _tem(info, "GroupName") else None,
        topico=((info.GroupTopic.Topic, bool(info.GroupTopic.TopicDeleted))
                if _tem(info, "GroupTopic") else None),
        travado=info.GroupLocked.isLocked if _tem(info, "GroupLocked") else None,
        avisos=info.GroupAnnounce.IsAnnounce if _tem(info, "GroupAnnounce") else None)
    return [item] if item else []


def id_de_pessoa(c, jid):
    """Numero antes do LID (o app nao sabe mostrar um LID); sem par, o LID."""
    texto = jid_texto(jid)
    if jid is not None and getattr(jid, "Server", "") == "lid":
        try:
            par = jid_texto(c.get_pn_from_lid(jid))
            if par and not par.endswith("@lid"):
                texto = par
        except Exception:
            pass
    return msgmap.jid_wpp(texto)


def emitir_itens(s, itens):
    if itens:
        s.emitir("groups.update", {"session": s.nome, "data": itens})


def grupo_mudou(c, s, e):
    """GroupInfoEv -> groups.update. Quem registra o GroupInfoEv e o server.py
    (o neonize guarda UM manipulador por tipo de evento: um segundo registro
    apagaria o primeiro), que chama esta funcao. Nao testado contra uma conta
    real."""
    try:
        itens = atualizacoes_do_evento(e, lambda j: id_de_pessoa(c, j))
        log.info("grupo %s mudou (%d item(ns))",
                 redigir(jid_texto(getattr(e, "JID", None))), len(itens))
        emitir_itens(s, itens)
    except Exception:
        log.exception("grupo mudou")


def registrar_eventos(cli, s):
    """Liga o JoinedGroupEv do cliente neonize `cli` a sessao `s` (precisa de
    `s.nome` e `s.emitir`). Chamado na thread do neonize. O GroupInfoEv NAO
    e registrado aqui: ver grupo_mudou(). Nao testado contra uma conta real."""
    import neonize.events as nev

    @cli.event(nev.JoinedGroupEv)
    def _entrou_no_grupo(c, e):
        try:
            itens = atualizacoes_de_entrada(e)
            log.info("entrada em grupo (%d item(ns))", len(itens))
            emitir_itens(s, itens)
        except Exception:
            log.exception("entrada em grupo")
