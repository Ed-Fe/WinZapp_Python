"""Remaining routes: the platform of a message, the Meta AI terms and the 501s."""
import asyncio
from types import SimpleNamespace as N

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import remaining


@pytest.mark.parametrize("serial,expected", [
    ("false_5511999@c.us_3EB0ABCDEF0123456789", "web"),
    ("true_5511999@c.us_3A1B2C3D4E5F60718293", "iphone"),
    ("false_5511999@c.us_" + "A" * 22, "android"),
    ("false_5511999@c.us_ABCDEF0123456789ABCD", "unknown"),
    # in a group the id is the third piece; the participant comes after
    ("false_120363-1@g.us_3EB0ABCDEF_5511888@c.us", "web"),
    ("3A1B2C3D4E5F", "iphone"),
    ("", "unknown"),
    (None, "unknown"),
])
def test_message_platform_follows_the_wa_js_rules(serial, expected):
    assert remaining.message_platform(serial) == expected


def test_platform_at_the_21_character_limit():
    assert remaining.message_platform("x_y_" + "B" * 21) == "unknown"
    assert remaining.message_platform("x_y_" + "B" * 22) == "android"


def run(s, requests):
    """Brings up an aiohttp server with only these routes and makes the requests:
    (method, path) or (method, path, JSON body)."""
    async def main():
        app = web.Application()
        remaining.register_routes(app.router, session=lambda req: s,
                                  ok=lambda **kw: web.json_response(kw),
                                  jid_from_text=lambda text: text, jid_text=str)
        async with TestClient(TestServer(app)) as http:
            outputs = []
            for method, path, *body in requests:
                resp = await http.request(method, "/api/tok" + path,
                                          json=body[0] if body else None)
                outputs.append((resp.status, await resp.json()))
            return outputs
    return asyncio.run(main())


def session(go=None, status="CONNECTED"):
    client = N(uuid=b"u1", **{"_NewClient__client": go}) if go is not None \
        else N(uuid=b"u1")
    return N(client=client, status=status)


def test_impossible_routes_answer_501_with_code_and_reason():
    s = session()
    requests = [(m, "/" + r.replace("{communityId}", "1@g.us").replace("{subgroupId}", "2@g.us")
                 .replace("{id}", "123")) for m, r, _, _ in remaining.IMPOSSIBLE]
    outputs = run(s, requests)
    assert len(outputs) == len(remaining.IMPOSSIBLE)
    for (status, body), (_, _, code, _) in zip(outputs, remaining.IMPOSSIBLE):
        assert status == 501 and body["status"] == "error"
        assert body["code"] == code and body["message"]


# The routes that were impossible before WinZapp's neonize build, and the code
# each answered: an older build still answers them so.
FORMERLY_IMPOSSIBLE = [
    ("POST", "/community-subgroups/1@g.us/2@g.us/join", None, "unavailable"),
    ("POST", "/community-subgroups/1@g.us/2@g.us/cancel-request", None, "unavailable"),
    ("POST", "/deactivate-community", {"id": "1@g.us"}, "unavailable"),
    ("POST", "/privacy/status", {"setting": "contact", "list": []},
     "status_privacy_unsupported"),
    ("POST", "/remove-contact", {"phone": "5511999998888"}, "contact_operation_unsupported"),
    ("PUT", "/newsletter/123", {"name": "New name"}, "newsletter_unsupported"),
    ("DELETE", "/newsletter/123", None, "newsletter_unsupported"),
    ("GET", "/custom-lists", None, "lists_not_available"),
]


def test_an_older_neonize_build_still_answers_501_with_the_old_codes(monkeypatch):
    import app_state_mirror
    monkeypatch.setattr(app_state_mirror, "supported", lambda: False)
    outputs = run(session(), [(m, p, b) for m, p, b, _ in FORMERLY_IMPOSSIBLE])
    for (status, body), (_, path, _, code) in zip(outputs, FORMERLY_IMPOSSIBLE):
        assert (status, body["code"]) == (501, code), path


def test_no_impossible_route_is_the_last_seen_or_presence_one():
    routes = {r for _, r, _, _ in remaining.IMPOSSIBLE}
    assert not any("last-seen" in r or "chat-is-online" in r for r in routes)


def test_meta_ai_terms_says_unknown_without_needing_a_connection():
    (status, body), = run(session(status="CLOSED"), [("GET", "/meta-ai-terms")])
    assert status == 200
    assert body["response"] == {"noticeId": "20250502", "state": "UNKNOWN"}


def test_accepting_the_meta_ai_terms_answers_501_and_records_no_consent():
    (status, body), = run(session(), [("POST", "/meta-ai-terms/accept")])
    assert status == 501 and body["code"] == "meta_ai_terms_unsupported"
    assert "response" not in body


def test_remaining_does_not_call_native_code():
    """The consent is not recorded through ctypes: no AcceptTOSNotice here."""
    code = open(remaining.__file__, encoding="utf-8").read()
    assert "ctypes" not in code


def test_get_platform_from_message_answers_the_word_in_json():
    outputs = run(session(), [
        ("GET", "/get-platform-from-message/false_5511@c.us_3EB0AA"),
        ("GET", "/get-platform-from-message/true_5511@c.us_3AFF00")])
    assert outputs == [(200, "web"), (200, "iphone")]
