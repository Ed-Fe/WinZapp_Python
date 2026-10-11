"""Polls: a new poll's validation, how a received one is read and shown, and
how one is sent.

WhatsApp Web's type for a poll is ``poll_creation`` (wa-js's
``sendCreatePollMessage`` builds exactly that). The normalizer used to wait for
``pollCreation``, so every real poll fell through to "Mensagem incompatível".

ConversationsPanel and MainWindow need a wx.App, so their methods run against
plain stubs.
"""

import types

import pytest

from core import polls
from core.message_queue import PendingMessage
from core.websocket_client import WebSocketClient
from main import MainWindow
from tests.locales import load_strings, registered_locale_codes
from ui.conversations import ConversationsPanel

PHONE = "5511999999999@s.whatsapp.net"
GROUP = "120363000000000001@g.us"


class _I18n:
    def __init__(self, strings=None):
        self._strings = strings or {
            "poll_option_votes": "{name}, votes: {count}",
            "poll_row_question": "Poll: {name}",
            "poll_row_options": "{question}. Options: {options}",
            "notif_poll_no_name": "Poll",
        }

    def t(self, key):
        return self._strings.get(key, key)


def _raw_poll(**kw):
    raw = {
        "id": f"false_{PHONE}_3EB0POLL",
        "type": "poll_creation",
        "t": 1790222611,
        "from": PHONE,
        "to": "5500000000000@s.whatsapp.net",
        "fromMe": False,
        "pollName": "Pizza ou sushi?",
        "pollOptions": [{"name": "Pizza", "localId": 0}, {"name": "Sushi", "localId": 1}],
        "pollSelectableOptionsCount": 1,
    }
    raw.update(kw)
    return raw


class _Normalizer:
    _normalize_wpp_message = WebSocketClient._normalize_wpp_message
    _clean_jid = WebSocketClient._clean_jid


class TestValidation:
    def test_a_good_poll_has_no_error(self):
        assert polls.poll_input_error("Q?", ["a", "b"]) is None

    def test_the_question_is_required(self):
        assert polls.poll_input_error("   ", ["a", "b"]) == "poll_error_no_question"

    def test_blank_options_do_not_count(self):
        assert polls.poll_input_error("Q?", ["a", "  ", ""]) == "poll_error_too_few_options"

    def test_twelve_options_is_the_limit(self):
        assert polls.poll_input_error("Q?", [str(i) for i in range(12)]) is None
        assert (
            polls.poll_input_error("Q?", [str(i) for i in range(13)])
            == "poll_error_too_many_options"
        )

    def test_duplicates_are_refused_ignoring_case(self):
        assert polls.poll_input_error("Q?", ["Sim", " sim "]) == "poll_error_duplicate_option"

    def test_input_is_trimmed(self):
        assert polls.clean_poll_input(" Q? ", [" a ", "", "b "]) == ("Q?", ["a", "b"])

    def test_selectable_count(self):
        assert polls.selectable_count(False) == 1
        assert polls.selectable_count(True) == 0


class TestSendPayload:
    def test_shape_matches_the_node_route(self):
        payload = polls.poll_send_payload(
            PHONE, {"name": "Q?", "options": ["a", "b"], "selectableCount": 0},
            is_group=False, is_lid=False,
        )
        assert payload == {
            "phone": [PHONE], "isGroup": False, "isLid": False,
            "name": "Q?", "choices": ["a", "b"], "options": {"selectableCount": 0},
        }


class TestNormalizer:
    def test_poll_creation_becomes_a_poll_message(self):
        result = _Normalizer()._normalize_wpp_message(_raw_poll())
        assert result["messageType"] == "pollCreationMessage"
        assert result["message"]["pollCreationMessage"] == {
            "name": "Pizza ou sushi?",
            "options": [{"name": "Pizza"}, {"name": "Sushi"}],
            "selectableOptionsCount": 1,
        }

    def test_the_old_type_name_still_works(self):
        result = _Normalizer()._normalize_wpp_message(_raw_poll(type="pollCreation"))
        assert result["messageType"] == "pollCreationMessage"

    def test_a_multiple_choice_poll_is_read_as_such(self):
        result = _Normalizer()._normalize_wpp_message(_raw_poll(pollSelectableOptionsCount=0))
        poll = result["message"]["pollCreationMessage"]
        assert polls.allows_multiple(poll) is True
        assert polls.allows_multiple({"selectableOptionsCount": 1}) is False


