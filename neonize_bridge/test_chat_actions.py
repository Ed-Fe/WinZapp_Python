"""Action routes on chats: pure functions, the store (clear, delete,
receipts) and the routes that do not need neonize."""
import asyncio
from types import SimpleNamespace as N

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import chat_actions as ca
from store import Store


def test_keep_starred_by_default():
    assert ca.keep_starred({}) is True
    assert ca.keep_starred({"keepStarred": True}) is True
    assert ca.keep_starred({"keepStarred": False}) is False
    assert ca.keep_starred({"keepStarred": "false"}) is False


def test_clear_index():
    assert ca.clear_index(True) == ("0", "1")
    assert ca.clear_index(False) == ("1", "1")


def test_phones_accepts_list_or_text():
    assert ca.phones({"phone": ["1@c.us", "2@c.us"]}) == ["1@c.us", "2@c.us"]
    assert ca.phones({"phone": "1@c.us"}) == ["1@c.us"]
    for body in ({}, {"phone": []}, {"phone": [""]}):
        with pytest.raises(ValueError):
            ca.phones(body)


def test_typing_state():
    assert ca.typing_state(0) == ("CHAT_PRESENCE_COMPOSING", "CHAT_PRESENCE_MEDIA_TEXT")
    assert ca.typing_state("1") == ("CHAT_PRESENCE_COMPOSING", "CHAT_PRESENCE_MEDIA_AUDIO")
    assert ca.typing_state(2)[0] == "CHAT_PRESENCE_PAUSED"
    for bad in (3, None, "x"):
        with pytest.raises(ValueError):
            ca.typing_state(bad)


def test_receipt_participants_one_row_per_person():
    rows = [("a@c.us", "delivered", 10), ("a@c.us", "read", 20),
            ("b@c.us", "delivered", 12), ("c@c.us", "played", 30)]
    output = {p["id"]: p for p in ca.receipt_participants(rows)}
    assert output["a@c.us"] == {"id": "a@c.us", "deliveredAt": 10, "readAt": 20}
    assert output["b@c.us"] == {"id": "b@c.us", "deliveredAt": 12}
    # whoever listened also received: without a delivery receipt, delivery takes the same time
    assert output["c@c.us"] == {"id": "c@c.us", "playedAt": 30, "deliveredAt": 30}


def test_receipt_participants_ignores_an_unknown_stage():
    assert ca.receipt_participants([("a@c.us", "sent", 5)]) == []


def msg(mid, starred=False, t=1):
    return {"id": {"id": mid, "fromMe": False}, "fromMe": False, "t": t, "star": starred}


def test_clear_chat_keeps_the_starred_ones(tmp_path):
    db = Store(str(tmp_path / "x.db"))
    db.chat("a@s.whatsapp.net", unread=3)
    db.message("a@s.whatsapp.net", msg("1", starred=True))
    db.message("a@s.whatsapp.net", msg("2"))
    db.message("b@s.whatsapp.net", msg("3"))
    assert db.clear_chat("a@s.whatsapp.net", keep_starred=True) == 1
    assert [m["id"]["id"] for m in db.messages("a@s.whatsapp.net")] == ["1"]
    assert [m["id"]["id"] for m in db.messages("b@s.whatsapp.net")] == ["3"]
    assert db.unread("a@s.whatsapp.net") == 0


def test_clear_chat_without_keeping_deletes_everything_and_releases_the_pinned(tmp_path):
    db = Store(str(tmp_path / "x.db"))
    db.message("a@s.whatsapp.net", msg("1", starred=True))
    db.pin_message("1", "a@s.whatsapp.net", True, ts=1)
    assert db.clear_chat("a@s.whatsapp.net", keep_starred=False) == 1
    assert db.messages("a@s.whatsapp.net") == []
    assert db.pinned_of(["a@s.whatsapp.net"], 2) == []


def test_delete_chat_takes_it_off_the_list(tmp_path):
    db = Store(str(tmp_path / "x.db"))
    db.chat("a@s.whatsapp.net", name="Ana")
    db.message("a@s.whatsapp.net", msg("1", starred=True))
    db.delete_chat("a@s.whatsapp.net")
    assert not db.chat_exists("a@s.whatsapp.net")
    assert db.messages("a@s.whatsapp.net") == []


