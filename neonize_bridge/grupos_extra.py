"""Rotas de grupo que faltavam em grupos.py: listar grupos e membros, link de
convite, foto do grupo e as configuracoes (so admins enviam / so admins editam).

Nenhuma delas e chamada pelo cliente do WinZapp hoje (grep em client/ nao acha),
entao os formatos de resposta seguem o que o WPPConnect Server devolve
({status, response}) e os ids saem como o WhatsApp Web os escreve (@c.us).

As funcoes puras ficam no topo (testadas em test_grupos_extra.py); `registrar`
liga as rotas ao servidor. Sem `neonize` nem `aiohttp` no topo: os dois so
entram dentro de `registrar`, para as funcoes puras serem testaveis sem eles.
"""
import asyncio
import base64
import binascii
import io
import logging
import time

import mensagens_efemeras
import msgmap

log = logging.getLogger("ponte")

LADO_MAXIMO_FOTO = 640       # o WhatsApp Web recorta a foto do grupo em 640x640

# propriedade do WPPConnect (group-property) -> o que o neonize sabe mudar.
# `ephemeral` tem ramo proprio em group_property (mensagens_efemeras.py).
# `no_frequently_forwarded` e os modos de aprovacao/adicao de membros nao
# entram: o neonize 0.5.2 nao tem chamada confirmada para eles.
PROPRIEDADES = {"announce": "announce", "announcement": "announce",
                "restrict": "locked", "locked": "locked"}


def jid_do_grupo(texto) -> str:
    """'123-456' / '123-456@g.us' -> '123-456@g.us'. Outros servidores passam."""
    texto = str(texto or "").strip()
    return texto if "@" in texto else texto + "@g.us"


def grupos_do_corpo(corpo) -> list:
    """Os grupos que o pedido cita: `groupId` ou `phone` (texto ou lista)."""
    bruto = corpo.get("groupId")
    if bruto in (None, ""):
        bruto = corpo.get("phone")
    if isinstance(bruto, (list, tuple)):
        itens = bruto
    else:
        itens = [bruto]
    return [jid_do_grupo(x) for x in itens if x not in (None, "")]


def como_booleano(valor, padrao=True) -> bool:
    """true/false do JSON ou do multipart ('true', 'false', '1', '0')."""
    if valor is None or valor == "":
        return padrao
    if isinstance(valor, str):
        return valor.strip().lower() not in ("false", "0", "no", "off")
    return bool(valor)


def texto_do_jid(jid) -> str:
    """JID do neonize -> 'usuario@servidor' (vazio se nao ha)."""
    if jid is None or not getattr(jid, "User", ""):
        return ""
    return "%s@%s" % (jid.User, jid.Server)


def wid_de(texto) -> dict:
    """'5511@s.whatsapp.net' -> {server, user, _serialized} (o Wid do WhatsApp Web)."""
    serial = msgmap.jid_wpp(texto)
    usuario, _, servidor = serial.partition("@")
    return {"server": servidor, "user": usuario, "_serialized": serial}


def jid_do_participante(p) -> str:
    """O id que identifica a pessoa: o numero quando se sabe, senao JID, senao LID."""
    return (texto_do_jid(p.PhoneNumber) or texto_do_jid(p.JID)
            or texto_do_jid(p.LID))


def participante_para_wpp(p) -> dict:
    """GroupParticipant -> uma linha de group-members."""
    linha = {"id": wid_de(jid_do_participante(p)),
             "isAdmin": bool(p.IsAdmin or p.IsSuperAdmin),
             "isSuperAdmin": bool(p.IsSuperAdmin)}
    lid = texto_do_jid(p.LID)
    if lid:
        linha["lid"] = wid_de(lid)
    if p.DisplayName:
        linha["name"] = p.DisplayName
    return linha


def membros_para_wpp(info) -> list:
    return [participante_para_wpp(p) for p in info.Participants]


def ids_dos_membros(info) -> list:
    return [wid_de(jid_do_participante(p)) for p in info.Participants]


def ids_dos_administradores(info) -> list:
    return [wid_de(jid_do_participante(p)) for p in info.Participants
            if p.IsAdmin or p.IsSuperAdmin]


