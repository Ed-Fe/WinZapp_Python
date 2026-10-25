"""Nada de JID, telefone ou nome de sessao inteiro no log da ponte.

O log.log do usuario circula (relatos de erro, issues). `redigir` deixa so o
bastante para distinguir uma conversa de outra no mesmo log: o fim da parte do
usuario e o servidor (`g.us`, `lid`, `s.whatsapp.net`), que nao identifica
ninguem. Funcao pura, testada em test_redacao.py.
"""


def redigir(valor) -> str:
    """`5511999998888@s.whatsapp.net` -> `...88@s.whatsapp.net`; `conta1` -> `...a1`."""
    texto = str(valor or "").strip()
    if not texto:
        return "?"
    usuario, arroba, servidor = texto.partition("@")
    return f"...{usuario[-2:]}{arroba}{servidor}"
