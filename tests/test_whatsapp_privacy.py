"""File > WhatsApp settings > Privacy: what is shown, what is sent, and what
counts as done (core/whatsapp_privacy.py), the server calls
(main_window/whatsapp_privacy.py) and the people offered for a Status audience
(ui/dialogs/status_audience_dialog.audience_rows). No window is opened.
"""

import pytest

from core import whatsapp_privacy as privacy
from main_window import whatsapp_privacy as mixin_module
from main_window.whatsapp_privacy import WhatsAppPrivacyMixin

SIX = {
    "lastSeen": "contacts", "online": "all", "about": "all",
    "profilePicture": "contacts", "readReceipts": "all", "groupAdd": "contacts",
}


def _field(setting):
    return next(f for f in privacy.PRIVACY_FIELDS if f.setting == setting)


# ── Account settings ─────────────────────────────────────────────────────────

class TestAccountSettings:
    def test_every_field_offers_only_values_the_server_accepts(self):
        # Mirrors ACCOUNT_SETTINGS in api_patches/src/controller/privacyController.ts.
        accepted = {
            "lastSeen": {"all", "contacts", "none"},
            "online": {"all", "match_last_seen"},
            "about": {"all", "contacts", "none"},
            "profilePicture": {"all", "contacts", "none"},
            "readReceipts": {"all", "none"},
            "groupAdd": {"all", "contacts"},
        }
        assert {f.setting: {v for v, _k in f.options} for f in privacy.PRIVACY_FIELDS} == accepted

    def test_an_unknown_value_leaves_the_dropdown_empty(self):
        assert privacy.option_index(_field("about"), "contact_blacklist") == -1
        assert privacy.option_index(_field("about"), "contacts") == 1

    def test_an_answer_with_no_known_value_is_unreadable(self):
        assert privacy.unreadable({"lastSeen": None})
        assert privacy.unreadable(None)
        assert not privacy.unreadable(SIX)

    def test_unrecognized_fields_are_named(self):
        settings = {**SIX, "lastSeen": "contact_blacklist"}
        assert [f.setting for f in privacy.unrecognized_fields(settings)] == ["lastSeen"]

    def test_only_what_changed_is_sent(self):
        chosen = {"lastSeen": 2, "online": 0, "about": -1}
        assert privacy.changed_privacy(SIX, chosen) == [("lastSeen", "none")]

    def test_a_setting_not_read_back_is_not_confirmed(self):
        readback = {**SIX, "lastSeen": "contacts"}
        assert privacy.not_confirmed([("lastSeen", "none")], readback) == ["lastSeen"]
        assert privacy.not_confirmed([("lastSeen", "contacts")], readback) == []
        assert privacy.not_confirmed([("about", "none")], None) == ["about"]


# ── Who can see my Status ────────────────────────────────────────────────────

class TestStatusPrivacy:
    @pytest.mark.parametrize("raw,expected", [
        ("contact", privacy.STATUS_CONTACTS),
        ("contacts", privacy.STATUS_CONTACTS),
        ("deny-list", privacy.STATUS_EXCEPT),
        ("blacklist", privacy.STATUS_EXCEPT),
        ("allow-list", privacy.STATUS_ONLY),
        ("whitelist", privacy.STATUS_ONLY),
    ])
    def test_known_spellings(self, raw, expected):
        assert privacy.parse_status_privacy({"setting": raw, "list": []}).setting == expected

    def test_an_unknown_setting_leaves_status_out(self):
        assert privacy.parse_status_privacy({"setting": "everyone"}) is None
        assert privacy.parse_status_privacy(None) is None

    def test_a_list_that_was_not_read_is_none_not_empty(self):
        parsed = privacy.parse_status_privacy({"setting": "deny-list", "list": None})
        assert parsed.people is None

    @pytest.mark.parametrize("jid,expected", [
        ("5551999998888@s.whatsapp.net", "5551999998888@c.us"),
        ("5551999998888:12@s.whatsapp.net", "5551999998888@c.us"),
        ("5551999998888@c.us", "5551999998888@c.us"),
        ("123456789012345@lid", "123456789012345@lid"),
        ("120363000000000000@g.us", ""),
        ("status@broadcast", ""),
        ("", ""),
    ])
    def test_status_list_ids(self, jid, expected):
        assert privacy.jid_for_status_list(jid) == expected

    def test_unchanged_choice_sends_nothing(self):
        loaded = privacy.StatusPrivacy(privacy.STATUS_EXCEPT, ("5551999998888@c.us",))
        assert privacy.status_change(loaded, privacy.STATUS_EXCEPT, None) is None
        assert privacy.status_change(
            loaded, privacy.STATUS_EXCEPT, ["5551999998888@s.whatsapp.net"]) is None

    def test_my_contacts_never_sends_a_list(self):
        loaded = privacy.StatusPrivacy(privacy.STATUS_ONLY, ("5551999998888@c.us",))
        assert privacy.status_change(loaded, privacy.STATUS_CONTACTS, None) == (
            privacy.STATUS_CONTACTS, [])

    def test_picked_people_are_sent_once_each(self):
        loaded = privacy.StatusPrivacy(privacy.STATUS_CONTACTS, ())
        change = privacy.status_change(loaded, privacy.STATUS_ONLY, [
            "5551999998888@s.whatsapp.net", "5551999998888@c.us", "120363@g.us"])
        assert change == (privacy.STATUS_ONLY, ["5551999998888@c.us"])

    def test_a_list_that_could_not_be_read_is_only_replaced_on_purpose(self):
        loaded = privacy.StatusPrivacy(privacy.STATUS_EXCEPT, None)
        assert privacy.status_change(loaded, privacy.STATUS_EXCEPT, None) is None
        assert privacy.status_change(loaded, privacy.STATUS_EXCEPT, ["5551999998888@c.us"]) == (
            privacy.STATUS_EXCEPT, ["5551999998888@c.us"])

    def test_only_share_with_nobody_is_refused(self):
        loaded = privacy.StatusPrivacy(privacy.STATUS_CONTACTS, ())
        change = privacy.status_change(loaded, privacy.STATUS_ONLY, None)
        assert privacy.status_needs_people(change)
        assert not privacy.status_needs_people(
            privacy.status_change(loaded, privacy.STATUS_EXCEPT, None))

    def test_confirmation_compares_the_setting_read_back(self):
        assert not privacy.status_not_confirmed(privacy.STATUS_EXCEPT, "deny-list")
        assert privacy.status_not_confirmed(privacy.STATUS_EXCEPT, "contact")
        assert privacy.status_not_confirmed(privacy.STATUS_EXCEPT, None)


