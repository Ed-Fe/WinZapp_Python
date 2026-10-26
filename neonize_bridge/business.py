"""Business and catalog routes. neonize (whatsmeow) has almost no support for
this: there is no business profile, catalog, products, collections, cart or
orders (the only commerce item it exposes is `resolve_business_message_link`,
which serves no client route). WPPConnect does all of it inside the WhatsApp
Web page, which this bridge does not have.

There is only one thing it can answer truthfully: `commerce/account`, which the
client polls from time to time (client/main_window/commerce.py). Without a
route every poll fails and the client retries right away; with a definitive
answer it stops. The answer is "this account offers no commerce features
through this engine": even on a Business account the actions the client would
enable (create order, turn on the cart) would fail, so we do not offer them.

The pure functions are at the top (tested in test_business.py);
`register_routes` wires the route to the server.
"""


def account_without_commerce() -> dict:
    """The body of `commerce/account` in the format the client reads
    (core/commerce.py parse_account): no Business account, no cart option
    (None = does not apply) and no known seller chats."""
    return {"business": False, "cartEnabled": None, "sellers": []}


def register_routes(r, *, session, ok):
    """Wire the business routes to the aiohttp router `r`."""

    async def commerce_account(req):
        session(req)   # validates the token like the other routes
        return ok(status="success", response=account_without_commerce())

    r.add_get("/api/{tok}/commerce/account", commerce_account)