def test_receipt_keeps_the_first_time_of_each_stage(tmp_path):
    db = Store(str(tmp_path / "x.db"))
    db.receipt("m1", "a@c.us", "delivered", 10)
    db.receipt("m1", "a@c.us", "delivered", 99)
    db.receipt("m1", "a@c.us", "read", 20)
    assert db.receipts_of("m1") == [("a@c.us", "delivered", 10), ("a@c.us", "read", 20)]
    assert db.receipts_of("other") == []


def ask(tmp_path, path, body, prepare=lambda db: None, connected=True):
    db = Store(str(tmp_path / "x.db"))
    prepare(db)
    s = N(db=db, client=object() if connected else None,
          status="CONNECTED" if connected else "CLOSED",
          chat_keys=lambda jid: [], existing_chat=lambda jid: "",
          id_parts=staticmethod(lambda serial: (
              serial.startswith("true_"), "", serial.split("_")[2] if serial.count("_") >= 2 else serial, "")))

    async def in_thread(function, *args):
        return function(*args)

    async def run():
        app = web.Application()
        ca.register_routes(
            app.router, session=lambda req: s, ok=lambda **kw: web.json_response(kw),
            error=lambda m, status=500: web.json_response(
                {"status": "error", "message": str(m)}, status=status),
            in_thread=in_thread)
        async with TestClient(TestServer(app)) as client:
            response = await client.post(path, json=body)
            return response.status, await response.json()
    return asyncio.run(run())


def test_message_ack_returns_the_participants(tmp_path):
    def prepare(db):
        db.receipt("M1", "a@c.us", "delivered", 10)
        db.receipt("M1", "a@c.us", "read", 20)
    status, body = ask(tmp_path, "/api/t/message-ack",
                       {"messageId": "true_55@c.us_M1"}, prepare)
    assert status == 200
    assert body["response"] == {
        "participants": [{"id": "a@c.us", "deliveredAt": 10, "readAt": 20}],
        "deliveryRemaining": 0}


def test_message_ack_without_a_receipt_or_of_a_received_message_is_404(tmp_path):
    status, _ = ask(tmp_path, "/api/t/message-ack", {"messageId": "true_55@c.us_M1"})
    assert status == 404

    def prepare(db):
        db.receipt("M2", "a@c.us", "read", 20)
    status, _ = ask(tmp_path, "/api/t/message-ack", {"messageId": "false_55@c.us_M2"}, prepare)
    assert status == 404


def test_clear_chat_validates_the_body_and_the_connection(tmp_path):
    status, _ = ask(tmp_path, "/api/t/clear-chat", {"isGroup": False})
    assert status == 400
    status, body = ask(tmp_path, "/api/t/clear-chat", {"phone": ["55@c.us"]}, connected=False)
    assert status == 404 and body["status"] == "Disconnected"


def test_clear_chat_answers_in_the_format_the_client_checks(tmp_path, monkeypatch):
    monkeypatch.setattr(ca, "clear_on_phone", lambda s, jid, keep: True)
    status, body = ask(tmp_path, "/api/t/clear-chat",
                       {"phone": ["55@c.us"], "isGroup": False, "keepStarred": False})
    assert status == 200
    assert body["response"] == {"data": {"55@c.us": True}, "keepStarred": False}


def test_clear_chat_refused_by_whatsapp_is_500(tmp_path, monkeypatch):
    monkeypatch.setattr(ca, "clear_on_phone", lambda s, jid, keep: False)
    status, _ = ask(tmp_path, "/api/t/clear-chat", {"phone": ["55@c.us"]})
    assert status == 500


def test_delete_chat_and_the_bulk_routes(tmp_path, monkeypatch):
    deleted = []
    monkeypatch.setattr(ca, "delete_on_phone", lambda s, jid: deleted.append(jid) or True)
    status, body = ask(tmp_path, "/api/t/delete-chat", {"phone": ["55@c.us"]})
    assert status == 200 and body["response"]["data"] == {"55@c.us": True}

    def prepare(db):
        db.chat("a@s.whatsapp.net")
        db.chat("b@s.whatsapp.net")
        db.chat("status@broadcast")
    deleted.clear()
    status, body = ask(tmp_path, "/api/t/delete-all-chats", {}, prepare)
    assert body["response"] == {"total": 2, "done": 2}
    assert sorted(deleted) == ["a@s.whatsapp.net", "b@s.whatsapp.net"]


def test_chat_state_refuses_an_unknown_value(tmp_path):
    status, _ = ask(tmp_path, "/api/t/chat-state", {"phone": "55", "chatstate": 7})
    assert status == 400