class TestRow:
    POLL = {"name": "Q?", "options": [{"name": "a"}, {"name": "b", "votes": 3}]}

    def test_question_first_then_options_most_voted_first(self):
        assert polls.poll_row_text(_I18n(), self.POLL) == (
            "Poll: Q?. Options: b, votes: 3; a, votes: 0"
        )

    def test_a_poll_without_options_still_reads_as_a_poll(self):
        assert polls.poll_row_text(_I18n(), {"name": "Q?"}) == "Poll: Q?"
        assert polls.poll_row_text(_I18n(), {}) == "Poll"

    def test_the_message_list_uses_it(self):
        stub = type("Panel", (), {
            "_get_message_content": ConversationsPanel._get_message_content,
            "main_window": types.SimpleNamespace(i18n=_I18n()),
        })()
        msg = {
            "key": {"fromMe": False},
            "messageType": "pollCreationMessage",
            "message": {"pollCreationMessage": self.POLL},
        }
        assert stub._get_message_content(msg) == "Poll: Q?. Options: b, votes: 3; a, votes: 0"


class TestQueue:
    def test_pending_message_carries_the_poll(self):
        pm = PendingMessage("id", PHONE, poll={"name": "Q?", "options": ["a", "b"]})
        assert pm.poll["name"] == "Q?"


class _Response:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)

    def json(self):
        return self._payload


class _Sender:
    send_poll_message = MainWindow.send_poll_message
    _classify_send_exception = MainWindow._classify_send_exception

    def __init__(self, resolved=PHONE):
        self.wpp_server = "http://127.0.0.1"
        self.wpp_port = 6300
        self.token = "tok"
        self.i18n = _I18n()
        self._resolved = resolved

    def _resolve_jid_for_send(self, jid):
        return self._resolved

    def _legacy_phone_for_send(self, jid):
        return jid.replace("@lid", "@c.us")

    def _check_wa_connection_closed(self, response):
        return False


def _post(monkeypatch, *responses):
    calls = []
    queue = list(responses)

    def fake(url, **kwargs):
        calls.append((url, kwargs.get("json")))
        return queue.pop(0) if len(queue) > 1 else queue[0]

    from main_window import poll_sending
    monkeypatch.setattr(poll_sending, "api_post", fake)
    return calls


POLL = {"name": "Q?", "options": ["a", "b"], "selectableCount": 1}


class TestSending:
    def test_a_confirmed_send_returns_the_message_id(self, monkeypatch):
        calls = _post(monkeypatch, _Response(201, {
            "status": "success", "response": [{"id": "true_x_3EB0ABC", "ack": 0}],
        }))
        assert _Sender().send_poll_message(PHONE, POLL) == "3EB0ABC"
        assert calls[0][0].endswith("/send-poll-message")
        assert calls[0][1]["choices"] == ["a", "b"]

    def test_a_group_goes_without_its_suffix(self, monkeypatch):
        calls = _post(monkeypatch, _Response(201, {
            "status": "success", "response": [{"id": "true_x_3EB0ABC"}],
        }))
        _Sender(GROUP).send_poll_message(GROUP, POLL)
        assert calls[0][1]["phone"] == ["120363000000000001"]
        assert calls[0][1]["isGroup"] is True

    def test_an_audited_rejection_is_permanent(self, monkeypatch):
        _post(monkeypatch, _Response(201, {
            "status": "success",
            "response": [{"id": "x", "ack": -1, "error": "send-poll-message was rejected (ack=-1)"}],
        }))
        result = _Sender().send_poll_message(PHONE, POLL)
        assert result["ok"] is False and result["retry"] is False

    def test_a_server_error_is_never_resent(self, monkeypatch):
        calls = _post(monkeypatch, _Response(500, {"status": "error"}))
        result = _Sender().send_poll_message(PHONE, POLL)
        assert result["ambiguous"] is True and result["retry"] is False
        assert len(calls) == 1

    def test_a_refused_lid_falls_back_to_the_phone_form(self, monkeypatch):
        calls = _post(
            monkeypatch,
            _Response(400, {"status": "error"}),
            _Response(201, {"status": "success", "response": [{"id": "true_x_3EB0ABC"}]}),
        )
        lid = "10000000000001@lid"
        assert _Sender(lid).send_poll_message(lid, POLL) == "3EB0ABC"
        assert calls[1][1]["phone"] == ["10000000000001@c.us"]
        assert calls[1][1]["isLid"] is False


@pytest.mark.parametrize("locale", registered_locale_codes())
def test_every_locale_has_the_poll_strings(locale):
    strings = load_strings(locale)
    for key in (
        "attachment_poll", "new_poll_title", "poll_option_votes", "poll_row_question",
        "poll_row_options", "poll_dialog_title", "poll_error_duplicate_option",
    ):
        assert strings.get(key), (locale, key)
    assert "{count}" in strings["poll_option_votes"] and "{name}" in strings["poll_option_votes"]


class TestVoteBars:
    OPTIONS = [{"name": "a", "votes": 3}, {"name": "b", "votes": 1}, {"name": "c", "votes": 0}]

    def test_percent_is_the_share_of_all_votes(self):
        assert [polls.vote_percent(o, self.OPTIONS) for o in self.OPTIONS] == [75, 25, 0]

    def test_no_votes_is_zero_everywhere(self):
        empty = [{"name": "a", "votes": 0}]
        assert polls.vote_percent(empty[0], empty) == 0
