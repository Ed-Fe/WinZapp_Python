"""Rotas de comunidades da ponte: listar, criar, vincular e desvincular grupos,
promover e rebaixar administradores. Os formatos de resposta sao os que o
cliente do WinZapp le (core/communities.py).

Uma comunidade, no neonize/whatsmeow, e um grupo "pai" (`GroupParent.IsParent`);
seus grupos apontam para ele por `GroupLinkedParent`, e `link_group` /
`unlink_group` ligam e desligam um grupo. O neonize 0.5.2 NAO tem como
desativar uma comunidade, nem entrar em (ou cancelar o pedido de entrada em) um
grupo de uma comunidade: essas tres rotas (`deactivate-community` e
`community-subgroups/:c/:g/join` e `.../cancel-request`) ficam de fora.

As funcoes puras ficam no topo (testadas em test_comunidades.py); `registrar`
liga as rotas ao servidor. Nada aqui foi testado contra uma conta real.
"""
import asyncio
import logging
import re
import uuid

from aiohttp import web

from grupos import resultado_de_participantes, usuario_de

log = logging.getLogger("ponte")

# Os mesmos formatos que as rotas proprias do WinZapp aceitam
# (communityAdminController.ts).
_GRUPO = re.compile(r"\d+(-\d+)?@g\.us")
_PESSOA = re.compile(r"\d+@(c\.us|lid)")


def jid_texto(jid) -> str:
    """JID do neonize -> 'usuario@servidor' (vazio se nao ha). Igual ao do server.py."""
    if jid is None or not getattr(jid, "User", ""):
        return ""
    return "%s@%s" % (jid.User, jid.Server)


def e_comunidade(grupo) -> bool:
    return bool(grupo.GroupParent.IsParent)


def id_da_pessoa(p) -> str:
    """O id com que a comunidade mostra um participante: o numero, quando se
    sabe (como o group-info do server.py), senao o JID ou o LID."""
    return jid_texto(p.PhoneNumber) or jid_texto(p.JID) or jid_texto(p.LID)


def administradores_de(grupo) -> list:
    """[{id, superAdmin}] de quem administra a comunidade."""
    return [{"id": id_da_pessoa(p), "superAdmin": bool(p.IsSuperAdmin)}
            for p in grupo.Participants
            if (p.IsAdmin or p.IsSuperAdmin) and id_da_pessoa(p)]


def sou_administrador(grupo, meus_usuarios) -> bool:
    """A conta e administradora da comunidade. `meus_usuarios` sao o numero e o
    LID proprios (so a parte antes do @); desconhecido e "nao"."""
    meus = {u for u in meus_usuarios if u}
    for p in grupo.Participants:
        if (p.IsAdmin or p.IsSuperAdmin) and meus & {p.JID.User, p.LID.User, p.PhoneNumber.User}:
            return True
    return False


def subgrupo_entrado(grupo) -> dict:
    return {"id": jid_texto(grupo.JID), "subject": grupo.GroupName.Name,
            "announcement": bool(grupo.GroupIsDefaultSub.IsDefaultSubGroup),
            "general": False, "joined": True, "approval": False}


def subgrupo_de_alvo(alvo) -> dict:
    """GroupLinkTarget (um grupo da comunidade, que a conta pode nao ter
    entrado) -> uma linha de subgrupo. O neonize nao diz se entrar exige
    aprovacao nem se ja ha pedido: `approval` e `requested` ficam falsos."""
    return {"id": jid_texto(alvo.JID), "subject": alvo.GroupName.Name,
            "announcement": bool(alvo.GroupIsDefaultSub.IsDefaultSubGroup),
            "general": False, "joined": False, "approval": False, "requested": False}


