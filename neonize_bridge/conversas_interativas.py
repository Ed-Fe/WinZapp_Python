"""Rota `reply-button/:messageId`: responder a um botao de uma mensagem de
empresa (o WinZapp chama; ver client/core/business_buttons.py). Os botoes e o
formato de cada resposta estao em botoes.py.

O cliente le o codigo de erro (`code`) do corpo, nao a mensagem: 400
invalid_request, 404 message_not_found / button_not_found, 501
reply_button_unavailable (o que a ponte nao sabe enviar).

Sem efeito enquanto msgmap.montar nao copiar `botoes.campos_de_botoes` para as
mensagens recebidas: sem esses campos a mensagem guardada nao tem botao.
A resposta enviada nao e guardada no armazem (o cliente so quer saber se saiu).
Enviada uma vez, nunca repetida: um erro pode ter chegado a empresa.

`send-buttons` e `send-list-message` nao existem aqui de proposito: o WhatsApp
nao mostra botoes nem lista de contas pessoais (so de empresa, com um no
`biz` que o whatsmeow nao monta). Nada aqui foi testado contra uma conta real.

As funcoes puras ficam no topo (testadas em test_conversas_interativas.py);
`registrar` liga a rota ao servidor.
"""
import asyncio
import logging
import re

from aiohttp import web

import botoes

log = logging.getLogger("ponte")

#: "true_<conversa>_<ID>" de uma conversa individual (a mesma regra do cliente)
_MENSAGEM = re.compile(r"(?:true|false)_\d+@(?:c\.us|lid)_[A-Za-z0-9]{8,64}")
MAXIMO_DE_BOTOES = 12


def pedido_valido(serial, corpo):
    """(source, index) do pedido, ou BotaoRecusado(invalid_request)."""
    source = corpo.get("source") if isinstance(corpo, dict) else None
    indice = corpo.get("index") if isinstance(corpo, dict) else None
    if (not isinstance(serial, str) or not _MENSAGEM.fullmatch(serial)
            or source not in botoes.SOURCES or isinstance(indice, bool)
            or not isinstance(indice, int) or not 0 <= indice < MAXIMO_DE_BOTOES):
        raise botoes.BotaoRecusado("invalid_request", 400)
    return source, indice


def registrar(r, *, sessao, ok, destino_de, contexto_citado):
    """Liga `reply-button` ao roteador `r` do aiohttp."""

    def recusa(codigo, status):
        return web.json_response({"status": "error", "code": codigo}, status=status)

    async def reply_button(req):
        s = sessao(req)
        serial = req.match_info["messageId"]
        try:
            corpo = await req.json()
        except ValueError:
            corpo = None
        try:
            source, indice = pedido_valido(serial, corpo)
            _, conversa, mid, _ = s.partes_do_id(serial)
            original = s.bd.mensagem_wpp(mid)
            if original is None:
                raise botoes.BotaoRecusado("message_not_found", 404)
            plano = botoes.plano_da_resposta(original, source, indice)
        except botoes.BotaoRecusado as e:
            return recusa(e.codigo, e.status)
        if s.cliente is None or s.status != "CONNECTED":
            return recusa("reply_button_unavailable", 503)
        ctx = contexto_citado(s, serial)
        if ctx is None:
            return recusa("message_not_found", 404)
        try:
            await asyncio.get_running_loop().run_in_executor(
                None, s.cliente.send_message, destino_de(conversa),
                botoes.montar_resposta(plano, ctx))
        except Exception:
            log.exception("reply-button")
            return recusa("reply_button_unavailable", 503)
        return ok(status="success", response={"sent": True})

    r.add_post("/api/{tok}/reply-button/{messageId}", reply_button)
