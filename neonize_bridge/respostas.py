"""Respostas de recusa no formato que o cliente do WinZapp reconhece.

O servidor real (WPPConnect) tem formatos fixos para as recusas que o cliente
trata de forma propria; um erro generico ("status":"error") nao e reconhecido:

* sem conexao: HTTP 404 `{"response": null, "status": "Disconnected", ...}`
  (middleware statusConnection.ts). E o que faz o cliente pausar a fila, nao
  tentar o endereco @c.us de um destino @lid e deixar a mensagem na fila em vez
  de dar o envio por perdido (main_window/sending.py `_check_wa_connection_closed`,
  core/message_edit.py `response_not_sent`).
* citacao inexistente: HTTP 500 com `code: quoted_message_not_found`
  (api_patches/src/util/quotedMessagePreflight.ts, core/reply_failure.py): so
  assim o cliente sabe que nada foi enviado e devolve o texto ao campo.
* edicao recusada antes de enviar: o corpo traz "Cannot edit this message"
  (main_window/message_actions.py).

Funcoes puras sobre aiohttp, testadas em test_respostas.py. Nenhuma toca o
neonize.
"""
from aiohttp import web

#: o texto que o servidor real manda junto do Disconnected
MENSAGEM_DESCONECTADA = "A sessão do WhatsApp não está ativa."

#: o cliente procura este trecho no corpo (main_window/message_actions.py)
TEXTO_EDICAO_RECUSADA = "Cannot edit this message"


def conectada(s):
    """A sessao tem cliente neonize e esta CONNECTED?"""
    return s.cliente is not None and s.status == "CONNECTED"


def desconectada():
    """404 Disconnected, como o middleware statusConnection do servidor real."""
    return web.json_response(
        {"response": None, "status": "Disconnected", "message": MENSAGEM_DESCONECTADA},
        status=404)


def sem_conexao(s):
    """A resposta Disconnected se a sessao nao esta conectada, senao None."""
    return None if conectada(s) else desconectada()


def citacao_ausente():
    """500 que diz ao cliente que a citacao nao existe e que NADA foi enviado
    (reply_failure.quote_missing_before_send exige cada um destes campos)."""
    return web.json_response(
        {"status": "error", "code": "quoted_message_not_found",
         "stage": "quote_resolution", "sendAttempted": False}, status=500)


def edicao_recusada(motivo):
    """Recusa de edicao que o cliente sabe que ocorreu antes de enviar algo;
    `motivo` (not_mine, not_stored, outside_window) so ajuda a ler o log."""
    return web.json_response(
        {"status": "error", "message": TEXTO_EDICAO_RECUSADA, "reason": motivo},
        status=400)