def grupo_para_chat(info) -> dict:
    """GroupInfo -> uma linha de all-groups (um chat de grupo com seus metadados)."""
    jid = texto_do_jid(info.JID)
    dono = texto_do_jid(info.OwnerPN) or texto_do_jid(info.OwnerJID)
    return {
        "id": wid_de(jid), "name": info.GroupName.Name, "isGroup": True,
        "groupMetadata": {
            "id": wid_de(jid), "subject": info.GroupName.Name,
            "desc": info.GroupTopic.Topic, "owner": wid_de(dono) if dono else None,
            "creation": msgmap.segundos(info.GroupCreated),
            "announce": bool(info.GroupAnnounce.IsAnnounce),
            "restrict": bool(info.GroupLocked.isLocked),
            "participants": membros_para_wpp(info)}}


def acao_da_propriedade(propriedade):
    """'announce' / 'restrict' (e apelidos) -> 'announce' / 'locked'; None se o
    neonize nao sabe mudar essa propriedade."""
    return PROPRIEDADES.get(str(propriedade or "").strip().lower())


def imagem_do_corpo(campos):
    """Bytes da foto vindos do JSON ou dos campos do multipart: `base64` ou `path`
    como base64 puro ou data URI. Caminho de arquivo do disco nao e lido de
    proposito. Vazio se nao veio nada; ValueError se nao e base64."""
    bruto = campos.get("base64") or campos.get("path") or ""
    if not isinstance(bruto, str) or not bruto.strip():
        return b""
    try:
        return base64.b64decode(bruto.strip().split(",", 1)[-1], validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("a foto nao esta em base64")


def foto_para_jpeg(dados: bytes) -> bytes:
    """O WhatsApp so aceita JPEG como foto de grupo: converte (PNG, WebP...) e
    reduz o lado maior a 640. JPEG ja pequeno passa sem recompressao."""
    from PIL import Image
    imagem = Image.open(io.BytesIO(dados))
    if imagem.format == "JPEG" and max(imagem.size) <= LADO_MAXIMO_FOTO:
        return dados
    imagem = imagem.convert("RGB")
    imagem.thumbnail((LADO_MAXIMO_FOTO, LADO_MAXIMO_FOTO))
    saida = io.BytesIO()
    imagem.save(saida, format="JPEG", quality=90)
    return saida.getvalue()


def registrar(r, *, sessao, ok, jid_de_texto, jid_texto, qr_data_url):
    """Liga as rotas ao roteador `r` do aiohttp. Mesmos argumentos de
    grupos.registrar (nem todos sao usados aqui)."""
    from aiohttp import web
    from grupos import nao_e_administrador

    async def executar(funcao, *args):
        return await asyncio.get_running_loop().run_in_executor(None, funcao, *args)

    def erro(mensagem, status=500):
        return web.json_response({"status": "error", "message": str(mensagem)}, status=status)

    def conectada(s):
        return s.cliente is not None and s.status == "CONNECTED"

    def falha(e, nome):
        if nao_e_administrador(e):
            return erro("not an administrator", 403)
        log.exception(nome)
        return erro(e)

    async def info_do_grupo(req, nome):
        """(info, None) ou (None, resposta de erro)."""
        s = sessao(req)
        if not conectada(s):
            return None, erro("Disconnected", 400)
        try:
            return await executar(s.cliente.get_group_info,
                                  jid_de_texto(jid_do_grupo(req.match_info["groupId"]))), None
        except Exception as e:
            log.info("%s: %s", nome, type(e).__name__)
            return None, erro(e, 404)

    async def all_groups(req):
        s = sessao(req)
        if not conectada(s):
            return erro("Disconnected", 400)
        try:
            grupos = await executar(s.cliente.get_joined_groups)
        except Exception as e:
            log.exception("all-groups")
            return erro(e)
        return ok(status="success", response=[grupo_para_chat(g) for g in grupos])

    async def group_members(req):
        info, falhou = await info_do_grupo(req, "group-members")
        return falhou or ok(status="success", response=membros_para_wpp(info))

    async def group_members_ids(req):
        info, falhou = await info_do_grupo(req, "group-members-ids")
        return falhou or ok(status="success", response=ids_dos_membros(info))

    async def group_admins(req):
        info, falhou = await info_do_grupo(req, "group-admins")
        return falhou or ok(status="success", response=ids_dos_administradores(info))

    async def link_do_grupo(req, revogar):
        s = sessao(req)
        if not conectada(s):
            return erro("Disconnected", 400)
        try:
            link = await executar(s.cliente.get_group_invite_link,
                                  jid_de_texto(jid_do_grupo(req.match_info["groupId"])), revogar)
        except Exception as e:
            return falha(e, "group-invite-link")
        return ok(status="success", response=link)

    async def group_invite_link(req):
        return await link_do_grupo(req, False)

    async def group_revoke_link(req):
        return await link_do_grupo(req, True)

    async def ajustar(req, nome, campo, acao=None):
        """Liga/desliga uma permissao em todos os grupos do pedido. `acao` e
        'announce' ou 'locked'; sem ela vem do campo `property` (group-property)."""
        s = sessao(req)
        if not conectada(s):
            return erro("Disconnected", 400)
        corpo = await req.json()
        acao = acao or acao_da_propriedade(corpo.get("property"))
        if acao is None:
            return erro("unsupported group property: %s" % corpo.get("property"), 400)
        alvos = grupos_do_corpo(corpo)
        if not alvos:
            return erro("groupId is required", 400)
        funcao = getattr(s.cliente, "set_group_" + acao)
        valor = como_booleano(corpo.get(campo), True)
        try:
            for alvo in alvos:
                await executar(funcao, jid_de_texto(alvo), valor)
        except Exception as e:
            return falha(e, nome)
        return ok(status="success", response={})

    async def grupo_efemero(req, corpo):
        """property=ephemeral: `value` em segundos (0 desliga) ou booleano."""
        s = sessao(req)
        try:
            segundos = mensagens_efemeras.segundos_do_timer(corpo.get("value"))
        except ValueError as e:
            return erro(e, 400)
        alvos = grupos_do_corpo(corpo)
        if not alvos:
            return erro("groupId is required", 400)
        if not conectada(s):
            return erro("Disconnected", 400)
        try:
            for alvo in alvos:
                await executar(mensagens_efemeras.aplicar_timer, s.cliente,
                               jid_de_texto(alvo), segundos, time.time())
        except Exception as e:
            return falha(e, "group-property")
        return ok(status="success", response={})

    async def group_property(req):
        corpo = await req.json()
        if mensagens_efemeras.e_propriedade_efemera(corpo.get("property")):
            return await grupo_efemero(req, corpo)
        return await ajustar(req, "group-property", "value")

    async def messages_admins_only(req):
        return await ajustar(req, "messages-admins-only", "value", "announce")

    async def change_privacy_group(req):
        return await ajustar(req, "change-privacy-group", "status", "announce")

    async def group_pic(req):
        s = sessao(req)
        if not conectada(s):
            return erro("Disconnected", 400)
        campos, arquivo = {}, b""
        if req.content_type.startswith("multipart/"):
            leitor = await req.multipart()
            while True:
                parte = await leitor.next()
                if parte is None:
                    break
                if parte.filename is not None:
                    arquivo = await parte.read(decode=False)
                else:
                    campos[parte.name] = await parte.text()
        else:
            campos = await req.json()
        alvos = grupos_do_corpo(campos)
        if not alvos:
            return erro("groupId is required", 400)
        try:
            dados = arquivo or imagem_do_corpo(campos)
        except ValueError as e:
            return erro(e, 400)
        if not dados:
            return erro("the image is required", 400)
        try:
            jpeg = await executar(foto_para_jpeg, dados)
        except Exception:
            return erro("the file is not a valid image", 400)
        ids = []
        try:
            for alvo in alvos:
                ids.append(await executar(s.cliente.set_group_photo, jid_de_texto(alvo), jpeg))
        except Exception as e:
            return falha(e, "group-pic")
        return ok(status="success", response={"pictureId": ids[0] if len(ids) == 1 else ids})

    r.add_get("/api/{tok}/all-groups", all_groups)
    r.add_get("/api/{tok}/group-members/{groupId}", group_members)
    r.add_get("/api/{tok}/group-members-ids/{groupId}", group_members_ids)
    r.add_get("/api/{tok}/group-admins/{groupId}", group_admins)
    r.add_get("/api/{tok}/group-invite-link/{groupId}", group_invite_link)
    r.add_get("/api/{tok}/group-revoke-link/{groupId}", group_revoke_link)
    r.add_post("/api/{tok}/group-property", group_property)
    r.add_post("/api/{tok}/messages-admins-only", messages_admins_only)
    r.add_post("/api/{tok}/change-privacy-group", change_privacy_group)
    r.add_post("/api/{tok}/group-pic", group_pic)
