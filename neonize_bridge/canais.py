"""Rotas de canais (newsletters) da ponte: criar um canal e silenciar/reativar o
som de um canal. O cliente do WinZapp nao chama nenhuma rota de canal (conversas
@newsletter sao ignoradas, ver CLAUDE.md), entao o formato das respostas segue o
das outras rotas da ponte: {"status": "success", "response": ...}.

Fora daqui, de proposito: `PUT /newsletter/:id` (editar) e `DELETE /newsletter/:id`
(apagar o canal). O neonize 0.5.2 nao tem funcao para nenhum dos dois (so
create/follow/unfollow/toggle_mute/info/mensagens); `unfollow_newsletter` deixa de
seguir, que nao e apagar o canal, entao nao foi usado no lugar.

As funcoes puras ficam no topo (testadas em test_canais.py); `registrar` liga as
rotas ao servidor.
"""
import asyncio
import base64
import binascii
import logging

from aiohttp import web

log = logging.getLogger("ponte")

SERVIDOR = "newsletter"
# Valores dos enums do neonize (NewsletterMuteState.ON, NewsletterThreadMetadata
# .VERIFIED), repetidos aqui para as funcoes puras nao importarem o neonize.
MUDO_ON = 1
VERIFICADO = 1


def id_de_canal(texto) -> str:
    """'123', '123@newsletter' -> '123@newsletter'. Vazio se nao for um canal
    (outro servidor, sem usuario): o neonize trataria um JID de pessoa como canal
    e o erro viria so do WhatsApp."""
    usuario, _, servidor = str(texto or "").strip().partition("@")
    if not usuario or servidor not in ("", SERVIDOR):
        return ""
    return "%s@%s" % (usuario, SERVIDOR)


def imagem_de_texto(texto) -> bytes:
    """Base64 (ou data URL) -> bytes; b'' se nao veio imagem.

    Nunca devolve `str`: o neonize trata uma `str` como caminho de arquivo ou URL
    e leria um arquivo qualquer do disco. Levanta ValueError se nao for base64."""
    texto = str(texto or "").strip()
    if not texto:
        return b""
    if texto.startswith("data:"):
        texto = texto.partition(",")[2]
    try:
        return base64.b64decode(texto, validate=True)
    except (binascii.Error, ValueError) as erro:
        raise ValueError("picture must be base64") from erro


def pedido_de_criacao(corpo) -> dict:
    """Corpo de `POST /newsletter` -> {name, description, picture(bytes)}.
    A imagem pode vir em `picture` ou em `options.picture`. Levanta ValueError
    sem nome ou com imagem que nao e base64."""
    corpo = corpo if isinstance(corpo, dict) else {}
    nome = str(corpo.get("name") or "").strip()
    if not nome:
        raise ValueError("name is required")
    opcoes = corpo.get("options")
    opcoes = opcoes if isinstance(opcoes, dict) else {}
    imagem = corpo.get("picture") or opcoes.get("picture")
    descricao = corpo.get("description")
    if descricao is None:
        descricao = opcoes.get("description")
    return {"name": nome, "description": str(descricao or ""),
            "picture": imagem_de_texto(imagem)}


def silenciar_de(corpo) -> bool:
    """Corpo de `POST /mute-newsletter/:id` -> True para silenciar, False para
    reativar o som. Aceita `mute`, `value` ou `muted`; sem nada, silencia."""
    if isinstance(corpo, dict):
        for chave in ("mute", "value", "muted"):
            if chave in corpo:
                valor = corpo[chave]
                if isinstance(valor, str):
                    return valor.strip().lower() not in ("false", "0", "no", "off", "")
                return bool(valor)
    return True


def canal_para_wpp(meta, jid_texto) -> dict:
    """NewsletterMetadata do neonize -> o dicionario de resposta de um canal.
    `jid_texto` converte um JID do neonize em 'usuario@servidor'."""
    thread, viewer = meta.ThreadMeta, meta.ViewerMeta
    return {
        "id": jid_texto(meta.ID),
        "name": thread.Name.Text,
        "description": thread.Description.Text,
        "inviteCode": thread.InviteCode,
        "creationTime": int(thread.CreationTime or 0),
        "subscribersCount": int(thread.SubscriberCount or 0),
        "verified": thread.VerificationState == VERIFICADO,
        "muted": viewer.Mute == MUDO_ON,
    }


def canal_nao_achado(erro: Exception) -> bool:
    texto = str(erro).lower()
    return "not-found" in texto or "not found" in texto or "404" in texto


def registrar(r, *, sessao, ok, jid_de_texto, jid_texto):
    """Liga as rotas de canal ao roteador `r` do aiohttp."""

    async def executar(funcao, *args):
        return await asyncio.get_running_loop().run_in_executor(None, funcao, *args)

    def erro(mensagem, status=500):
        return web.json_response({"status": "error", "message": str(mensagem)}, status=status)

    def conectada(s):
        return s.cliente is not None and s.status == "CONNECTED"

    async def corpo_json(req):
        try:
            return await req.json()
        except ValueError:
            return {}

    async def criar_canal(req):
        s = sessao(req)
        corpo = await corpo_json(req)
        if not conectada(s):
            return erro("Disconnected", 400)
        try:
            pedido = pedido_de_criacao(corpo)
        except ValueError as e:
            return erro(e, 400)
        try:
            meta = await executar(
                s.cliente.create_newsletter, pedido["name"], pedido["description"],
                pedido["picture"])
        except Exception as e:
            log.exception("newsletter (criar)")
            return erro(e)
        return web.json_response(
            {"status": "success", "response": canal_para_wpp(meta, jid_texto)}, status=201)

    async def silenciar_canal(req):
        s = sessao(req)
        corpo = await corpo_json(req)
        if not conectada(s):
            return erro("Disconnected", 400)
        canal = id_de_canal(req.match_info["id"])
        if not canal:
            return erro("not a newsletter id", 400)
        silenciar = silenciar_de(corpo)
        try:
            await executar(
                s.cliente.newsletter_toggle_mute, jid_de_texto(canal), silenciar)
        except Exception as e:
            if canal_nao_achado(e):
                return erro("newsletter not found", 404)
            log.exception("mute-newsletter")
            return erro(e)
        return ok(status="success", response={"id": canal, "muted": silenciar})

    r.add_post("/api/{tok}/newsletter", criar_canal)
    r.add_post("/api/{tok}/mute-newsletter/{id}", silenciar_canal)