# ── Server calls ─────────────────────────────────────────────────────────────

class _Response:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body

    def json(self):
        return self._body


class _Stub(WhatsAppPrivacyMixin):
    wpp_server = "http://127.0.0.1"
    wpp_port = 6300
    token = "SESSION:TOKEN"


@pytest.fixture
def posts(monkeypatch):
    calls = []
    answers = []

    def fake_post(url, json=None, headers=None, timeout=None):
        calls.append((url.rsplit("/api/SESSION:TOKEN/", 1)[1], json))
        return answers.pop(0)

    monkeypatch.setattr(mixin_module, "api_post", fake_post)
    return calls, answers


class TestServerCalls:
    def test_read_returns_settings_and_status(self, posts):
        calls, answers = posts
        answer = {"settings": SIX, "status": {"setting": "contact", "list": []}}
        answers.append(_Response(200, {"status": "success", "response": answer}))
        assert _Stub().fetch_privacy_settings() == answer
        assert calls == [("privacy", {})]

    def test_a_failed_read_is_none(self, posts):
        _calls, answers = posts
        answers.append(_Response(503, {"status": "error", "code": "privacy_unavailable"}))
        assert _Stub().fetch_privacy_settings() is None

    def test_a_read_without_settings_is_none(self, posts):
        _calls, answers = posts
        answers.append(_Response(200, {"status": "success", "response": {"status": None}}))
        assert _Stub().fetch_privacy_settings() is None

    def test_a_failed_setter_reports_its_code_and_kind(self, posts):
        calls, answers = posts
        answers.append(_Response(502, {"code": "privacy_setting_failed", "kind": "TypeError"}))
        assert _Stub().set_privacy_setting("about", "none") == "privacy_setting_failed (TypeError)"
        assert calls == [("privacy/set", {"setting": "about", "value": "none"})]

    def test_status_audience_answers_with_the_setting_read_back(self, posts):
        calls, answers = posts
        answers.append(_Response(200, {"status": "success", "response": {"setting": "deny-list"}}))
        assert _Stub().set_status_privacy("deny-list", ["5551999998888@c.us"]) == ("deny-list", None)
        assert calls == [("privacy/status", {"setting": "deny-list", "list": ["5551999998888@c.us"]})]

    def test_an_exception_is_an_error_string_not_a_crash(self, monkeypatch):
        def boom(*args, **kwargs):
            raise ConnectionError("refused")

        monkeypatch.setattr(mixin_module, "api_post", boom)
        assert _Stub().fetch_privacy_settings() is None
        assert _Stub().set_privacy_setting("about", "all") == "ConnectionError"
        assert _Stub().set_status_privacy("contact", []) == (None, "ConnectionError")


# ── People offered for a Status audience ─────────────────────────────────────

class _I18n:
    def t(self, key):
        return {"wa_privacy_status_unknown_contact": "Not in your contacts ({number})"}.get(key, key)


class _Mw:
    def __init__(self, contacts):
        self.contacts = contacts
        self.chats = {}
        self._lid_to_phone = {"999999999999999@lid": "5551999998888@s.whatsapp.net"}
        self.i18n = _I18n()

    def _normalize_jid(self, jid):
        return jid.replace("@c.us", "@s.whatsapp.net")


class TestAudienceRows:
    def test_contacts_then_people_only_whatsapp_knows(self):
        from ui.dialogs.status_audience_dialog import audience_rows

        mw = _Mw({
            "5551999998888@s.whatsapp.net": {"name": "Bruno", "isMyContact": True},
            "5551988887777@s.whatsapp.net": {"name": "Ana", "isMyContact": True},
        })
        rows = audience_rows(mw, ["999999999999999@lid", "5511912345678@c.us"])
        jids = [jid for _key, _label, jid in rows]
        # Ana and Bruno once each (Bruno's @lid is the same person), then the
        # one person on WhatsApp's list who is not a synchronised contact.
        assert jids == ["5551988887777@s.whatsapp.net", "5551999998888@s.whatsapp.net",
                        "5511912345678@c.us"]
        assert rows[-1][1].startswith("Not in your contacts (")
