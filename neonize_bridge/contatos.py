"""Rotas de contatos da ponte: bloquear/desbloquear, consultar se um numero esta
no WhatsApp, salvar na agenda e enviar cartao de contato. Os formatos de
resposta sao os que o cliente do WinZapp le (core/phone_contacts.py,
main_window/chat_actions.py, main_window/sending.py) e os que o servidor de
referencia monta (api_patches/src/controller/contactSaveController.ts,
deviceController.ts, middleware/statusConnection.ts).

As funcoes puras ficam no topo (testadas em test_contatos.py); `registrar` liga
as rotas ao servidor. `remove-contact` nao esta aqui: o aviso de estado do
neonize (MutationInfo) so sabe gravar, nao apagar uma entrada da agenda.
"""
import asyncio
import logging
import re
import time

from aiohttp import web

from grupos import usuario_de

log = logging.getLogger("ponte")

# Codigos de erro que o cliente entende (core/phone_contacts.py::_error_of).
CODIGOS_CONHECIDOS = (
    "contact_not_available", "contact_invalid", "contact_name_required",
    "contact_lid_without_phone", "contact_operation_failed",
)

# Um contato so tem estas duas formas de id (o servidor de referencia barra o
# resto: grupo, lista de transmissao, canal, texto livre).
_WID = re.compile(r"^\d{7,20}@(c\.us|lid)$")

_MINIMO_DIGITOS = 7
_LIMITE_NOME = 100

# Colecao do estado do aplicativo onde o WhatsApp guarda a agenda.
COLECAO_CONTATOS = "critical_unblock_low"
INDICE_CONTATO = "contact"
VERSAO_CONTATO = 2


def alvo_de_contato(texto) -> str:
    """O que o cliente mandou em `phone` -> 'usuario@servidor' do whatsmeow, ou ''
    se nao e uma pessoa (grupo, lista de transmissao, texto livre).

    Numeros viram '@s.whatsapp.net'; um '@lid' continua '@lid' (os digitos de um
    LID nao sao um telefone)."""
    usuario, arroba, servidor = str(texto or "").strip().partition("@")
    usuario = usuario.split(":")[0]
    if not usuario.isdigit():
        return ""
    if servidor == "lid":
        return f"{usuario}@lid"
    if arroba and servidor not in ("c.us", "s.whatsapp.net"):
        return ""
    return f"{usuario}@s.whatsapp.net" if len(usuario) >= _MINIMO_DIGITOS else ""


def primeiro_destino(valor):
    """O campo `phone` chega como texto ou como lista com um destino."""
    return valor[0] if isinstance(valor, list) and valor else valor


def formas_do_numero(digitos: str) -> list:
    """O numero e, para um celular brasileiro, tambem a outra forma dele (com ou
    sem o 9o digito): o WhatsApp so conhece uma das duas."""
    formas = [digitos]
    if digitos.startswith("55"):
        if len(digitos) == 13 and digitos[4] == "9":
            formas.append(digitos[:4] + digitos[5:])
        elif len(digitos) == 12 and digitos[4] in "6789":
            formas.append(digitos[:4] + "9" + digitos[4:])
    return formas


def achado_na_consulta(respostas):
    """IsOnWhatsAppResponse do neonize -> {'jid': 'x@s.whatsapp.net', 'negocio': bool}
    para o primeiro numero que o WhatsApp conhece, ou None se nenhum."""
    for item in respostas or ():
        if item.IsIn and item.JID.User:
            return {"jid": f"{item.JID.User}@{item.JID.Server or 's.whatsapp.net'}",
                    "negocio": item.HasField("VerifiedName")}
    return None


def _id_wpp(usuario: str, servidor: str = "c.us") -> dict:
    return {"server": servidor, "user": usuario, "_serialized": f"{usuario}@{servidor}"}


def resposta_do_numero(digitos: str, achado) -> dict:
    """O corpo de `check-number-status`. `numberExists` e `id._serialized` sao os
    campos que o servidor de referencia le (middleware/statusConnection.ts); o id
    e o canonico, que pode diferir do digitado (9o digito)."""
    if achado is None:
        return {"id": _id_wpp(digitos), "status": 404, "isBusiness": False,
                "canReceiveMessage": False, "numberExists": False}
    usuario, _, servidor = achado["jid"].partition("@")
    servidor = "c.us" if servidor == "s.whatsapp.net" else servidor
    return {"id": _id_wpp(usuario.split(":")[0], servidor), "status": 200,
            "isBusiness": bool(achado.get("negocio")), "canReceiveMessage": True,
            "numberExists": True}