def subgrupos_da_comunidade(comunidade, grupos, alvos=()) -> list:
    """Os grupos da comunidade: primeiro os que a conta entrou (`grupos`: tudo
    que a conta participa, ligados ao pai por GroupLinkedParent) e depois, de
    `alvos` (get_sub_groups), os que ela nao entrou."""
    usuario = comunidade.JID.User
    linhas = [subgrupo_entrado(g) for g in grupos
              if g.GroupLinkedParent.LinkedParentJID.User == usuario
              and g.JID.User != usuario]
    conhecidos = {linha["id"] for linha in linhas}
    for alvo in alvos:
        linha = subgrupo_de_alvo(alvo)
        if linha["id"] and linha["id"] not in conhecidos and alvo.JID.User != usuario:
            conhecidos.add(linha["id"])
            linhas.append(linha)
    return linhas


def comunidade_para_wpp(comunidade, subgrupos, meus_usuarios) -> dict:
    """GroupInfo do pai -> um item de `communities` como o cliente le."""
    return {"id": jid_texto(comunidade.JID), "subject": comunidade.GroupName.Name,
            "description": comunidade.GroupTopic.Topic,
            "iAmAdmin": sou_administrador(comunidade, meus_usuarios),
            "admins": administradores_de(comunidade), "subgroups": subgrupos}


def comunidades_para_wpp(grupos, meus_usuarios) -> list:
    """Todas as comunidades de uma lista de grupos, so com os subgrupos que a
    conta entrou (nada de rede por comunidade)."""
    return [comunidade_para_wpp(g, subgrupos_da_comunidade(g, grupos), meus_usuarios)
            for g in grupos if e_comunidade(g)]


def ids_de_grupos(valor):
    """A lista de ids de grupo (`groupIds` / `groupsIds`) ou None se vier algo
    que nao seja uma lista de ids de grupo."""
    if not isinstance(valor, list):
        return None
    if not all(isinstance(x, str) and _GRUPO.fullmatch(x) for x in valor):
        return None
    return valor


def resultado_de_vinculos(chave, ok_ids, falhas) -> dict:
    """{<chave>: [...], failedGroups: [{groupJid, errorText}]}: a resposta da
    biblioteca do WPPConnect para vincular (`linkedGroupJids`) e desvincular
    (`unlinkedGroupJids`). `falhas` e uma lista de (id, texto do erro)."""
    return {chave: list(ok_ids),
            "failedGroups": [{"groupJid": i, "errorText": t} for i, t in falhas]}


def resultado_de_promocao(pedidos, participantes) -> dict:
    """Resposta de promote/demote-community-participant: {participants, success}.
    `success` e falso se alguem nao foi aceito; o cliente trata `success: false`
    como recusa (core/communities.py, _act)."""
    por_pessoa = resultado_de_participantes(pedidos, participantes)
    return {"participants": [{"id": pedido, "code": por_pessoa[pedido]["code"]}
                             for pedido in pedidos],
            "success": all(v["code"] == 200 for v in por_pessoa.values())}


def promocao_aceita(pedido, participantes) -> bool:
    """O WhatsApp aceitou a promocao/rebaixamento dessa pessoa."""
    return resultado_de_participantes([pedido], participantes)[pedido]["code"] == 200


def pessoas_da_comunidade(participantes, administradores) -> list:
    """Linhas de `community-participants`: {id, isAdmin, isSuperAdmin}.
    `participantes` sao os JIDs de quem esta na comunidade e `administradores`,
    os GroupParticipant do pai (de onde vem quem e admin)."""
    admins = {}
    for p in administradores:
        for u in (p.JID.User, p.LID.User, p.PhoneNumber.User):
            if u:
                admins[u] = p
    linhas = []
    for jid in participantes:
        if not getattr(jid, "User", ""):
            continue
        p = admins.get(jid.User)
        linhas.append({"id": jid_texto(jid),
                       "isAdmin": bool(p and (p.IsAdmin or p.IsSuperAdmin)),
                       "isSuperAdmin": bool(p and p.IsSuperAdmin)})
    return linhas


