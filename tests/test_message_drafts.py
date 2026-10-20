"""A draft stays with the conversation it was typed in, like WhatsApp Web.

Reported: type a message, leave it unsent, open another conversation, and the
text was still in the field there, where Enter sent it to the wrong person.

core/message_drafts.py directly, and the mixin's unbound methods on a plain
stub; no window opens.
"""

import inspect
from types import SimpleNamespace

from core.message_drafts import MessageDrafts
from ui.conversation_panel import conversation_navigation
from ui.conversation_panel.composer import ComposerMixin
from ui.conversation_panel.message_drafts import MessageDraftsMixin


class TestMessageDrafts:
    def test_one_per_conversation_handed_back_once(self):
        drafts = MessageDrafts()
        drafts.stash("a@s.whatsapp.net", "oi Ana", ["ana@lid"], {"ana@lid": "Ana"})
        drafts.stash("b@s.whatsapp.net", "oi Bia")
        draft = drafts.take("a@s.whatsapp.net")
        assert (draft.text, draft.mentions, draft.mention_names) == (
            "oi Ana", ["ana@lid"], {"ana@lid": "Ana"})
        assert drafts.take("a@s.whatsapp.net") is None
        assert drafts.take("b@s.whatsapp.net").text == "oi Bia"

    def test_a_blank_field_forgets_the_draft(self):
        drafts = MessageDrafts()
        drafts.stash("a", "rascunho")
        drafts.stash("a", "   \n")
        assert drafts.take("a") is None and len(drafts) == 0

    def test_no_conversation_keeps_nothing(self):
        drafts = MessageDrafts()
        drafts.stash("", "texto")
        assert len(drafts) == 0 and drafts.take("") is None


class _Field:
    def __init__(self, value=""):
        self.value = value
        self.sets = []

    def GetValue(self):
        return self.value

    def SetValue(self, value):
        self.value = value
        self.sets.append(value)

    def SetInsertionPointEnd(self):
        pass


class _Panel(MessageDraftsMixin):
    def __init__(self):
        self.main_window = SimpleNamespace(_normalize_jid=lambda jid: jid.replace("@c.us", "@s.whatsapp.net"))
        self.message_field = _Field()
        self.conversation = None
        self._editing_message_id = None
        self._pending_mentions = []
        self._pending_mention_display_names = {}
        self.cancelled_edits = 0
        self.pills = 0

    def _on_cancel_edit(self):
        self.cancelled_edits += 1
        self._editing_message_id = None
        self.message_field.value = ""

    def _rebuild_mention_pills(self):
        self.pills += 1

    def open(self, jid):
        """What navigate_to_conversation() does around the switch."""
        self._stash_message_draft()
        self.conversation = {"remoteJid": jid}
        self._restore_message_draft()


ANA, BIA = "ana@s.whatsapp.net", "bia@s.whatsapp.net"


class TestSwitchingConversations:
    def test_the_text_stays_with_its_conversation(self):
        panel = _Panel()
        panel.open(ANA)
        panel.message_field.value = "te ligo depois"
        panel.open(BIA)
        assert panel.message_field.value == ""
        panel.message_field.value = "e aí?"
        panel.open(ANA)
        assert panel.message_field.value == "te ligo depois"
        panel.open(BIA)
        assert panel.message_field.value == "e aí?"

    def test_the_mentions_come_back_with_the_text(self):
        panel = _Panel()
        panel.open(ANA)
        panel.message_field.value = "@Carla olha isso"
        panel._pending_mentions = ["carla@lid"]
        panel._pending_mention_display_names = {"carla@lid": "Carla"}
        panel.open(BIA)
        panel._pending_mentions, panel._pending_mention_display_names = [], {}
        panel.open(ANA)
        assert panel._pending_mentions == ["carla@lid"]
        assert panel._pending_mention_display_names == {"carla@lid": "Carla"}
        assert panel.pills == 1

    def test_the_text_being_edited_is_not_a_draft(self):
        panel = _Panel()
        panel.open(ANA)
        panel._editing_message_id = "MSG"
        panel.message_field.value = "texto editado"
        panel.open(BIA)
        assert panel.cancelled_edits == 1
        panel.open(ANA)
        assert panel.message_field.value == ""

    def test_the_same_chat_under_its_legacy_jid_finds_its_draft(self):
        panel = _Panel()
        panel.open(ANA)
        panel.message_field.value = "rascunho"
        panel.open(BIA)
        panel.open("ana@c.us")
        assert panel.message_field.value == "rascunho"


