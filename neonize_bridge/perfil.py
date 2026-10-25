"""Rotas de perfil e privacidade da ponte: o proprio perfil (nome, recado, foto),
foto e recado de outras pessoas e os ajustes de privacidade da conta. Os
formatos de resposta sao os que o cliente do WinZapp le (core/whatsapp_profile.py,
core/whatsapp_privacy.py, core/profile_picture.py, main_window/identity.py).

As funcoes puras ficam no topo (testadas em test_perfil.py); `registrar` liga as
rotas ao servidor. Nada aqui foi provado contra uma conta real.
"""
import asyncio
import logging
import re

from aiohttp import web

import msgmap

log = logging.getLogger("ponte")

# Uma foto de perfil e pequena; acima disso nao e uma.
FOTO_MAXIMA = 10 * 1024 * 1024

# Ajustes da conta no vocabulario do WinZapp (o mesmo de privacyController.ts) ->
# (nome em neonize.utils.enum.PrivacySettingType, valores aceitos).
AJUSTES = {
    "lastSeen": ("LAST_SEEN", ("all", "contacts", "none")),
    "online": ("ONLINE", ("all", "match_last_seen")),
    "about": ("STATUS", ("all", "contacts", "none")),      # "status" e o recado
    "profilePicture": ("PROFILE", ("all", "contacts", "none")),
    "readReceipts": ("READ_RECEIPTS", ("all", "none")),
    "groupAdd": ("GROUP_ADD", ("all", "contacts")),
}

# PrivacySettings.PrivacySetting do proto do neonize -> texto do WhatsApp Web.
# 1 (UNDEFINED) fica de fora: o cliente le null como "valor que nao conheco".
VALORES_PRIVACIDADE = {2: "all", 3: "contacts", 4: "contact_blacklist",
                       5: "match_last_seen", 6: "known", 7: "none"}

# Campo de PrivacySettings -> ajuste do WinZapp.
CAMPOS_PRIVACIDADE = {"LastSeen": "lastSeen", "Online": "online", "Status": "about",
                      "Profile": "profilePicture", "ReadReceipts": "readReceipts",
                      "GroupAdd": "groupAdd"}

_ID_DE_PESSOA = re.compile(r"^\d+(?:@(?:c\.us|s\.whatsapp\.net|lid))?$")


def jid_do_perfil(phone: str, eh_grupo: bool = False) -> str:
    """O id que as rotas de perfil recebem ('5511999', '5511999@c.us', 'x@lid')
    -> 'usuario@servidor' do whatsmeow. Com `isGroup=true` o numero e de grupo."""
    phone = str(phone or "")
    if "@" in phone:
        return msgmap.jid_whatsmeow(phone)
    return phone + ("@g.us" if eh_grupo else "@s.whatsapp.net")


def id_de_pessoa_valido(texto: str) -> bool:
    """So uma pessoa: nao grupo, nem canal, nem expressao qualquer."""
    return bool(_ID_DE_PESSOA.match(str(texto or "")))


def ajuste_valido(ajuste, valor) -> bool:
    """O ajuste existe e o valor esta entre os que o WhatsApp aceita para ele."""
    return (isinstance(ajuste, str) and isinstance(valor, str)
            and ajuste in AJUSTES and valor in AJUSTES[ajuste][1])


def ajustes_para_wpp(privacidade) -> dict:
    """PrivacySettings do neonize -> `settings` de `POST /privacy`. Um valor que
    o WhatsApp nao informou (ou que nao se conhece) vai como None."""
    return {ajuste: VALORES_PRIVACIDADE.get(int(getattr(privacidade, campo, 0) or 0))
            for campo, ajuste in CAMPOS_PRIVACIDADE.items()}


def sem_foto(erro: Exception) -> bool:
    """A pessoa/grupo nao tem foto ou a escondeu de nos: resposta definitiva do
    servidor, nao uma falha."""
    texto = str(erro).lower()
    return ("profile picture" in texto or "item-not-found" in texto
            or "not-authorized" in texto or "unauthorized" in texto)


def foto_para_wpp(info, jid: str):
    """ProfilePictureInfo -> o registro que o cliente le (`imgFull`, `img`,
    `eurl`, `id`, `tag`); None quando nao ha URL."""
    url = getattr(info, "URL", "") or ""
    if not url:
        return None
    return {"eurl": url, "img": url, "imgFull": url, "id": jid,
            "tag": getattr(info, "ID", "") or ""}


