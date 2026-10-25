"""O erro do pedido de codigo de pareamento, no formato que o cliente le.

O cliente (websocket_client.on_wpp_phone_code_error) mostra `name: message` e
decide se o WhatsApp recusou por quota com `rateLimited` (ou lendo
"rate-overlimit" no texto). A quota e por numero de telefone e sobrevive ao
processo: repetir um pedido recusado por ela e o que a mantem viva
(docs/traps/pairing-flow.md). Funcoes puras, testadas em test_pareamento.py.
"""
import re

_QUOTA = re.compile(r"rate-?overlimit|\b429\b", re.IGNORECASE)
_NUMEROS_LONGOS = re.compile(r"\d{7,}")
LIMITE_DA_MENSAGEM = 200


def erro_do_codigo(erro) -> dict:
    """O payload de `phoneCodeError` para a excecao `erro`. O texto nunca leva
    um numero de telefone: sequencias de 7 ou mais digitos viram `<n>`."""
    nome = type(erro).__name__
    texto = _NUMEROS_LONGOS.sub("<n>", str(erro))
    return {
        "name": "LinkCodeError",
        "message": f"{nome}: {texto}"[:LIMITE_DA_MENSAGEM] if texto else nome,
        "rateLimited": bool(_QUOTA.search(f"{nome} {texto}")),
    }