def para_wid(telefone) -> str:
    """O id do contato em `save-contact`: so digitos viram 'digitos@c.us'; um id
    inteiro ('...@lid') fica como veio. '' se nao e um contato."""
    bruto = str(primeiro_destino(telefone) or "").strip()
    wid = bruto if "@" in bruto else re.sub(r"\D", "", bruto) + "@c.us"
    wid = wid.replace("@s.whatsapp.net", "@c.us")
    return wid if _WID.match(wid) else ""


def validar_salvar(corpo) -> tuple:
    """Corpo de `save-contact` -> (campos, codigo_de_erro). Campos: wid, primeiro,
    sobrenome, sincronizar. A ordem das checagens e a do servidor de referencia."""
    corpo = corpo if isinstance(corpo, dict) else {}
    wid = para_wid(corpo.get("phone"))
    if not wid:
        return None, "contact_invalid"
    primeiro = str(corpo.get("name") or "").strip()[:_LIMITE_NOME]
    if not primeiro:
        return None, "contact_name_required"
    return {"wid": wid, "primeiro": primeiro,
            "sobrenome": str(corpo.get("lastName") or "").strip()[:_LIMITE_NOME],
            # sincronizado com o celular, a nao ser que o chamador diga que nao
            "sincronizar": corpo.get("syncAddressBook") is not False}, ""


def nome_completo(primeiro: str, sobrenome: str) -> str:
    return f"{primeiro} {sobrenome}".strip()


def campos_da_agenda(campos: dict, fone_jid: str, lid_jid: str = "") -> dict:
    """Os campos do ContactAction (aviso de estado) que salva o contato."""
    acao = {"fullName": nome_completo(campos["primeiro"], campos["sobrenome"]),
            "firstName": campos["primeiro"],
            "saveOnPrimaryAddressbook": bool(campos["sincronizar"]),
            "pnJID": fone_jid}
    if lid_jid:
        acao["lidJID"] = lid_jid
    return acao


def erro_de_contato(codigo: str) -> tuple:
    """(status HTTP, corpo) de um erro de contato; um codigo desconhecido nunca
    leva texto de excecao (pode ter nome e numero)."""
    codigo = codigo if codigo in CODIGOS_CONHECIDOS else "contact_operation_failed"
    status = {"contact_not_available": 503, "contact_operation_failed": 502}.get(codigo, 400)
    return status, {"status": "error", "code": codigo}


def resposta_de_salvar(fone_jid: str, sincronizar: bool) -> dict:
    """O `response` de `save-contact`: o id sob o qual o contato foi salvo
    (o cliente o usa no lugar dos digitos digitados)."""
    usuario, _, servidor = fone_jid.partition("@")
    servidor = "c.us" if servidor == "s.whatsapp.net" else servidor
    return {"id": f"{usuario.split(':')[0]}@{servidor}", "isMyContact": True,
            "syncToAddressbook": bool(sincronizar)}


def _escapar_vcard(texto: str) -> str:
    texto = " ".join(str(texto).split())
    return texto.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")


def vcard_de(nome: str, numero: str) -> str:
    """O cartao vCard 3.0 que o WhatsApp mostra como contato; `waid` e o que o
    liga ao numero no WhatsApp."""
    nome = _escapar_vcard(nome)
    return ("BEGIN:VCARD\nVERSION:3.0\n"
            f"N:;{nome};;;\nFN:{nome}\n"
            f"TEL;type=CELL;waid={numero}:+{numero}\n"
            "END:VCARD")


def nome_do_cartao(achado: dict, numero: str, pedido="") -> str:
    """Nome do cartao: o que o chamador pediu, senao o da agenda, senao o apelido
    publico, senao o proprio numero."""
    return (str(pedido or "").strip() or achado.get("name") or achado.get("pushname")
            or numero)


def nome_do_conjunto(nomes) -> str:
    """displayName de um cartao com varios contatos."""
    return ", ".join(nomes)


