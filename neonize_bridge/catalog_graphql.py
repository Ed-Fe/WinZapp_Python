"""Orders through WhatsApp's catalog GraphQL endpoint, as WhatsApp Web places
and reads them (read over CDP on 2026-10-26: WAWebBizCreateOrderJob,
WAWebBizQueryOrderJob, WAWebRelayEnvironment, WAWebGraphQLConstants).

Its "whatsapp_catalog" environment is a plain HTTPS POST to
https://graph.whatsapp.com/graphql/catalog with a public application token
built into WhatsApp Web, the query's doc_id and its variables: no account
credential goes with it. What ties an order to its people is the order's own
id and token, which travel in the order message.

- place an order: doc 26486627094287046, {input: {order: {jid: <seller>,
  products: [{id, name, quantity, price (thousandths, a string), currency}]}}}
  -> xwa_checkout_place_order.order {order_id, token, price{subtotal_amount,
  total_amount, currency}};
- read an order: doc 26593811266898374, {request: {order: {jid: <seller>,
  token: {sensitive_string_value}, id, image_dimensions: {height, width}}}}
  -> xwa_checkout_get_order_info.order.

The pure functions are at the top (tested in test_catalog_graphql.py).
"""
import json

ENDPOINT = "https://graph.whatsapp.com/graphql/catalog"
ACCESS_TOKEN = "WA|787118555984857|7bb1544a3599aa180ac9a3f7688ba243"
PLACE_ORDER_DOC = "26486627094287046"
GET_ORDER_DOC = "26593811266898374"
TIMEOUT = 30


class GraphQLError(Exception):
    def __init__(self, message, code=0):
        super().__init__(message)
        self.code = int(code or 0)


def request_body(doc_id, variables, lang="en_US") -> dict:
    return {"access_token": ACCESS_TOKEN, "doc_id": doc_id, "variables": variables, "lang": lang}


def place_order_variables(seller_jid, items) -> dict:
    """`items`: [{id, name, quantity, price (thousandths) or None, currency}]."""
    products = []
    for item in items:
        product = {"id": str(item["id"]), "name": item.get("name") or "",
                   "quantity": int(item["quantity"])}
        if item.get("currency"):
            product["currency"] = item["currency"]
        if item.get("price") is not None:
            product["price"] = str(int(item["price"]))
        products.append(product)
    return {"input": {"order": {"jid": seller_jid, "products": products}}}


def get_order_variables(seller_jid, order_id, token, side=80) -> dict:
    return {"request": {"order": {"jid": seller_jid, "token": {"sensitive_string_value": token},
                                  "id": order_id,
                                  "image_dimensions": {"height": side, "width": side}}}}


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def placed_order(answer) -> dict:
    """{id, token, subtotal, total, currency} of a place order answer."""
    order = ((answer or {}).get("data") or {}).get("xwa_checkout_place_order") or {}
    order = order.get("order") or {}
    if not order.get("order_id"):
        raise GraphQLError("the order was not placed", 500)
    price = order.get("price") or {}
    return {"id": str(order["order_id"]), "token": order.get("token") or "",
            "subtotal": _int(price.get("subtotal_amount")),
            "total": _int(price.get("total_amount")), "currency": price.get("currency") or ""}


def order_info(answer) -> dict:
    """The order of a get order answer, as WhatsApp Web reads it."""
    order = ((answer or {}).get("data") or {}).get("xwa_checkout_get_order_info") or {}
    order = order.get("order")
    if not isinstance(order, dict):
        raise GraphQLError("no such order", 404)
    details = order.get("price_details") or {}
    products = []
    for p in order.get("products") or ():
        images = ((p.get("media") or {}).get("images") or [{}])
        products.append({"id": str(p.get("id") or ""), "name": p.get("name") or "",
                         "price": _int(p.get("price")), "currency": p.get("currency") or "",
                         "quantity": _int(p.get("quantity")),
                         "thumbnailUrl": (images[0] or {}).get("request_image_url") or ""})
    return {"createdAt": _int(order.get("creation_time_stamp")),
            "currency": details.get("currency") or "",
            "subtotal": _int(details.get("subtotal_amount")),
            "total": _int(details.get("total_amount")), "products": products}


def answer_errors(answer):
    """GraphQLError for an answer that carries errors, else None."""
    errors = (answer or {}).get("errors") if isinstance(answer, dict) else None
    if errors:
        first = errors[0] if isinstance(errors[0], dict) else {}
        return GraphQLError(first.get("message") or "graphql error", first.get("code") or 0)
    return None


def post(doc_id, variables, http=None) -> dict:
    """One query; the decoded answer, or GraphQLError. Runs on a thread."""
    import requests
    http = http or requests
    try:
        response = http.post(ENDPOINT, data=json.dumps(request_body(doc_id, variables)),
                             headers={"Accept": "application/json",
                                      "Content-Type": "application/json"},
                             timeout=TIMEOUT)
        answer = response.json()
    except Exception as error:
        raise GraphQLError(f"catalog endpoint unreachable ({type(error).__name__})") from None
    error = answer_errors(answer)
    if error is not None:
        raise error
    if getattr(response, "status_code", 200) >= 400:
        raise GraphQLError("catalog endpoint refused", response.status_code)
    return answer


def place_order(seller_jid, items, http=None) -> dict:
    return placed_order(post(PLACE_ORDER_DOC, place_order_variables(seller_jid, items), http))


def get_order(seller_jid, order_id, token, http=None) -> dict:
    return order_info(post(GET_ORDER_DOC, get_order_variables(seller_jid, order_id, token), http))