def registrar(r, *, sessao, ok, jid_de_texto):
    """Liga as rotas de comunidade ao roteador `r` do aiohttp."""
    from neonize.utils.enum import ParticipantChange

    async def executar(funcao, *args):
        return await asyncio.get_running_loop().run_in_executor(None, funcao, *args)

    def erro(mensagem, status=500, codigo=None):
        corpo = {"status": "error", "message": str(mensagem)}
        if codigo:
            corpo["code"] = codigo
        return web.json_response(corpo, status=status)

    def conectada(s):
        return s.cliente is not None and s.status == "CONNECTED"

    def meus_usuarios(s):
        return {usuario_de(s.eu or ""), usuario_de(s.lid or "")} - {""}

    async def create_community(req):
        s = sessao(req)
        corpo = await req.json()
        if not conectada(s):
            return erro("Disconnected", 400)
        nome = corpo.get("name")
        descricao = corpo.get("description") or ""
        filhos = ids_de_grupos(corpo.get("groupIds") or [])
        if not isinstance(nome, str) or not nome.strip() or filhos is None:
            return erro("invalid request", 400, "invalid_request")
        from neonize.proto.Neonize_pb2 import GroupParent
        try:
            info = await executar(s.cliente.create_group, nome.strip(), [],
                                  None, GroupParent(IsParent=True))
        except Exception as e:
            log.exception("create-community")
            return erro(e)
        pai = info.JID
        if descricao:
            try:
                await executar(s.cliente.set_group_topic, pai, "",
                               uuid.uuid4().hex.upper(), descricao)
            except Exception as e:
                # A comunidade existe: a descricao falhar nao a desfaz.
                log.warning("create-community: descricao nao gravada (%s)", type(e).__name__)
        vinculados, falhas = await vincular(s, pai, filhos, True)
        return ok(status="success", response={
            "wid": jid_texto(pai),
            "subGroups": resultado_de_vinculos("linkedGroupJids", vinculados, falhas)})

    async def vincular(s, pai, filhos, ligar):
        """Liga (ou desliga) cada grupo, um a um, devolvendo (feitos, falhas)."""
        funcao = s.cliente.link_group if ligar else s.cliente.unlink_group
        feitos, falhas = [], []
        for texto in filhos:
            try:
                await executar(funcao, pai, jid_de_texto(texto))
                feitos.append(texto)
            except Exception as e:
                log.warning("community %s: um grupo recusado (%s)",
                            "link" if ligar else "unlink", type(e).__name__)
                falhas.append((texto, str(e)))
        return feitos, falhas

    async def vinculos(req, ligar):
        s = sessao(req)
        corpo = await req.json()
        if not conectada(s):
            return erro("Disconnected", 400)
        comunidade = corpo.get("id")
        filhos = ids_de_grupos(corpo.get("groupsIds"))
        if not isinstance(comunidade, str) or not _GRUPO.fullmatch(comunidade) or not filhos:
            return erro("invalid request", 400, "invalid_request")
        feitos, falhas = await vincular(s, jid_de_texto(comunidade), filhos, ligar)
        if falhas and not feitos:
            return erro(falhas[0][1], 500)
        chave = "linkedGroupJids" if ligar else "unlinkedGroupJids"
        return ok(status="success", response=resultado_de_vinculos(chave, feitos, falhas))

    async def add_community_subgroup(req):
        return await vinculos(req, True)

    async def remove_community_subgroup(req):
        return await vinculos(req, False)

    async def communities(req):
        s = sessao(req)
        if not conectada(s):
            return erro("Disconnected", 400)
        try:
            grupos = await executar(s.cliente.get_joined_groups)
        except Exception as e:
            log.exception("communities")
            return erro(e, 503, "communities_unavailable")
        return ok(status="success", response=comunidades_para_wpp(grupos, meus_usuarios(s)))

    async def community_subgroups(req):
        s = sessao(req)
        if not conectada(s):
            return erro("Disconnected", 400)
        id_pai = req.match_info["communityId"]
        if not _GRUPO.fullmatch(id_pai):
            return erro("invalid community", 400, "invalid_community")
        try:
            grupos = await executar(s.cliente.get_joined_groups)
        except Exception as e:
            log.exception("community-subgroups")
            return erro(e, 503, "communities_unavailable")
        pai = next((g for g in grupos if e_comunidade(g) and jid_texto(g.JID) == id_pai), None)
        if pai is None:
            return erro("community not found", 404, "community_not_found")
        try:
            alvos = await executar(s.cliente.get_sub_groups, pai.JID)
        except Exception as e:
            # Sem a lista do servidor, ainda ha os grupos que a conta entrou.
            log.warning("community-subgroups: get_sub_groups falhou (%s)", type(e).__name__)
            alvos = []
        subgrupos = subgrupos_da_comunidade(pai, grupos, alvos)
        return ok(status="success", response=[
            comunidade_para_wpp(pai, subgrupos, meus_usuarios(s))])

    async def community_participants(req):
        s = sessao(req)
        if not conectada(s):
            return erro("Disconnected", 400)
        id_pai = req.match_info["id"]
        if not _GRUPO.fullmatch(id_pai):
            return erro("invalid community", 400, "invalid_community")
        pai = jid_de_texto(id_pai)
        try:
            pedidos = await executar(s.cliente.get_linked_group_participants, pai)
        except Exception as e:
            log.exception("community-participants")
            return erro(e)
        try:
            administradores = (await executar(s.cliente.get_group_info, pai)).Participants
        except Exception:
            administradores = []
        return ok(status="success", response=pessoas_da_comunidade(
            [p.Participant for p in pedidos], administradores))

    async def mudar_administrador(s, id_pai, pessoas, acao):
        """promote/demote no grupo pai; devolve os GroupParticipant do neonize."""
        return await executar(
            s.cliente.update_group_participants, jid_de_texto(id_pai),
            [jid_de_texto(x) for x in pessoas], acao)

    async def community_admin_promote(req):
        s = sessao(req)
        corpo = await req.json()
        if not conectada(s):
            return erro("Disconnected", 400)
        id_pai = req.match_info["communityId"]
        pessoa = corpo.get("participant")
        if not _GRUPO.fullmatch(id_pai) or not isinstance(pessoa, str) \
                or not _PESSOA.fullmatch(pessoa):
            return erro("invalid request", 400, "invalid_request")
        try:
            resposta = await mudar_administrador(s, id_pai, [pessoa], ParticipantChange.PROMOTE)
        except Exception as e:
            log.exception("community-admin promote")
            return erro(e, 503, "promotion_unavailable")
        if not promocao_aceita(pessoa, resposta):
            return erro("promotion refused", 409, "promotion_refused")
        return ok(status="success", response={"promoted": True})

    async def acao_de_participantes(req, acao):
        s = sessao(req)
        corpo = await req.json()
        if not conectada(s):
            return erro("Disconnected", 400)
        id_pai = corpo.get("id")
        pessoas = corpo.get("participantsId")
        if not isinstance(id_pai, str) or not _GRUPO.fullmatch(id_pai) \
                or not isinstance(pessoas, list) or not pessoas \
                or not all(isinstance(x, str) and x for x in pessoas):
            return erro("invalid request", 400, "invalid_request")
        try:
            resposta = await mudar_administrador(s, id_pai, pessoas, acao)
        except Exception as e:
            log.exception("community participants")
            return erro(e)
        return ok(status="success", response=resultado_de_promocao(pessoas, resposta))

    async def promote_community_participant(req):
        return await acao_de_participantes(req, ParticipantChange.PROMOTE)

    async def demote_community_participant(req):
        return await acao_de_participantes(req, ParticipantChange.DEMOTE)

    r.add_post("/api/{tok}/create-community", create_community)
    r.add_post("/api/{tok}/add-community-subgroup", add_community_subgroup)
    r.add_post("/api/{tok}/remove-community-subgroup", remove_community_subgroup)
    r.add_get("/api/{tok}/communities", communities)
    r.add_get("/api/{tok}/community-subgroups/{communityId}", community_subgroups)
    r.add_get("/api/{tok}/community-participants/{id}", community_participants)
    r.add_post("/api/{tok}/community-admin/{communityId}/promote", community_admin_promote)
    r.add_post("/api/{tok}/promote-community-participant", promote_community_participant)
    r.add_post("/api/{tok}/demote-community-participant", demote_community_participant)
