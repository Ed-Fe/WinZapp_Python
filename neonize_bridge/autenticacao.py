"""Quem pode falar com a porta local da ponte.

A ponte guarda a sessao do WhatsApp do usuario e escuta em 127.0.0.1. Isso nao
basta: uma pagina aberta no navegador tambem alcanca 127.0.0.1. Por isso:

* a ponte tem um segredo proprio, gerado na primeira vez e guardado em
  `<pasta>/segredo`; o cliente le o mesmo arquivo (e ele que faz o papel da
  "chave secreta" do WPPConnect);
* `generate-token` so responde a quem sabe o segredo, e o token de uma sessao
  e um HMAC do nome da sessao com esse segredo;
* toda rota `/api/<token>/...` confere esse token; as rotas de administracao
  (`show-all-sessions`, `start-all`) pedem o proprio segredo no lugar do token;
* o cabecalho Host tem de ser o da propria maquina (contra DNS rebinding).

Funcoes puras, testadas em test_autenticacao.py. A pagina de outro site nao
conhece o segredo nem pode ler o arquivo, e o navegador nao a deixa mandar o
cabecalho Authorization sem um preflight que a ponte nao responde.
"""
import hashlib
import hmac
import os
import secrets

from aiohttp import web

ARQUIVO_DO_SEGREDO = "segredo"
HOSTS_PERMITIDOS = ("127.0.0.1", "localhost", "[::1]")
ROTAS_ABERTAS = ("/healthz", "/winzapp/identity", "/api-docs", "/metrics")
ROTAS_DE_ADMINISTRACAO = ("show-all-sessions", "start-all")


def carregar_segredo(pasta) -> str:
    """O segredo da ponte: o do arquivo, ou um novo (so o dono pode le-lo)."""
    caminho = os.path.join(pasta, ARQUIVO_DO_SEGREDO)
    try:
        with open(caminho, encoding="utf-8") as arquivo:
            segredo = arquivo.read().strip()
        if segredo:
            return segredo
    except OSError:
        pass
    os.makedirs(pasta, exist_ok=True)
    segredo = secrets.token_hex(32)
    descritor = os.open(caminho, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descritor, "w", encoding="utf-8") as arquivo:
        arquivo.write(segredo)
    return segredo


def token_da_sessao(segredo: str, nome: str) -> str:
    return hmac.new(segredo.encode(), nome.encode(), hashlib.sha256).hexdigest()


def segredo_valido(segredo: str, candidato) -> bool:
    return bool(candidato) and hmac.compare_digest(str(candidato), segredo)


def credencial_valida(segredo: str, token) -> bool:
    """`token` e `<sessao>:<hash>`; vale se o hash e o desta sessao."""
    nome, separador, hash_recebido = str(token or "").partition(":")
    if not (nome and separador and hash_recebido):
        return False
    return hmac.compare_digest(hash_recebido, token_da_sessao(segredo, nome))


def host_permitido(cabecalho) -> bool:
    """Host de quem fala com a propria maquina, com ou sem porta."""
    host = str(cabecalho or "").strip().lower()
    if host.startswith("["):                        # [::1]:6300
        host = host.split("]", 1)[0] + "]"
    else:
        host = host.rsplit(":", 1)[0] if ":" in host else host
    return host in HOSTS_PERMITIDOS


def exigencia(caminho: str) -> tuple:
    """O que a rota pede: ("aberta", ""), ("chave", <segredo no caminho>) ou
    ("sessao", <token no caminho>)."""
    partes = [p for p in caminho.split("/") if p]
    if caminho in ROTAS_ABERTAS or not partes or partes[0] != "api":
        return ("aberta", "")                       # inclui /socket.io (confere o connect)
    if len(partes) == 4 and partes[3] == "generate-token":
        return ("chave", partes[2])
    if len(partes) == 3 and partes[2] in ROTAS_DE_ADMINISTRACAO:
        return ("chave", partes[1])
    if len(partes) >= 3:
        return ("sessao", partes[1])
    return ("aberta", "")                           # /api/x: o roteador responde 404


def criar_middleware(segredo: str):
    @web.middleware
    async def conferir(request, handler):
        if not host_permitido(request.headers.get("Host")):
            return web.json_response(
                {"status": "error", "message": "forbidden host"}, status=403)
        tipo, valor = exigencia(request.path)
        if (tipo == "chave" and not segredo_valido(segredo, valor)) or (
                tipo == "sessao" and not credencial_valida(segredo, valor)):
            return web.json_response(
                {"status": "error", "message": "unauthorized"}, status=401)
        return await handler(request)
    return conferir
