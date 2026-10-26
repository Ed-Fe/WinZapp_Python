"""Orders through the catalog GraphQL endpoint WhatsApp Web uses."""
import json
from types import SimpleNamespace as N

import pytest

import catalog_graphql as cg


def test_the_request_is_whatsapp_webs():
    body = cg.request_body("123", {"a": 1})
    assert body == {"access_token": cg.ACCESS_TOKEN, "doc_id": "123", "variables": {"a": 1},
                    "lang": "en_US"}
    assert cg.ENDPOINT == "https://graph.whatsapp.com/graphql/catalog"


def test_place_order_variables():
    variables = cg.place_order_variables("5511@s.whatsapp.net", [
        {"id": "1", "name": "Pão", "quantity": 2, "price": 7500, "currency": "BRL"},
        {"id": "2", "name": "Sem preço", "quantity": 1, "price": None, "currency": ""}])
    assert variables == {"input": {"order": {"jid": "5511@s.whatsapp.net", "products": [
        {"id": "1", "name": "Pão", "quantity": 2, "currency": "BRL", "price": "7500"},
        {"id": "2", "name": "Sem preço", "quantity": 1}]}}}


def test_get_order_variables():
    assert cg.get_order_variables("5511@s.whatsapp.net", "O1", "TK") == {"request": {"order": {
        "jid": "5511@s.whatsapp.net", "token": {"sensitive_string_value": "TK"}, "id": "O1",
        "image_dimensions": {"height": 80, "width": 80}}}}


def test_a_placed_order():
    answer = {"data": {"xwa_checkout_place_order": {"order": {
        "order_id": "O1", "token": "TK",
        "price": {"subtotal_amount": "15000", "total_amount": "15000", "currency": "BRL"}}}}}
    assert cg.placed_order(answer) == {"id": "O1", "token": "TK", "subtotal": 15000,
                                       "total": 15000, "currency": "BRL"}
    with pytest.raises(cg.GraphQLError):
        cg.placed_order({"data": {"xwa_checkout_place_order": {"order": {}}}})


def test_an_order_read_back():
    answer = {"data": {"xwa_checkout_get_order_info": {"order": {
        "creation_time_stamp": "1700000000",
        "products": [{"id": "1", "name": "Pão", "price": "7500", "currency": "BRL",
                      "quantity": "2", "media": {"images": [{"request_image_url": "u"}]}}],
        "price_details": {"subtotal_amount": "15000", "total_amount": "15000",
                          "currency": "BRL"}}}}}
    info = cg.order_info(answer)
    assert info["createdAt"] == 1700000000 and info["total"] == 15000
    assert info["products"] == [{"id": "1", "name": "Pão", "price": 7500, "currency": "BRL",
                                 "quantity": 2, "thumbnailUrl": "u"}]
    with pytest.raises(cg.GraphQLError):
        cg.order_info({"data": {}})


class Http:
    def __init__(self, answer, status=200):
        self.answer, self.status, self.sent = answer, status, []

    def post(self, url, data, headers, timeout):
        self.sent.append((url, json.loads(data), headers))
        return N(status_code=self.status, json=lambda: self.answer)


def test_post_sends_json_and_raises_graphql_errors():
    http = Http({"data": {"x": 1}})
    assert cg.post("9", {"v": 1}, http) == {"data": {"x": 1}}
    url, body, headers = http.sent[0]
    assert url == cg.ENDPOINT and body["doc_id"] == "9" and body["variables"] == {"v": 1}
    assert headers["Content-Type"] == "application/json"
    with pytest.raises(cg.GraphQLError) as caught:
        cg.post("9", {}, Http({"errors": [{"message": "nope", "code": 451}]}))
    assert caught.value.code == 451


def test_place_order_end_to_end():
    http = Http({"data": {"xwa_checkout_place_order": {"order": {"order_id": "O1", "token": "T",
                                                                  "price": {}}}}})
    order = cg.place_order("5511@s.whatsapp.net", [{"id": "1", "name": "P", "quantity": 1}], http)
    assert order["id"] == "O1" and http.sent[0][1]["doc_id"] == cg.PLACE_ORDER_DOC
