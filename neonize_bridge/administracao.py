"""Rotas de servidor e sessao da ponte: identidade do servidor, documentacao,
metricas, listar e iniciar sessoes, QR e o aviso de "reconectar o fluxo".

Os formatos de resposta sao os que o cliente do WinZapp le (identidade em
core/wpp_update_health.py e core/local_api_port.py; show-all-sessions em
main_window/wpp_server.py; reconnect-socket-stream em main_window/connection.py)
ou, onde o cliente nao usa a rota, os do WPPConnect Server
(api_patches/src/controller/sessionController.ts).

As funcoes puras ficam no topo (testadas em test_administracao.py); `registrar`
liga as rotas ao servidor. Nada aqui importa o neonize.

Ficam de fora, de proposito (a COBERTURA.md explica): `collect-page-garbage`
(o cliente trata 404 como "este servidor nao tem" e para de pedir; nao ha pagina
de navegador para coletar), `unhealthy`, `backup-sessions`, `restore-sessions`,
`chatwoot` e `clear-session-data`.
"""
import asyncio
import base64
import logging
import os
import re
import time
from types import SimpleNamespace

from aiohttp import web

log = logging.getLogger("ponte")

PROTOCOLO = 1
STATUS_CONHECIDOS = ("CLOSED", "INITIALIZING", "QRCODE", "CONNECTED")
ARQUIVO_DA_SESSAO = "sessao.db"


def identidade(ambiente, pid) -> dict:
    """Corpo de GET /winzapp/identity. O cliente compara `installation_id` com o
    da instalacao dele; a ponte e um processo compartilhado que o cliente nao
    sobe com essas variaveis, entao vazias = "nao e o Node deste cliente"."""
    return {"installation_id": ambiente.get("WINZAPP_INSTALLATION_ID", ""),
            "instance_id": ambiente.get("WINZAPP_INSTANCE_ID", ""),
            "protocol_version": PROTOCOLO, "pid": pid}


def documento_openapi(rotas, versao="1") -> dict:
    """Um OpenAPI 3 minimo (so caminhos e metodos) das rotas registradas.
    `rotas` e uma lista de (metodo, caminho); HEAD, '*' e o socket.io ficam de fora."""
    caminhos = {}
    for metodo, caminho in rotas:
        metodo = metodo.upper()
        if metodo in ("HEAD", "*", "OPTIONS") or caminho.startswith("/socket.io"):
            continue
        operacao = {"responses": {"200": {"description": "OK"}}}
        nomes = re.findall(r"\{(\w+)\}", caminho)
        if nomes:
            operacao["parameters"] = [
                {"name": n, "in": "path", "required": True, "schema": {"type": "string"}}
                for n in nomes]
        caminhos.setdefault(caminho, {})[metodo.lower()] = operacao
    return {"openapi": "3.0.0",
            "info": {"title": "WinZapp Neonize bridge", "version": versao},
            "paths": dict(sorted(caminhos.items()))}


def texto_metricas(status_das_sessoes, segundos_ligada) -> str:
    """Metricas no formato de texto do Prometheus: sessoes por estado e o tempo
    que a ponte esta de pe."""
    contagem = {s: 0 for s in STATUS_CONHECIDOS}
    for status in status_das_sessoes:
        contagem[status] = contagem.get(status, 0) + 1
    linhas = ["# HELP winzapp_bridge_uptime_seconds Seconds since the bridge started.",
              "# TYPE winzapp_bridge_uptime_seconds gauge",
              "winzapp_bridge_uptime_seconds %d" % max(0, int(segundos_ligada)),
              "# HELP winzapp_bridge_sessions Sessions held by the bridge, by state.",
              "# TYPE winzapp_bridge_sessions gauge"]
    for status, quantas in contagem.items():
        linhas.append('winzapp_bridge_sessions{status="%s"} %d' % (status, quantas))
    return "\n".join(linhas) + "\n"


def sessoes_em_disco(pasta) -> list:
    """Nomes das contas que ja foram pareadas (pasta com sessao.db), em ordem."""
    try:
        nomes = os.listdir(pasta)
    except OSError:
        return []
    return sorted(n for n in nomes
                  if os.path.isfile(os.path.join(pasta, n, ARQUIVO_DA_SESSAO)))


