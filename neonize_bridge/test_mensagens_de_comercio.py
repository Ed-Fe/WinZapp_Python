import json

from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import Message

import mensagens_de_comercio as mc
import msgmap

CHAT = "5511999990000@s.whatsapp.net"


def _montar(mensagem):
    return msgmap.montar(conversa=CHAT, de_mim=False, id="ID1", remetente=CHAT,
                         ts=1700000000, mensagem=mensagem)


def test_pedido_chega_como_order_com_os_campos_que_o_cliente_le():
    msg = Message()
    msg.orderMessage.orderID = "123"
    msg.orderMessage.itemCount = 3
    msg.orderMessage.status = 1
    msg.orderMessage.totalAmount1000 = 45500
    msg.orderMessage.totalCurrencyCode = "BRL"
    msg.orderMessage.message = "sem cebola"
    m = _montar(msg)
    assert m["type"] == "order"
    assert (m["itemCount"], m["status"], m["totalAmount1000"]) == (3, 1, 45500)
    assert m["totalCurrencyCode"] == "BRL" and m["message"] == "sem cebola"


def test_produto_chega_como_product_com_preco_e_id():
    msg = Message()
    msg.productMessage.product.productID = "98765"
    msg.productMessage.product.title = "Pao"
    msg.productMessage.product.description = "Fresco"
    msg.productMessage.product.currencyCode = "BRL"
    msg.productMessage.product.priceAmount1000 = 7500
    m = _montar(msg)
    assert m["type"] == "product" and m["productId"] == "98765"
    assert m["title"] == "Pao" and m["description"] == "Fresco"
    assert m["priceAmount1000"] == 7500 and m["currencyCode"] == "BRL"
    assert m["body"] == "Pao"           # nunca a miniatura da imagem


def test_aviso_de_pedido_ganha_o_nome_do_fluxo():
    msg = Message()
    botao = msg.interactiveMessage.nativeFlowMessage.buttons.add()
    botao.name = "review_order"
    botao.buttonParamsJSON = json.dumps({"reference_id": "R1"})
    m = _montar(msg)
    assert m["nativeFlowName"] == "order_status"
    assert m["interactivePayload"]["buttons"][0]["name"] == "review_order"


def test_botao_nativo_comum_nao_ganha_nome_de_fluxo():
    msg = Message()
    botao = msg.interactiveMessage.nativeFlowMessage.buttons.add()
    botao.name = "quick_reply"
    assert "nativeFlowName" not in _montar(msg)


def test_fluxos_conhecidos():
    assert mc.fluxo_nativo("review_and_pay") == "order_details"
    assert mc.fluxo_nativo("payment_info") == "payment_info"
    assert mc.fluxo_nativo("outro") == "" and mc.fluxo_nativo(None) == ""
