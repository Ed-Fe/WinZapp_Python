"""Rotas de conversas e mensagens que faltavam depois de conversas.py: liga os
tres modulos delas (temporary-messages em mensagens_efemeras, all-new-messages
e send-status em conversas_novas, reply-button em conversas_interativas).
server.py chama so `registrar`.
"""
import asyncio
import time

from aiohttp import web

import conversas_interativas
import conversas_novas
import mensagens_efemeras


def registrar(r, *, sessao, ok, destino_de, contexto_citado, publicar_status,
              status_de_texto):
    """Liga as rotas restantes de conversas ao roteador `r` do aiohttp."""

    async def em_thread(funcao, *args):
        return await asyncio.get_running_loop().run_in_executor(None, funcao, *args)

    def erro(mensagem, status=500):
        return web.json_response({"status": "error", "message": str(mensagem)}, status=status)

    mensagens_efemeras.registrar(r, sessao=sessao, ok=ok, erro=erro, destino_de=destino_de,
                                 em_thread=em_thread, agora=time.time)
    conversas_novas.registrar(r, sessao=sessao, ok=ok, erro=erro,
                              publicar_status=publicar_status, status_de_texto=status_de_texto)
    conversas_interativas.registrar(r, sessao=sessao, ok=ok, destino_de=destino_de,
                                    contexto_citado=contexto_citado)
