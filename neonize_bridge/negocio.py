"""Rotas de negocio e catalogo. O neonize (whatsmeow) quase nao tem suporte a
isso: nao ha perfil comercial, catalogo, produtos, colecoes, carrinho nem
pedidos (o unico item comercial exposto e `resolve_business_message_link`,
que nao serve a nenhuma rota do cliente). O WPPConnect faz tudo isso por dentro
da pagina do WhatsApp Web, que esta ponte nao tem.

O que da para responder com verdade e uma coisa so: `commerce/account`, que o
cliente consulta de tempos em tempos (client/main_window/commerce.py). Sem
rota, cada consulta falha e o cliente tenta de novo logo em seguida; com uma
resposta definitiva ele para. A resposta e "esta conta nao oferece recursos
comerciais por este motor": mesmo numa conta Business as acoes que o cliente
liberaria (criar pedido, ligar o carrinho) falhariam, entao nao as oferecemos.

As funcoes puras ficam no topo (testadas em test_negocio.py); `registrar` liga a
rota ao servidor.
"""


def conta_sem_comercio() -> dict:
    """O corpo de `commerce/account` no formato que o cliente le
    (core/commerce.py parse_account): sem conta Business, sem a opcao de
    carrinho (None = nao se aplica) e sem chats de vendedores conhecidos."""
    return {"business": False, "cartEnabled": None, "sellers": []}


def registrar(r, *, sessao, ok):
    """Liga as rotas de negocio ao roteador `r` do aiohttp."""

    async def commerce_account(req):
        sessao(req)   # valida o token como as demais rotas
        return ok(status="success", response=conta_sem_comercio())

    r.add_get("/api/{tok}/commerce/account", commerce_account)