def recado_de(informacoes) -> str:
    """O recado ('Sobre') da primeira pessoa da resposta de get_user_info."""
    for item in informacoes or ():
        texto = getattr(getattr(item, "UserInfo", None), "Status", "") or ""
        if texto:
            return texto
    return ""


def e_jpeg(dados: bytes) -> bool:
    """O whatsmeow so envia foto de perfil em JPEG."""
    return bytes(dados[:3]) == b"\xff\xd8\xff"


def foto_de_perfil(dados: bytes) -> bytes:
    """A foto no unico formato que o whatsmeow envia (JPEG). O cliente tambem
    deixa escolher PNG (core/whatsapp_profile.py): converte, como na foto de
    grupo. JPEG passa como veio; um arquivo que nao e imagem levanta excecao."""
    if e_jpeg(dados):
        return dados
    from grupos_extra import foto_para_jpeg
    return foto_para_jpeg(dados)


def perfil_proprio(nome, recado, tem_foto) -> dict:
    """`{name, status, photo}` de `my-profile`: o que nao se leu vai como None
    (o cliente mostra o resto), e o texto vazio tambem e 'nao sei'."""
    def texto(valor):
        return valor.strip() or None if isinstance(valor, str) else None
    return {"name": texto(nome), "status": texto(recado),
            "photo": tem_foto if isinstance(tem_foto, bool) else None}


