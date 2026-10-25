"""Validacao do que chega pela porta local: nome de sessao e arquivo do corpo.

Funcoes puras, testadas em test_entradas.py.
"""
import re

from conversas_envios import dados_de_base64

_NOME_DE_SESSAO = re.compile(r"[A-Za-z0-9_-]{1,128}")


def nome_de_sessao_valido(nome) -> bool:
    """O nome vira uma pasta em dados-real: sem separadores, sem '..'."""
    return bool(_NOME_DE_SESSAO.fullmatch(str(nome or "")))


def bytes_do_corpo(corpo) -> bytes:
    """Bytes do campo `path` (data URI base64 ou so o base64). Nunca abre um
    arquivo do disco: o caminho vem de quem fala com a porta local."""
    return dados_de_base64((corpo or {}).get("path"))[0]
