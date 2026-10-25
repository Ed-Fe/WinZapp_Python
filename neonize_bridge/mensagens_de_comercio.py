"""Mensagens de comercio (pedido e produto) no formato do WhatsApp Web.

O cliente (`client/core/commerce.py`) le `type: "order"` com `itemCount`,
`status`, `totalAmount1000`, `totalCurrencyCode`, `message`; e `type: "product"`
com `title`, `description`, `salePriceAmount1000`/`priceAmount1000`,
`currencyCode`, `productId`. Sem isto uma conta de vendedor nem via o pedido
chegar (tudo caia em `unknown`). Funcoes puras; nada aqui foi testado contra
uma conta real.
"""

# nome do botao da mensagem interativa -> nativeFlowName que o cliente conhece
# (WAWebBizTemplateAndInteractiveMessagesUtils do WhatsApp Web)
FLUXOS = {
    "review_and_pay": "order_details",
    "review_order": "order_status",
    "order_details": "order_details",
    "order_status": "order_status",
    "payment_info": "payment_info",
    "payment_status": "payment_status",
    "payment_method": "payment_method",
}


def fluxo_nativo(nome_do_botao) -> str:
    """O `nativeFlowName` de um botao nativo, ou '' se nao e de pedido/pagamento."""
    return FLUXOS.get(nome_do_botao or "", "")


def campos_do_pedido(pedido) -> dict:
    """Campos de uma `OrderMessage`."""
    return {"type": "order",
            "itemCount": int(pedido.itemCount or 0),
            "status": int(pedido.status or 0),
            "totalAmount1000": int(pedido.totalAmount1000 or 0),
            "totalCurrencyCode": pedido.totalCurrencyCode,
            "message": pedido.message,
            "body": pedido.orderTitle or pedido.message}


def campos_do_produto(produto) -> dict:
    """Campos de uma `ProductMessage`. O `body` e o texto da mensagem, nunca a
    miniatura da imagem."""
    p = produto.product
    return {"type": "product",
            "productId": p.productID,
            "title": p.title,
            "description": p.description,
            "currencyCode": p.currencyCode,
            "priceAmount1000": int(p.priceAmount1000 or 0),
            "salePriceAmount1000": int(p.salePriceAmount1000 or 0),
            "body": produto.body or p.title}
