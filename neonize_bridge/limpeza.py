"""Apagar uma conta da ponte: `POST /api/<token>/delete-session`.

Quem apaga uma conta no WinZapp (ou desfaz um pareamento) tem de levar junto o
que a ponte guardou dela: as chaves do WhatsApp (`sessao.db`) e todas as
mensagens em texto puro (`ponte.db`), em `<pasta>/<nome>`. So a ponte pode
fazer isso: ela mantem os dois SQLite abertos, e no Windows um arquivo aberto
nao se apaga. A rota: sai da conta (o aparelho deixa de ficar vinculado no
celular), para a sessao, solta os arquivos, apaga a pasta e tira o nome de
`ativas.json`.

Exige a credencial normal da sessao (o middleware de autenticacao.py confere o
`<nome>:<hash>` de `/api/{tok}/...`), entao uma conta so apaga a si mesma.

As funcoes puras ficam no topo (testadas em test_limpeza.py). Nada aqui importa
o neonize.
"""
import asyncio
import logging
import os
import shutil

from aiohttp import web

import entradas
from redacao import redigir

log = logging.getLogger("ponte")


def pasta_da_sessao(pasta, nome):
    """A pasta `<pasta>/<nome>` a apagar, ou None quando nao e seguro apagar.

    So um filho direto de `pasta`: o nome tem de ser um nome de sessao comum
    (sem separador, sem '..') e o caminho resolvido tem de continuar sendo esse
    filho, nunca um atalho (link simbolico ou juncao) que leve para fora dos
    dados da ponte.
    """
    if not entradas.nome_de_sessao_valido(nome):
        return None
    base = os.path.realpath(pasta)
    destino = os.path.join(base, nome)
    if os.path.islink(destino):
        return None
    real = os.path.realpath(destino)
    if os.path.normcase(os.path.dirname(real)) != os.path.normcase(base):
        return None
    if os.path.normcase(os.path.basename(real)) != os.path.normcase(nome):
        return None
    return destino


def apagar_pasta(pasta, nome, remover=shutil.rmtree) -> bool:
    """Apaga a pasta da sessao. True tambem quando ela ja nao existe (pedir de
    novo depois de uma falha tem de poder terminar o servico). False se a pasta
    ficou (arquivo ainda aberto, sem permissao) ou se o nome nao e seguro."""
    destino = pasta_da_sessao(pasta, nome)
    if destino is None:
        return False
    if not os.path.lexists(destino):
        return True
    try:
        remover(destino)
    except OSError as erro:
        log.error("nao consegui apagar a pasta da sessao %s: %s", redigir(nome),
                  type(erro).__name__)
        return False
    return not os.path.lexists(destino)


def encerrar(sessao) -> bool:
    """Sai da conta, para a sessao e solta os arquivos dela. Devolve se a conta
    estava conectada (so entao o aparelho e desvinculado de verdade)."""
    estava_conectada = sessao.cliente is not None and sessao.status == "CONNECTED"
    sessao.sair()
    if sessao.thread is not None:
        sessao.thread.join(10)
    try:
        sessao.bd.fechar()
    except Exception:
        log.exception("fechar o armazenamento")
    return estava_conectada


def registrar(r, *, ok, estado, pasta, marcar_ativa):
    """Liga a rota ao roteador `r` do aiohttp."""

    async def delete_session(req):
        nome = req.match_info["tok"].split(":", 1)[0]
        if estado["fixa"] or pasta_da_sessao(pasta, nome) is None:
            return web.json_response(
                {"status": "error", "message": "invalid session name"}, status=400)
        laco = asyncio.get_running_loop()
        conta = estado["sessoes"].pop(nome, None)
        saiu = False
        if conta is not None:
            saiu = await laco.run_in_executor(None, encerrar, conta)
        marcar_ativa(nome, False)
        if not await laco.run_in_executor(None, apagar_pasta, pasta, nome):
            return web.json_response(
                {"status": "error", "message": "could not remove the session data"},
                status=500)
        log.info("sessao apagada: %s", redigir(nome))
        return ok(status="success", response={"removed": True, "loggedOut": saiu})

    r.add_post("/api/{tok}/delete-session", delete_session)
