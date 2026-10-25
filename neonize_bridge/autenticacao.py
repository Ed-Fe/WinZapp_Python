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
import time

from aiohttp import web

ARQUIVO_DO_SEGREDO = "segredo"
# Teto de um corpo lido de uma vez (JSON: voz e status em base64, campos de
# texto). 100 MiB de midia em base64 ficam em ~134 MiB; sobra margem. O
# send-file nao conta: le o multipart em blocos direto para o disco, e e por
# la que passam os 2 GiB (documento) e 1 GiB (midia) que o cliente aceita.
TAMANHO_MAXIMO_DO_CORPO = 160 * 1024**2
HOSTS_PERMITIDOS = ("127.0.0.1", "localhost", "[::1]")
# Modelos de rota (o `canonical` do recurso que o roteador escolheu), nunca o
# caminho cru: o roteador casa o caminho ainda codificado e so depois decodifica
# os valores, entao `/api/%2F/x` seria lido aqui como `/api///x`.
ROTAS_ABERTAS = ("/healthz", "/health", "/winzapp/identity", "/api-docs", "/metrics",
                 "/socket.io/")                     # o /socket.io/ confere no `connect`
MODELO_DO_TOKEN = "/api/{session}/{key}/generate-token"
MODELOS_DE_ADMINISTRACAO = ("/api/{secretkey}/show-all-sessions",
                            "/api/{secretkey}/start-all")
PREFIXO_DE_SESSAO = "/api/{tok}/"


def _ler_segredo(caminho) -> str:
    try:
        with open(caminho, encoding="utf-8") as arquivo:
            return arquivo.read().strip()
    except OSError:
        return ""


def carregar_segredo(pasta) -> str:
    """O segredo da ponte: o do arquivo, ou um novo (so o dono pode le-lo).

    Duas pontes iniciadas juntas (duas contas abrindo ao mesmo tempo) nao podem
    gravar segredos diferentes: o arquivo e criado de forma exclusiva e quem
    perde a corrida le o que o vencedor gravou."""
    caminho = os.path.join(pasta, ARQUIVO_DO_SEGREDO)
    segredo = _ler_segredo(caminho)
    if segredo:
        return segredo
    os.makedirs(pasta, exist_ok=True)
    for _ in range(40):
        novo = secrets.token_hex(32)
        try:
            descritor = os.open(caminho, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:             # outro acabou de criar: espera ele gravar
            time.sleep(0.05)
            segredo = _ler_segredo(caminho)
            if segredo:
                return segredo
            continue
        with os.fdopen(descritor, "w", encoding="utf-8") as arquivo:
            arquivo.write(novo)
        return novo
    # um arquivo vazio que nunca se completou: refaz
    novo = secrets.token_hex(32)
    descritor = os.open(caminho, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descritor, "w", encoding="utf-8") as arquivo:
        arquivo.write(novo)
    return novo


def token_da_sessao(segredo: str, nome: str) -> str:
    return hmac.new(segredo.encode(), nome.encode(), hashlib.sha256).hexdigest()


def _igual(a, b) -> bool:
    """Igualdade em tempo constante; em bytes, para nao falhar com nao ASCII."""
    return hmac.compare_digest(str(a).encode("utf-8"), str(b).encode("utf-8"))


def segredo_valido(segredo: str, candidato) -> bool:
    return bool(candidato) and isinstance(candidato, str) and _igual(candidato, segredo)


def credencial_valida(segredo: str, token) -> bool:
    """`token` e `<sessao>:<hash>`; vale se o hash e o desta sessao."""
    if not isinstance(token, str):
        return False
    nome, separador, hash_recebido = token.partition(":")
    if not (nome and separador and hash_recebido):
        return False
    return _igual(hash_recebido, token_da_sessao(segredo, nome))


def nome_da_credencial(token) -> str:
    """O nome da sessao no `<sessao>:<hash>`; so tem sentido depois de
    `credencial_valida`."""
    return str(token or "").partition(":")[0]


def host_permitido(cabecalho) -> bool:
    """Host de quem fala com a propria maquina, com ou sem porta."""
    host = str(cabecalho or "").strip().lower()
    if host.startswith("["):                        # [::1]:6300
        host = host.split("]", 1)[0] + "]"
    else:
        host = host.rsplit(":", 1)[0] if ":" in host else host
    return host in HOSTS_PERMITIDOS


def exigencia(modelo: str, partes) -> tuple:
    """O que a rota pede, a partir do modelo da rota que o roteador escolheu e
    dos valores que ele ja decodificou (`match_info`): ("aberta", ""),
    ("chave", <segredo>), ("sessao", <token>) ou ("negada", "") para qualquer
    rota que nao seja conhecida (nega por padrao)."""
    if modelo in ROTAS_ABERTAS:
        return ("aberta", "")
    if modelo == MODELO_DO_TOKEN:
        return ("chave", partes.get("key", ""))
    if modelo in MODELOS_DE_ADMINISTRACAO:
        return ("chave", partes.get("secretkey", ""))
    if modelo.startswith(PREFIXO_DE_SESSAO):
        return ("sessao", partes.get("tok", ""))
    return ("negada", "")


def criar_middleware(segredo: str):
    @web.middleware
    async def conferir(request, handler):
        if not host_permitido(request.headers.get("Host")):
            return web.json_response(
                {"status": "error", "message": "forbidden host"}, status=403)
        recurso = request.match_info.route.resource
        if recurso is None:                 # sem rota: o roteador responde 404/405
            return await handler(request)
        tipo, valor = exigencia(recurso.canonical, request.match_info)
        if tipo == "negada" or (tipo == "chave" and not segredo_valido(segredo, valor)) or (
                tipo == "sessao" and not credencial_valida(segredo, valor)):
            return web.json_response(
                {"status": "error", "message": "unauthorized"}, status=401)
        return await handler(request)
    return conferir
