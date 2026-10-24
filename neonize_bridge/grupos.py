"""Rotas de grupos da ponte: criar, participantes, descricao, convite, entrar por
codigo e grupos em comum. Os formatos de resposta sao os que o cliente do
WinZapp le (core/group_operations.py, group_join.py, group_invitation.py).

As funcoes puras ficam no topo (testadas em test_grupos.py); `registrar` liga as
rotas ao servidor.
"""
import asyncio
import logging
import uuid

from aiohttp import web

log = logging.getLogger("ponte")

CODIGO_OK = 200
CODIGO_RECUSADO = 403


def usuario_de(texto: str) -> str:
    """'5511999@c.us' / '5511999:12@s.whatsapp.net' / '5511999' -> '5511999'."""
    return str(texto).split("@")[0].split(":")[0]


def resultado_de_participantes(pedidos, participantes) -> dict:
    """{pedido: {"code": ...}} no formato do WPPConnect, um item por quem foi
    pedido (na chave que o cliente mandou).

    `participantes` sao os GroupParticipant devolvidos pelo neonize. Quem nao
    voltou na lista nao foi aceito (403). Erro 0 e 200; um 403 com convite leva
    o codigo do convite, que o cliente oferece reenviar.
    """
    por_usuario = {}
    for p in participantes:
        for u in (p.JID.User, p.LID.User, p.PhoneNumber.User):
            if u:
                por_usuario[u] = p
    saida = {}
    for pedido in pedidos:
        p = por_usuario.get(usuario_de(pedido))
        if p is None:
            saida[pedido] = {"code": CODIGO_RECUSADO}
            continue
        codigo = int(p.Error) or CODIGO_OK
        info = {"code": codigo}
        if codigo == CODIGO_RECUSADO and p.AddRequest.Code:
            info["invite_code"] = p.AddRequest.Code
            info["invite_code_exp"] = int(p.AddRequest.Expiration or 0)
        saida[pedido] = info
    return saida


def pedido_de_entrada_pendente(erro: Exception) -> bool:
    """O WhatsApp aceitou o pedido, mas um administrador precisa aprovar."""
    texto = str(erro).lower()
    return "approval" in texto or "request" in texto


def ja_participa(erro: Exception) -> bool:
    texto = str(erro).lower()
    return "already" in texto or "participant" in texto


def pedido_para_wpp(pedido, fone_do_lid="", nome="") -> dict:
    """GroupParticipantRequest do neonize -> uma linha de
    `group-membership-requests` como o cliente le: {id, phone, name, method, t}.
    O id e o da pessoa (numero@c.us ou numero@lid); `fone_do_lid` e o numero
    que esta por tras de um @lid, quando se sabe."""
    jid = pedido.Participant
    servidor = "lid" if jid.Server == "lid" else "c.us"
    linha = {"id": "%s@%s" % (usuario_de(jid.User), servidor), "method": "",
             "t": int(pedido.TimeAt or 0)}
    if servidor == "lid" and fone_do_lid:
        linha["phone"] = "%s@c.us" % usuario_de(fone_do_lid)
    if nome:
        linha["name"] = nome
    return linha


def nao_e_administrador(erro: Exception) -> bool:
    texto = str(erro).lower()
    return "not-authorized" in texto or "forbidden" in texto or "403" in texto


def usuarios_em_comum(grupos, procurados) -> list:
    """Os ids dos grupos em que alguma das pessoas procuradas participa."""
    achados = []
    for g in grupos:
        for p in g.Participants:
            if procurados & {p.JID.User, p.LID.User, p.PhoneNumber.User}:
                achados.append(g.JID)
                break
    return achados


