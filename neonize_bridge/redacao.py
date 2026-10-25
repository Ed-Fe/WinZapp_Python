"""Nada de JID, telefone ou nome de sessao inteiro no log da ponte.

O log.log do usuario circula (relatos de erro, issues). `redigir` deixa so o
bastante para distinguir uma conversa de outra no mesmo log: o fim da parte do
usuario e o servidor (`g.us`, `lid`, `s.whatsapp.net`), que nao identifica
ninguem. Funcao pura, testada em test_redacao.py.
"""
import logging
import re


def redigir(valor) -> str:
    """`5511999998888@s.whatsapp.net` -> `...88@s.whatsapp.net`; `conta1` -> `...a1`."""
    texto = str(valor or "").strip()
    if not texto:
        return "?"
    usuario, arroba, servidor = texto.partition("@")
    return f"...{usuario[-2:]}{arroba}{servidor}"


_JID = re.compile(r"(\d{4,})(:\d+)?(@(?:s\.whatsapp\.net|c\.us|lid|g\.us|newsletter|broadcast))")
_NUMERO = re.compile(r"(?<![\w.])\+?\d{9,}(?![\w])")


def mascarar_texto(texto) -> str:
    """Esconde JIDs e numeros longos de um texto de log (o de uma excecao do
    whatsmeow pode trazer o telefone). Mesmo corte do `redigir`."""
    texto = _JID.sub(lambda m: f"...{m.group(1)[-2:]}{m.group(3)}", str(texto))
    return _NUMERO.sub(lambda m: "..." + m.group(0)[-2:], texto)


class FiltroDeRedacao(logging.Filter):
    """Passa a mensagem e o traceback de cada registro por `mascarar_texto`."""

    def filter(self, registro):
        registro.msg = mascarar_texto(registro.getMessage())
        registro.args = ()
        if registro.exc_info and not registro.exc_text:
            registro.exc_text = logging.Formatter().formatException(registro.exc_info)
        if registro.exc_text:
            registro.exc_text = mascarar_texto(registro.exc_text)
        return True