def registrar(r, *, sessao, ok, jid_de_texto, jid_texto):
    """Liga as rotas de perfil e privacidade ao roteador `r` do aiohttp."""

    async def executar(funcao, *args):
        return await asyncio.get_running_loop().run_in_executor(None, funcao, *args)

    def erro(mensagem, status=500):
        return web.json_response({"status": "error", "message": str(mensagem)}, status=status)

    def erro_codigo(codigo, status, **extra):
        # privacy/* responde por `code` (e `kind`), como o privacyController.ts;
        # o cliente nunca ve o texto da excecao, que pode ter numero de telefone.
        return web.json_response({"status": "error", "code": codigo, **extra}, status=status)

    def conectada(s):
        return s.cliente is not None and s.status == "CONNECTED"

    async def my_profile(req):
        s = sessao(req)
        if not conectada(s):
            return erro_codigo("profile_unavailable", 503)
        eu = jid_de_texto(s.eu)
        nome = recado = foto = None
        try:
            nome = (await executar(s.cliente.get_me)).PushName
        except Exception as e:
            log.info("my-profile: nome nao lido (%s)", type(e).__name__)
        try:
            recado = recado_de(await executar(s.cliente.get_user_info, eu))
        except Exception as e:
            log.info("my-profile: recado nao lido (%s)", type(e).__name__)
        try:
            foto = foto_para_wpp(await executar(s.cliente.get_profile_picture, eu), "") is not None
        except Exception as e:
            # sem foto e uma resposta; qualquer outra falha deixa "nao sei"
            foto = False if sem_foto(e) else None
        return ok(status="success", response=perfil_proprio(nome, recado, foto))

    async def change_username(req):
        s = sessao(req)
        corpo = await req.json()
        nome = corpo.get("name")
        if not isinstance(nome, str) or not nome.strip():
            return erro("name is required", 400)
        if not conectada(s):
            return erro("Disconnected", 400)
        try:
            await executar(s.cliente.set_profile_name, nome.strip())
        except Exception as e:
            log.warning("change-username: %s", type(e).__name__)
            return erro(e)
        return ok(status="success", response={})

    async def set_profile_status(req):
        s = sessao(req)
        corpo = await req.json()
        texto = corpo.get("status")
        if not isinstance(texto, str):
            return erro("status is required", 400)
        if not conectada(s):
            return erro("Disconnected", 400)
        try:
            await executar(s.cliente.set_status_message, texto)
        except Exception as e:
            log.warning("profile-status: %s", type(e).__name__)
            return erro(e)
        return ok(status="success", response={})

    async def get_profile_status(req):
        s = sessao(req)
        if not conectada(s):
            return erro("Disconnected", 400)
        jid = jid_de_texto(jid_do_perfil(req.match_info["phone"]))
        try:
            informacoes = await executar(s.cliente.get_user_info, jid)
        except Exception as e:
            log.info("profile-status: %s", type(e).__name__)
            return erro(e)
        return ok(status="success", response={
            "id": msgmap.jid_wpp(jid_texto(jid)), "status": recado_de(informacoes)})

    async def profile_pic(req):
        s = sessao(req)
        if not conectada(s):
            return erro("Disconnected", 400)
        grupo = req.query.get("isGroup", "").lower() == "true"
        jid = jid_de_texto(jid_do_perfil(req.match_info["phone"], grupo))
        try:
            info = await executar(s.cliente.get_profile_picture, jid)
        except Exception as e:
            if sem_foto(e):
                return ok(status="success", response=None)
            log.info("profile-pic: %s", type(e).__name__)
            return erro(e)
        return ok(status="success",
                  response=foto_para_wpp(info, msgmap.jid_wpp(jid_texto(jid))))

    async def set_profile_pic(req):
        s = sessao(req)
        if not conectada(s):
            return erro("Disconnected", 400)
        dados = None
        leitor = await req.multipart()
        while True:
            parte = await leitor.next()
            if parte is None:
                break
            if parte.name == "file" and parte.filename is not None:
                dados = bytes(await parte.read(decode=False))
                break
        if not dados:
            return erro("file is required", 400)
        if len(dados) > FOTO_MAXIMA:
            return erro("photo is too large", 413)
        try:
            jpeg = await executar(foto_de_perfil, dados)
        except Exception as e:
            log.info("set-profile-pic: imagem nao convertida (%s)", type(e).__name__)
            return erro("unsupported image", 415)
        try:
            await executar(s.cliente.set_profile_photo, jpeg)
        except Exception as e:
            log.warning("set-profile-pic: %s", type(e).__name__)
            return erro(e)
        return ok(status="success", response={})

    async def get_phone_number(req):
        s = sessao(req)
        if not conectada(s) or not s.eu:
            return erro("Disconnected", 400)
        return ok(status="success", response=msgmap.jid_wpp(s.eu), mapper="device")

    async def last_seen(req):
        # A resposta vem da memoria de presenca (presenca.py), alimentada pelos
        # eventos; "unsupported" e o que o cliente entende como "sem dado ou
        # escondido", sem insistir.
        if not id_de_pessoa_valido(req.match_info["phone"]):
            return web.json_response(
                {"status": "error", "response": "Invalid contact id"}, status=400)
        import time

        import presenca
        memoria = getattr(sessao(req), "presencas", None)
        dado = memoria.ler(req.match_info["phone"], time.time()) if memoria else None
        return ok(status="success", **presenca.resposta_last_seen(dado))

    async def privacy(req):
        s = sessao(req)
        if not conectada(s):
            return erro_codigo("privacy_unavailable", 503)
        try:
            privacidade = await executar(s.cliente.get_privacy_settings)
        except Exception as e:
            log.info("privacy: %s", type(e).__name__)
            return erro_codigo("privacy_unavailable", 503)
        # `status` (quem ve meus Status) fica de fora: o neonize le a lista mas
        # nao tem como grava-la, e o cliente esconde a escolha quando vem null.
        return ok(status="success", response={
            "settings": ajustes_para_wpp(privacidade), "status": None})

    async def privacy_set(req):
        s = sessao(req)
        corpo = await req.json()
        ajuste, valor = corpo.get("setting"), corpo.get("value")
        if not ajuste_valido(ajuste, valor):
            return erro_codigo("invalid_privacy_setting", 400)
        if not conectada(s):
            return erro_codigo("privacy_unavailable", 503)
        from neonize.utils.enum import PrivacySetting, PrivacySettingType
        try:
            await executar(s.cliente.set_privacy_setting,
                           PrivacySettingType[AJUSTES[ajuste][0]], PrivacySetting(valor))
        except Exception as e:
            log.warning("privacy/set: %s", type(e).__name__)
            return erro_codigo("privacy_setting_failed", 502, kind=type(e).__name__)
        return ok(status="success")

    r.add_get("/api/{tok}/my-profile", my_profile)
    r.add_post("/api/{tok}/change-username", change_username)
    r.add_post("/api/{tok}/profile-status", set_profile_status)
    r.add_get("/api/{tok}/profile-status/{phone}", get_profile_status)
    r.add_get("/api/{tok}/profile-pic/{phone}", profile_pic)
    r.add_post("/api/{tok}/set-profile-pic", set_profile_pic)
    r.add_get("/api/{tok}/get-phone-number", get_phone_number)
    r.add_get("/api/{tok}/last-seen/{phone}", last_seen)
    r.add_post("/api/{tok}/privacy", privacy)
    r.add_post("/api/{tok}/privacy/set", privacy_set)