def registrar(r, *, sessao, ok, jid_de_texto, jid_texto, destino_de, guardar_enviada,
              contexto_citado):
    """Liga as rotas de contato ao roteador `r` do aiohttp."""

    async def executar(funcao, *args):
        return await asyncio.get_running_loop().run_in_executor(None, funcao, *args)

    def erro(mensagem, status=500):
        return web.json_response({"status": "error", "message": str(mensagem)}, status=status)

    def erro_codigo(codigo):
        status, corpo = erro_de_contato(codigo)
        return web.json_response(corpo, status=status)

    def conectada(s):
        return s.cliente is not None and s.status == "CONNECTED"

    async def consultar_numero(s, digitos):
        """O achado do WhatsApp para o numero (ou para a outra forma dele), ou None."""
        respostas = await executar(
            s.cliente.is_on_whatsapp, *["+" + f for f in formas_do_numero(digitos)])
        return achado_na_consulta(respostas)

    async def mudar_bloqueio(req, bloquear):
        from neonize.utils.enum import BlocklistAction
        s = sessao(req)
        corpo = await req.json()
        if not conectada(s):
            return erro("Disconnected", 400)
        alvo = alvo_de_contato(primeiro_destino(corpo.get("phone")))
        if not alvo:
            return erro("invalid contact", 400)
        acao = BlocklistAction.BLOCK if bloquear else BlocklistAction.UNBLOCK
        jid = jid_de_texto(alvo)
        try:
            try:
                await executar(s.cliente.update_blocklist, jid, acao)
            except Exception:
                if not alvo.endswith("@s.whatsapp.net"):
                    raise
                # um numero que o WhatsApp so conhece pelo LID: tenta por ele
                lid = await executar(s.cliente.get_lid_from_pn, jid)
                if not getattr(lid, "User", ""):
                    raise
                await executar(s.cliente.update_blocklist, lid, acao)
        except Exception:
            log.exception("block-contact" if bloquear else "unblock-contact")
            return erro("Error on block contact" if bloquear else "Error on unblock contact")
        return ok(status="success", response={
            "message": "Contact blocked" if bloquear else "Contact UnBlocked"})

    async def block_contact(req):
        return await mudar_bloqueio(req, True)

    async def unblock_contact(req):
        return await mudar_bloqueio(req, False)

    async def check_number_status(req):
        s = sessao(req)
        if not conectada(s):
            return erro("Disconnected", 400)
        digitos = usuario_de(req.match_info["phone"])
        if not digitos.isdigit() or len(digitos) < _MINIMO_DIGITOS:
            return ok(status="success", response=resposta_do_numero(digitos, None))
        try:
            achado = await consultar_numero(s, digitos)
        except Exception:
            log.exception("check-number-status")
            return erro("Error on check number status")
        return ok(status="success", response=resposta_do_numero(digitos, achado))

    def enviar_agenda(s, fone_jid, acao):
        """Grava o contato na agenda do WhatsApp (e, com `saveOnPrimaryAddressbook`,
        na do celular) por um aviso de estado. Roda numa thread. Nao usa
        Sessao.enviar_estado: ele so repara as colecoes 'regular'."""
        from neonize.proto.Neonize_pb2 import MutationInfo, PatchInfo
        from neonize.proto.waSyncAction.WAWebProtobufSyncAction_pb2 import (
            ContactAction, SyncActionValue)
        patch = PatchInfo(
            Timestamp=time.time_ns(), Type=PatchInfo.CRITICAL_UNBLOCK_LOW,
            Mutations=[MutationInfo(
                Index=[INDICE_CONTATO, fone_jid], Version=VERSAO_CONTATO,
                Value=SyncActionValue(contactAction=ContactAction(**acao)))])
        try:
            s.cliente.send_app_state(patch)
        except Exception as e:
            if "LTHash" not in str(e) and "409" not in str(e):
                raise
            # colecao fora de sincronia: baixa de novo e tenta mais uma vez
            if not s.reparar_colecao(COLECAO_CONTATOS):
                raise
            s.cliente.send_app_state(patch)

    async def save_contact(req):
        s = sessao(req)
        campos, codigo = validar_salvar(await req.json())
        if codigo:
            return erro_codigo(codigo)
        if not conectada(s):
            return erro_codigo("contact_not_available")
        wid = campos["wid"]
        lid = ""
        if wid.endswith("@lid"):
            # os digitos de um LID nao sao um telefone: precisa do numero por tras
            lid = wid
            try:
                digitos = (await executar(s.cliente.get_pn_from_lid, jid_de_texto(wid))).User
            except Exception:
                digitos = ""
            if not digitos:
                return erro_codigo("contact_lid_without_phone")
        else:
            digitos = usuario_de(wid)
        try:
            achado = await consultar_numero(s, digitos)
        except Exception:
            log.exception("save-contact: consulta do numero")
            # uma consulta que falhou nao prova que o numero nao existe; 408 e o
            # que o cliente repete (o servidor de referencia responde igual)
            return web.json_response(
                {"response": None, "status": "Connected",
                 "message": "Não foi possível consultar o número."}, status=408)
        if achado is None:
            return web.json_response(
                {"response": None, "status": "Connected",
                 "message": f"O número {digitos} não existe."}, status=400)
        fone_jid = achado["jid"]
        if not lid:
            try:
                achado_lid = await executar(s.cliente.get_lid_from_pn, jid_de_texto(fone_jid))
                lid = jid_texto(achado_lid)
            except Exception:
                lid = ""        # o LID e so um complemento; o aviso vale sem ele
        acao = campos_da_agenda(campos, fone_jid, lid)
        try:
            await executar(enviar_agenda, s, fone_jid, acao)
        except Exception:
            log.exception("save-contact")
            return erro_codigo("contact_operation_failed")
        # a agenda local da ponte (all-contacts) e a loja do neonize passam a ter o nome
        s.bd.contato(fone_jid, name=acao["fullName"])
        try:
            await executar(s.cliente.contact.put_contact_name, jid_de_texto(fone_jid),
                           acao["fullName"], acao["firstName"])
        except Exception:
            log.warning("save-contact: loja de contatos do neonize nao atualizada")
        return ok(status="success",
                  response=resposta_de_salvar(fone_jid, campos["sincronizar"]))

    async def contact_vcard(req):
        from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import (
            ContactMessage, ContactsArrayMessage, Message)
        s = sessao(req)
        corpo = await req.json()
        if not conectada(s):
            return erro("Disconnected", 400)
        fone = primeiro_destino(corpo.get("phone"))
        if not fone:
            return erro("phone is required", 400)
        if corpo.get("isGroup") and "@" not in fone:
            fone += "@g.us"
        destino = destino_de(fone)
        ids = [i for i in corpo.get("contactsId") or [] if i]
        if not ids:
            return erro("contactsId is required", 400)
        cartoes = []
        for contato in ids:
            alvo = alvo_de_contato(contato)
            if not alvo:
                return erro("invalid contact", 400)
            numero = usuario_de(alvo)
            if alvo.endswith("@lid"):
                try:
                    numero = (await executar(s.cliente.get_pn_from_lid,
                                             jid_de_texto(alvo))).User
                except Exception:
                    numero = ""
                if not numero:
                    return erro("contact has no phone number yet", 400)
            achado = s.bd.contato_de(f"{numero}@s.whatsapp.net")
            nome = nome_do_cartao(achado, numero, corpo.get("name") if len(ids) == 1 else "")
            cartoes.append(ContactMessage(displayName=nome, vcard=vcard_de(nome, numero)))
        citado = corpo.get("quotedMessageId")
        ctx = None
        if citado:
            ctx = contexto_citado(s, citado)
            if ctx is None:
                return erro("quoted message not found", 404)
        if len(cartoes) == 1:
            mensagem = Message(contactMessage=cartoes[0])
            if ctx is not None:
                mensagem.contactMessage.contextInfo.CopyFrom(ctx)
        else:
            mensagem = Message(contactsArrayMessage=ContactsArrayMessage(
                displayName=nome_do_conjunto([c.displayName for c in cartoes]),
                contacts=cartoes))
            if ctx is not None:
                mensagem.contactsArrayMessage.contextInfo.CopyFrom(ctx)
        try:
            resp = await executar(s.cliente.send_message, destino, mensagem)
        except Exception:
            log.exception("contact-vcard")
            return erro("Error on send contact vcard")
        return guardar_enviada(s, destino, resp)

    r.add_post("/api/{tok}/block-contact", block_contact)
    r.add_post("/api/{tok}/unblock-contact", unblock_contact)
    r.add_get("/api/{tok}/check-number-status/{phone}", check_number_status)
    r.add_post("/api/{tok}/save-contact", save_contact)
    r.add_post("/api/{tok}/contact-vcard", contact_vcard)