def png_do_data_url(data_url) -> bytes:
    """'data:image/png;base64,XXXX' -> os bytes do PNG."""
    return base64.b64decode(data_url.split(",", 1)[1])


def resposta_qrcode(iniciada, status, urlcode, data_url_do_qr):
    """GET qrcode-session como o WPPConnect responde: o PNG quando ha um QR;
    senao um JSON 200 que diz por que nao ha. ("png", bytes) ou ("json", dict)."""
    if urlcode:
        return "png", png_do_data_url(data_url_do_qr(urlcode))
    if not iniciada:
        return "json", {"status": None, "message": "Session not started. Please, use "
                        "the /start-session route, for initialization your session"}
    return "json", {"status": status, "message": "QRCode is not available..."}


def resposta_reconexao(status, conectado):
    """POST reconnect-socket-stream: (http, corpo). O cliente usa isto como um
    cutucao: 200 diz que a conexao com o WhatsApp esta de pe; um erro, tres vezes
    seguidas, o leva a reiniciar a sessao. O whatsmeow reconecta sozinho, entao
    aqui nao ha o que forcar: so se informa o estado real."""
    if status == "CONNECTED" and conectado:
        return 200, {"status": "success", "response": {"ok": True}}
    return 400, {"status": "error", "message": "The WhatsApp session is not active."}


def registrar(r, *, sessao, ok, estado, pasta, marcar_ativa, qr_data_url):
    """Liga as rotas de servidor e sessao ao roteador `r` do aiohttp."""
    ligada_em = time.monotonic()

    def rotas_registradas():
        saida = []
        for recurso in r.resources():
            for rota in recurso:
                saida.append((rota.method, recurso.canonical))
        return saida

    async def winzapp_identity(req):
        return web.json_response(identidade(os.environ, os.getpid()))

    async def api_docs(req):
        return web.json_response(documento_openapi(rotas_registradas()))

    async def metrics(req):
        texto = texto_metricas([s.status for s in estado["sessoes"].values()],
                               time.monotonic() - ligada_em)
        return web.Response(text=texto, content_type="text/plain", charset="utf-8",
                            headers={"X-Content-Type-Options": "nosniff"})

    async def show_all_sessions(req):
        # O segredo da ponte ja foi conferido pelo middleware (autenticacao.py).
        return ok(response=sorted(estado["sessoes"]))

    async def start_all(req):
        nomes = [estado["fixa"]] if estado["fixa"] else sessoes_em_disco(pasta)
        for nome in nomes:
            # sessao() so le match_info["tok"]: cria (ou acha) a conta pelo nome.
            s = sessao(SimpleNamespace(match_info={"tok": nome}))
            s.iniciar()
            marcar_ativa(nome, True)
        return web.json_response(
            {"status": "success", "message": "Starting all sessions"}, status=201)

    async def qrcode_session(req):
        s = sessao(req)
        tipo, dado = resposta_qrcode(s.thread is not None, s.status, s.urlcode, qr_data_url)
        if tipo == "png":
            return web.Response(body=dado, content_type="image/png")
        return web.json_response(dado)

    async def reconnect_socket_stream(req):
        s = sessao(req)
        conectado = False
        if s.status == "CONNECTED" and s.cliente is not None:
            try:
                conectado = bool(await asyncio.get_running_loop().run_in_executor(
                    None, lambda: s.cliente.is_connected))
            except Exception:
                log.exception("reconnect-socket-stream")
        http, corpo = resposta_reconexao(s.status, conectado)
        return web.json_response(corpo, status=http)

    r.add_get("/winzapp/identity", winzapp_identity)
    r.add_get("/api-docs", api_docs)
    r.add_get("/metrics", metrics)
    r.add_get("/api/{secretkey}/show-all-sessions", show_all_sessions)
    r.add_post("/api/{secretkey}/start-all", start_all)
    r.add_get("/api/{tok}/qrcode-session", qrcode_session)
    r.add_post("/api/{tok}/reconnect-socket-stream", reconnect_socket_stream)
