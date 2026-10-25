"""Conversas e mensagens da ponte: liga os tres modulos da area, um por tema
(leitura em conversas_listagem, envios em conversas_envios, acoes sobre
conversas em conversas_acoes). server.py chama so `registrar`.
"""
import asyncio
import time

from aiohttp import web

import conversas_acoes
import conversas_envios
import conversas_listagem


def registrar(r, *, sessao, ok, id_wpp, destino_de, guardar_enviada, enviar_midia,
              contexto_citado):
    """Liga as rotas de conversas e mensagens ao roteador `r` do aiohttp."""

    async def em_thread(funcao, *args):
        return await asyncio.get_running_loop().run_in_executor(None, funcao, *args)

    def erro(mensagem, status=500):
        return web.json_response({"status": "error", "message": str(mensagem)}, status=status)

    conversas_listagem.registrar(r, sessao=sessao, ok=ok, erro=erro, id_wpp=id_wpp,
                                 agora=time.time)
    conversas_envios.registrar(r, sessao=sessao, ok=ok, erro=erro, destino_de=destino_de,
                               guardar_enviada=guardar_enviada, enviar_midia=enviar_midia,
                               contexto_citado=contexto_citado, em_thread=em_thread)
    conversas_acoes.registrar(r, sessao=sessao, ok=ok, erro=erro, em_thread=em_thread)