def test_the_field_is_set_with_the_restore_flag_raised():
    seen = []
    panel = _Panel()
    panel.message_field.SetValue = lambda value: seen.append(panel._restoring_draft)
    panel._put_in_message_field("texto")
    assert seen == [True] and panel._restoring_draft is False


def test_a_restored_draft_tells_no_one_it_is_being_typed():
    sent = []
    panel = SimpleNamespace(
        _is_recording=False, _attachment_panel=SimpleNamespace(IsShown=lambda: False),
        message_field=_Field("rascunho"), _restoring_draft=True, _spell_checker=None,
        send_message_btn=SimpleNamespace(Show=lambda: None, Hide=lambda: None),
        record_voice_message_btn=SimpleNamespace(Show=lambda: None, Hide=lambda: None),
        _record_voice_alt_btn=SimpleNamespace(Show=lambda: None, Hide=lambda: None),
        conversation={"remoteJid": ANA}, _is_typing=False,
        main_window=SimpleNamespace(send_typing_status=lambda *a: sent.append(a)),
    )
    ComposerMixin.on_change_message_field(panel, None)
    assert sent == []


def test_navigation_and_closing_keep_the_draft():
    source = inspect.getsource(conversation_navigation)
    navigate = source[source.index("    def navigate_to_conversation("):]
    navigate = navigate[:navigate.index("\n    def ", 10)]
    assert navigate.index("self._stash_message_draft()") < navigate.index(
        "self.conversation = conversation\n        self._restore_message_draft()")
    close = source[source.index("    def _close_conversation_core("):]
    close = close[:close.index("\n    def ", 10)]
    assert close.index("self._stash_message_draft()") < close.index("self.conversation = None")


def _with_mode(panel, mode):
    panel.main_window.settings = {"user_interface": {"message_draft_mode": mode}}
    return panel


class TestTheThreeModes:
    """Settings > Interface > Conversations: keep the text in the field (as
    before), one draft per conversation (default), or discard it on leaving."""

    def test_per_conversation_by_default(self):
        from core import message_drafts
        from core.utils import DEFAULT_SETTINGS
        assert DEFAULT_SETTINGS["user_interface"]["message_draft_mode"] == "per_conversation"
        assert message_drafts.mode({}) == message_drafts.PER_CONVERSATION
        assert message_drafts.mode({"user_interface": {"message_draft_mode": "bogus"}})             == message_drafts.PER_CONVERSATION

    def test_discard_throws_the_text_away_on_leaving(self):
        panel = _with_mode(_Panel(), "discard")
        panel.open(ANA)
        panel.message_field.value = "não vou mandar"
        panel.open(BIA)
        assert panel.message_field.value == ""
        panel.open(ANA)
        assert panel.message_field.value == ""

    def test_a_draft_kept_before_switching_to_discard_is_not_brought_back(self):
        panel = _Panel()
        panel.open(ANA)
        panel.message_field.value = "guardado antes"
        panel.open(BIA)
        _with_mode(panel, "discard")
        panel.open(ANA)
        assert panel.message_field.value == ""

    def test_keep_in_field_leaves_the_field_as_it_always_was(self):
        panel = _with_mode(_Panel(), "keep_in_field")
        panel.open(ANA)
        panel.message_field.value = "continua aqui"
        panel.open(BIA)
        assert panel.message_field.value == "continua aqui"
        assert panel.message_field.sets == []