def registrar(r, *, sessao, ok, jid_de_texto, jid_texto, qr_data_url):
    """Liga as rotas de grupo ao roteador `r` do aiohttp."""
    from neonize.utils.enum import ParticipantChange

    async def executar(funcao, *args):
        return await asyncio.get_running_loop().run_in_executor(None, funcao, *args)

    def erro(mensagem, status=500):
        return web.json_response({"status": "error", "message": str(mensagem)}, status=status)

    def conectada(s):
        return s.cliente is not None and s.status == "CONNECTED"

    async def create_group(req):
        s = sessao(req)
        corpo = await req.json()
        if not conectada(s):
            return erro("Disconnected", 400)
        pedidos = list(corpo.get("participants") or [])
        try:
            info = await executar(
                s.cliente.create_group, corpo["name"], [jid_de_texto(x) for x in pedidos])
        except Exception as e:
            log.exception("create-group")
            return erro(e)
        participantes = resultado_de_participantes(pedidos, info.Participants)
        jid = jid_texto(info.JID)
        return ok(status="success", response={
            "gid": jid, "groupInfo": [{"id": jid, "participants": participantes}]})

    async def mudar_participantes(req, acao):
        s = sessao(req)
        corpo = await req.json()
        if not conectada(s):
            return erro("Disconnected", 400)
        pedidos = list(corpo.get("phone") or [])
        try:
            resposta = await executar(
                s.cliente.update_group_participants, jid_de_texto(corpo["groupId"]),
                [jid_de_texto(x) for x in pedidos], acao)
        except Exception as e:
            log.exception("participantes do grupo")
            return erro(e)
        return ok(status="success", response={
            "result": [resultado_de_participantes(pedidos, resposta)]})

    async def add_participant(req):
        return await mudar_participantes(req, ParticipantChange.ADD)

    async def remove_participant(req):
        return await mudar_participantes(req, ParticipantChange.REMOVE)

    async def promote_participant(req):
        return await mudar_participantes(req, ParticipantChange.PROMOTE)

    async def demote_participant(req):
        return await mudar_participantes(req, ParticipantChange.DEMOTE)

    async def group_description(req):
        s = sessao(req)
        corpo = await req.json()
        if not conectada(s):
            return erro("Disconnected", 400)
        jid = jid_de_texto(corpo["groupId"])
        try:
            info = await executar(s.cliente.get_group_info, jid)
            await executar(s.cliente.set_group_topic, jid, info.GroupTopic.TopicID,
                           uuid.uuid4().hex.upper(), corpo.get("description", ""))
        except Exception as e:
            log.exception("group-description")
            return erro(e)
        return ok(status="success", response={})

    async def group_info_from_invite_link(req):
        s = sessao(req)
        corpo = await req.json()
        if not conectada(s):
            return erro("Disconnected", 400)
        try:
            info = await executar(s.cliente.get_group_info_from_link, corpo["invitecode"])
        except Exception as e:
            log.info("group-info-from-invite-link: %r", e)
            return erro(e, 404)
        dados = {"id": jid_texto(info.JID), "subject": info.GroupName.Name,
                 "desc": info.GroupTopic.Topic}
        if info.Participants:
            dados["size"] = len(info.Participants)
        return ok(status="success", response=dados)

    async def join_code(req):
        s = sessao(req)
        corpo = await req.json()
        if not conectada(s):
            return erro("Disconnected", 400)
        codigo = corpo["inviteCode"]
        try:
            previa = await executar(s.cliente.get_group_info_from_link, codigo)
        except Exception as e:
            return erro(e, 404)
        jid = jid_texto(previa.JID)
        pendente = False
        try:
            await executar(s.cliente.join_group_with_link, codigo)
        except Exception as e:
            if ja_participa(e):
                return erro("already a participant", 409)
            if pedido_de_entrada_pendente(e):
                pendente = True
            else:
                log.exception("join-code")
                return erro(e)
        return ok(status="success", response={"id": jid, "pendingApproval": pendente})

    async def convite(req, revogar):
        s = sessao(req)
        if not conectada(s):
            return erro("Disconnected", 400)
        try:
            link = await executar(s.cliente.get_group_invite_link,
                                  jid_de_texto(req.match_info["groupId"]), revogar)
        except Exception as e:
            log.exception("group-invitation")
            return erro(e)
        return ok(status="success", response={"link": link, "qr": qr_data_url(link)})

    async def group_invitation(req):
        return await convite(req, False)

    async def group_invitation_reset(req):
        return await convite(req, True)

    async def common_groups(req):
        s = sessao(req)
        if not conectada(s):
            return erro("Disconnected", 400)
        wid = req.match_info["wid"]
        procurados = {usuario_de(wid)}
        if wid.endswith("@lid"):
            try:
                pn = await executar(s.cliente.get_pn_from_lid, jid_de_texto(wid))
                procurados.add(pn.User)
            except Exception:
                pass
        try:
            grupos = await executar(s.cliente.get_joined_groups)
        except Exception as e:
            log.exception("common-groups")
            return erro(e)
        achados = usuarios_em_comum(grupos, procurados - {""})
        return ok(status="success", response=[jid_texto(j) for j in achados])

    async def listar_pedidos(req):
        s = sessao(req)
        if not conectada(s):
            return erro("Disconnected", 400)
        jid = jid_de_texto(req.match_info["groupId"])
        try:
            pedidos = await executar(s.cliente.get_group_request_participants, jid)
        except Exception as e:
            if nao_e_administrador(e):
                return erro("not an administrator", 403)
            log.exception("group-membership-requests")
            return erro(e)
        linhas = []
        for pedido in pedidos:
            fone = ""
            if pedido.Participant.Server == "lid":
                try:
                    fone = (await executar(s.cliente.get_pn_from_lid,
                                           pedido.Participant)).User
                except Exception:
                    fone = ""
            nome = ""
            try:
                achado = s.bd.contato_de(jid_texto(pedido.Participant), fone)
                nome = achado.get("name") or achado.get("pushname") or ""
            except Exception:
                pass
            linhas.append(pedido_para_wpp(pedido, fone, nome))
        return ok(status="success", response={"requests": linhas, "refreshed": True})

    async def responder_pedidos(req, aprovar):
        s = sessao(req)
        corpo = await req.json()
        if not conectada(s):
            return erro("Disconnected", 400)
        if not hasattr(s.cliente, "update_group_request_participants"):
            return erro("This neonize build cannot answer join requests yet "
                        "(it needs the 0.5.2-winzapp-teste.2 build).", 501)
        from neonize.utils.enum import ParticipantRequestChange
        acao = (ParticipantRequestChange.APPROVE if aprovar
                else ParticipantRequestChange.REJECT)
        try:
            await executar(s.cliente.update_group_request_participants,
                           jid_de_texto(req.match_info["groupId"]),
                           [jid_de_texto(x) for x in corpo.get("participants") or []], acao)
        except Exception as e:
            if nao_e_administrador(e):
                return erro("not an administrator", 403)
            log.exception("group-membership-requests answer")
            return erro(e)
        return ok(status="success", response={})

    async def aprovar_pedidos(req):
        return await responder_pedidos(req, True)

    async def recusar_pedidos(req):
        return await responder_pedidos(req, False)

    r.add_post("/api/{tok}/create-group", create_group)
    r.add_post("/api/{tok}/add-participant-group", add_participant)
    r.add_post("/api/{tok}/remove-participant-group", remove_participant)
    r.add_post("/api/{tok}/promote-participant-group", promote_participant)
    r.add_post("/api/{tok}/demote-participant-group", demote_participant)
    r.add_post("/api/{tok}/group-description", group_description)
    r.add_post("/api/{tok}/group-info-from-invite-link", group_info_from_invite_link)
    r.add_post("/api/{tok}/join-code", join_code)
    r.add_get("/api/{tok}/group-invitation/{groupId}", group_invitation)
    r.add_post("/api/{tok}/group-invitation/{groupId}/reset", group_invitation_reset)
    r.add_get("/api/{tok}/common-groups/{wid}", common_groups)
    r.add_get("/api/{tok}/group-membership-requests/{groupId}", listar_pedidos)
    r.add_post("/api/{tok}/group-membership-requests/{groupId}/approve", aprovar_pedidos)
    r.add_post("/api/{tok}/group-membership-requests/{groupId}/reject", recusar_pedidos)
