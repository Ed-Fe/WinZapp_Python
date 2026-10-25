"""Rotas de envio de tipos de mensagem que o cliente nao manda por send-file:
localizacao, link com previa, figurinha (e figurinha animada), imagem e arquivo
em base64 e nota de voz em base64. Todas respondem como `send-message`
(`guardar_enviada` do server.py), o formato que o WinZapp le.

Nada aqui foi testado contra uma conta real: o neonize nao instala fora do
Windows. As assinaturas conferem com o codigo-fonte do neonize 0.5.2
(send_message, build_sticker_message, build_audio_message).

As funcoes puras ficam no topo (testadas em test_conversas_envios.py);
`registrar` liga as rotas ao servidor.
"""
import base64
import binascii
import logging
import mimetypes
import re

log = logging.getLogger("ponte")

_ESQUEMA = re.compile(r"^[a-z][a-z0-9+.-]*://", re.IGNORECASE)
_DATA_URI = re.compile(r"^data:([^;,]*)(?:;[^,]*)?,", re.IGNORECASE)


def coordenadas(lat, lng):
    """(latitude, longitude) como numeros dentro dos limites, ou ValueError."""
    try:
        a, o = float(lat), float(lng)
    except (TypeError, ValueError):
        raise ValueError("lat and lng must be numbers") from None
    if not (-90 <= a <= 90 and -180 <= o <= 180):
        raise ValueError("lat/lng out of range")
    return a, o


def dados_de_base64(texto):
    """'data:<mime>;base64,<dados>' ou so os dados -> (bytes, mime). O mime
    e '' quando nao veio. ValueError se nao for base64."""
    texto = str(texto or "").strip()
    achado = _DATA_URI.match(texto)
    mime = achado.group(1).lower() if achado else ""
    if achado:
        texto = texto[achado.end():]
    try:
        dados = base64.b64decode(texto, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("invalid base64") from None
    if not dados:
        raise ValueError("empty file")
    return dados, mime


def fonte_do_arquivo(texto):
    """Os bytes que o neonize recebe como arquivo, de um base64 ou data URI.
    URL (http/https/ftp/file...) e caminho de disco sao recusados: o neonize
    baixa a URL e abre o caminho dentro da maquina do usuario (loopback, rede
    interna, metadados de nuvem; arquivos locais) e encaminha o resultado a um
    contato. O cliente nunca manda URL a estas rotas, entao nao ha o que
    preservar."""
    texto = str(texto or "").strip()
    if _ESQUEMA.match(texto):
        raise ValueError("url sources are not supported: send base64 or a data URI")
    return dados_de_base64(texto)


def tipo_do_arquivo(mime, nome=""):
    """'image' | 'video' | 'audio' | 'document', pelo mime ou, sem ele, pelo
    nome do arquivo."""
    mime = (mime or mimetypes.guess_type(nome or "")[0] or "").lower()
    base = mime.split("/")[0]
    return base if base in ("image", "video", "audio") else "document"


def com_grupo(fone, grupo):
    """Numero sem '@' vira JID de grupo quando a rota diz isGroup."""
    fone = fone[0] if isinstance(fone, list) else fone
    fone = str(fone or "")
    return fone + "@g.us" if grupo and "@" not in fone else fone


def registrar(r, *, sessao, ok, erro, destino_de, guardar_enviada, enviar_midia,
              contexto_citado, em_thread):
    """Liga as rotas de envio ao roteador `r` do aiohttp."""
    def conectada(s):
        return s.cliente is not None and s.status == "CONNECTED"

    async def enviar(req, nome, montar):
        """Corpo JSON -> `montar(s, corpo, destino)` (roda numa thread e devolve
        o SendResponse do neonize) -> resposta no formato send-message."""
        s = sessao(req)
        try:
            corpo = await req.json()
            destino = destino_de(com_grupo(corpo["phone"], corpo.get("isGroup")))
            preparado = montar(s, corpo, destino)
        except (KeyError, ValueError) as e:
            return erro("invalid request: %s" % e, 400)
        if not conectada(s):
            return erro("Disconnected", 400)
        try:
            resp = await em_thread(preparado)
        except Exception as e:
            log.exception(nome)
            return erro(e)
        return guardar_enviada(s, destino, resp)

    def citada(s, corpo):
        citado = corpo.get("quotedMessageId")
        return contexto_citado(s, citado) if citado else None

    async def send_location(req):
        def montar(s, corpo, destino):
            lat, lng = coordenadas(corpo["lat"], corpo["lng"])
            from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import LocationMessage, Message
            mensagem = Message(locationMessage=LocationMessage(
                degreesLatitude=lat, degreesLongitude=lng,
                name=str(corpo.get("title") or corpo.get("name") or ""),
                address=str(corpo.get("address") or "")))
            return lambda: s.cliente.send_message(destino, mensagem)
        return await enviar(req, "send-location", montar)

    async def send_link_preview(req):
        # O neonize gera o cartao buscando a pagina (e a imagem og:image) da
        # maquina do usuario, a partir de uma URL escolhida por quem chama: o
        # mesmo SSRF das figurinhas. O WinZapp nao usa esta rota.
        return erro("link previews are not available through neonize: it would fetch "
                    "the page from this machine", 501)

    def figurinha(animada):
        def montar_com(s, corpo, destino):
            fonte, _ = fonte_do_arquivo(corpo.get("path") or corpo["base64"])
            return lambda: s.cliente.send_sticker(destino, fonte, animated_gif=animada)
        return montar_com

    async def send_sticker(req):
        return await enviar(req, "send-sticker", figurinha(False))

    async def send_sticker_gif(req):
        return await enviar(req, "send-sticker-gif", figurinha(True))

    async def send_image(req):
        def montar(s, corpo, destino):
            dados, _ = dados_de_base64(corpo["base64"])
            ctx = citada(s, corpo)
            return lambda: enviar_midia(s, destino, "image", dados,
                                        corpo.get("caption") or corpo.get("message") or "",
                                        corpo.get("filename") or "", "", False, ctx)
        return await enviar(req, "send-image", montar)

    async def send_file_base64(req):
        def montar(s, corpo, destino):
            dados, mime = dados_de_base64(corpo["base64"])
            nome = corpo.get("filename") or ""
            mime = mime or mimetypes.guess_type(nome)[0] or ""
            ctx = citada(s, corpo)
            return lambda: enviar_midia(s, destino, tipo_do_arquivo(mime, nome), dados,
                                        corpo.get("caption") or corpo.get("message") or "",
                                        nome, mime, bool(corpo.get("isPtt")), ctx)
        return await enviar(req, "send-file-base64", montar)

    async def send_voice(req):
        def montar(s, corpo, destino):
            fonte, _ = fonte_do_arquivo(corpo["path"])
            ctx = citada(s, corpo)
            return lambda: enviar_midia(s, destino, "audio", fonte, "", "", "", True, ctx)
        return await enviar(req, "send-voice", montar)

    p = "/api/{tok}/"
    r.add_post(p + "send-location", send_location)
    r.add_post(p + "send-link-preview", send_link_preview)
    r.add_post(p + "send-sticker", send_sticker)
    r.add_post(p + "send-sticker-gif", send_sticker_gif)
    r.add_post(p + "send-image", send_image)
    r.add_post(p + "send-file-base64", send_file_base64)
    r.add_post(p + "send-voice", send_voice)
