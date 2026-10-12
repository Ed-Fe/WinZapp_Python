"""A transcription in the message's own line, and the key that decides how.

Alt+Shift+N is the action Enter and Space do not do on a voice message: with
"Enter and Space transcribe" off (Enter plays) it transcribes the message into
its line of the list; with it on, it plays or pauses the message and Enter and
Space transcribe. ConversationsPanel needs a wx.App, so the methods run against
a plain stub, and nothing here opens a window.
"""

import ast
import pathlib
import types

import pytest

from core.transcription import stored
from tests.locales import load_strings, registered_locale_codes
from ui import transcription_flow
from ui.conversations import ConversationsPanel

CLIENT = pathlib.Path(__file__).resolve().parents[1] / "client"
LOCALES = list(registered_locale_codes())
TEXT = "olá, tudo bem com você"


class _I18n:
    def __init__(self, locale="pt-BR"):
        self.language = locale
        self._table = load_strings(locale)

    def t(self, key):
        return self._table[key]


def _voice(inline=True, text=TEXT, seconds=12, msg_id="A1"):
    msg = {
        "key": {"id": msg_id, "remoteJid": "1@s.whatsapp.net", "fromMe": False},
        "messageType": "audioMessage",
        "message": {"audioMessage": {"seconds": seconds, "ptt": True,
                                     "mimetype": "audio/ogg; codecs=opus"}},
    }
    if text is not None:
        msg[stored.TRANSCRIPTION_KEY] = {"text": text, "at": 1.0, **({"inline": True} if inline else {})}
    return msg


class _Stub:
    _get_message_content = ConversationsPanel._get_message_content
    _format_duration = ConversationsPanel._format_duration
    _enter_transcribes_voice = ConversationsPanel._enter_transcribes_voice
    activate_message = ConversationsPanel.activate_message
    _space_toggles_playback = ConversationsPanel._space_toggles_playback
    _is_separator = ConversationsPanel._is_separator
    _on_menu_toggle_voice_action = ConversationsPanel._on_menu_toggle_voice_action
    _on_accel_transcribe_inline = ConversationsPanel._on_accel_transcribe_inline
    _video_default_action = ConversationsPanel._video_default_action

    def __init__(self, enter_transcribes=False, locale="pt-BR"):
        ui = {"enter_transcribes_voice_messages": True} if enter_transcribes else {}
        self.main_window = types.SimpleNamespace(
            i18n=_I18n(locale), app_name="WinZapp", settings={"user_interface": ui}
        )
        self.played = []
        self.transcribed = []
        self._sorted_messages = []
        self.messages_list = types.SimpleNamespace(GetFirstSelected=lambda: 0)

    def _toggle_audio_message_playback(self, msg):
        self.played.append(msg["key"]["id"])

    def _on_menu_transcribe_inline(self, msg):
        self.transcribed.append(msg["key"]["id"])

    def _use_conversation_video_media_viewer_dialog(self):
        return True

    def open_media_viewer_for_message(self, msg, restore_index=None):
        self.played.append("viewer:" + msg["key"]["id"])

    def _play_toggle_video_message(self, msg):
        self.played.append("inplace:" + msg["key"]["id"])


def _video(text=TEXT, inline=True, gif=False, msg_id="V1"):
    msg = {
        "key": {"id": msg_id, "remoteJid": "1@s.whatsapp.net", "fromMe": False},
        "messageType": "videoMessage",
        "message": {"videoMessage": {"seconds": 30, "mimetype": "video/mp4",
                                     **({"gifPlayback": True} if gif else {})}},
    }
    if text is not None:
        msg[stored.TRANSCRIPTION_KEY] = {"text": text, "at": 1.0, **({"inline": True} if inline else {})}
    return msg


class TestVideos:
    def test_the_row_carries_the_inline_text(self):
        row = _Stub()._get_message_content(_video())
        assert row.endswith(f"{_I18n().t('transcription_row_label')}: {TEXT}")

    def test_by_default_enter_opens_it_and_the_key_transcribes(self):
        panel = _Stub()
        panel.activate_message(_video(text=None))
        panel._on_menu_toggle_voice_action(_video(text=None))
        assert panel.played == ["viewer:V1"] and panel.transcribed == ["V1"]

    def test_set_to_transcribe_enter_transcribes_and_the_key_opens_it(self):
        panel = _Stub(enter_transcribes=True)
        panel.activate_message(_video(text=None))
        panel._on_menu_toggle_voice_action(_video(text=None))
        assert panel.transcribed == ["V1"] and panel.played == ["viewer:V1"]

    def test_space_transcribes_when_set_to(self):
        panel = _Stub(enter_transcribes=True)
        assert panel._space_toggles_playback(_video(text=None)) is True
        assert panel.transcribed == ["V1"]

    def test_classic_mode_plays_it_in_place(self):
        panel = _Stub(enter_transcribes=True)
        panel._use_conversation_video_media_viewer_dialog = lambda: False
        panel._on_menu_toggle_voice_action(_video(text=None))
        assert panel.played == ["inplace:V1"]

    def test_a_gif_is_never_transcribed_by_enter(self):
        panel = _Stub(enter_transcribes=True)
        panel.activate_message(_video(text=None, gif=True))
        assert panel.transcribed == []


class TestTheRow:
    def test_the_text_follows_the_duration(self):
        row = _Stub()._get_message_content(_voice())
        i18n = _I18n()
        assert row == (
            f"{i18n.t('message_type_voice_message')}, {i18n.t('duration')}: "
            f"{_Stub()._format_duration(12)}, {i18n.t('transcription_row_label')}: {TEXT}"
        )

    def test_a_transcription_opened_in_the_window_leaves_the_row_short(self):
        row = _Stub()._get_message_content(_voice(inline=False))
        assert _I18n().t("transcription_row_label") not in row

    def test_no_transcription_no_clause(self):
        row = _Stub()._get_message_content(_voice(text=None))
        assert _I18n().t("transcription_row_label") not in row

    def test_a_note_of_unknown_length_still_carries_its_text(self):
        row = _Stub()._get_message_content(_voice(seconds=0))
        i18n = _I18n()
        assert row == (
            f"{i18n.t('message_type_voice_message')}, "
            f"{i18n.t('transcription_row_label')}: {TEXT}"
        )


class TestEnterAndSpace:
    def test_by_default_they_play(self):
        panel = _Stub()
        panel.activate_message(_voice())
        assert panel.played == ["A1"] and panel.transcribed == []

    def test_set_to_transcribe_they_transcribe_instead(self):
        panel = _Stub(enter_transcribes=True)
        panel.activate_message(_voice())
        assert panel.transcribed == ["A1"] and panel.played == []

    def test_space_follows_the_same_choice(self):
        panel = _Stub(enter_transcribes=True)
        assert panel._space_toggles_playback(_voice()) is True
        assert panel.transcribed == ["A1"] and panel.played == []

    def test_only_an_explicit_true_changes_anything(self):
        panel = _Stub()
        panel.main_window.settings["user_interface"]["enter_transcribes_voice_messages"] = "yes"
        panel.activate_message(_voice())
        assert panel.played == ["A1"]


class TestCtrlShiftT:
    def test_with_enter_playing_it_transcribes_into_the_line(self):
        panel = _Stub()
        panel._on_menu_toggle_voice_action(_voice())
        assert panel.transcribed == ["A1"] and panel.played == []

    def test_with_enter_transcribing_it_plays_instead(self):
        panel = _Stub(enter_transcribes=True)
        panel._on_menu_toggle_voice_action(_voice())
        assert panel.played == ["A1"] and panel.transcribed == []

    def test_the_two_keys_never_do_the_same_thing(self):
        for setting in (False, True):
            panel = _Stub(enter_transcribes=setting)
            panel.activate_message(_voice())
            panel._on_menu_toggle_voice_action(_voice())
            assert sorted([panel.played, panel.transcribed]) == [["A1"], ["A1"]]

    def test_the_key_acts_on_the_focused_message(self):
        panel = _Stub()
        panel._sorted_messages = [_voice(msg_id="B2")]
        panel._on_accel_transcribe_inline(None)
        assert panel.transcribed == ["B2"]


class TestWhatIsSaid:
    def test_the_text_then_the_voice_filter_warning(self):
        from core.transcription import narration

        i18n = _I18n("en-US")
        note = narration.Note(narration.VAD_UNAVAILABLE_I18N_KEY)
        sentence = transcription_flow.inline_speech(i18n, "hello\nthere", (note,))
        assert sentence.startswith("Transcription: hello there")
        assert i18n.t(narration.VAD_UNAVAILABLE_I18N_KEY) in sentence

    def test_without_a_warning_it_is_only_the_text(self):
        assert transcription_flow.inline_speech(_I18n("en-US"), "hi", ()) == (
            "Transcription: hi"
        )


class TestTheWiring:
    def test_the_key_is_in_the_messages_table_and_bound(self):
        source = (CLIENT / "ui/conversation_panel/accelerators.py").read_text(encoding="utf-8")
        assert 'ord("N"),          self.ID_ALT_SHIFT_N' in source
        assert "self._on_accel_transcribe_inline,   id=self.ID_ALT_SHIFT_N" in source

    def test_it_is_in_the_catalogue_with_ctrl_shift_t(self):
        from core.shortcut_catalog import SHORTCUTS

        row = next(s for s in SHORTCUTS if s.id == "messages.ID_ALT_SHIFT_N")
        assert (row.mod, row.key) == (5, ord("N"))
        assert row.label == "shortcut_alt_shift_n_label"

    def test_the_f1_list_describes_it(self):
        source = (CLIENT / "ui/dialogs/shortcuts_dialog.py").read_text(encoding="utf-8")
        assert 'i18n.t("shortcut_alt_shift_n_label")' in source

    def test_the_menu_item_names_the_action_the_setting_leaves(self):
        source = (CLIENT / "ui/conversation_panel/message_menu.py").read_text(encoding="utf-8")
        assert '"play_voice_message_menu"' in source
        assert '"transcribe_inline_menu"' in source
        assert "\\tAlt+Shift+N" in source

    def test_the_setting_is_off_by_default_in_both_defaults(self):
        import json

        from core.utils import DEFAULT_SETTINGS

        assert DEFAULT_SETTINGS["user_interface"]["enter_transcribes_voice_messages"] is False
        defaults = json.loads((CLIENT / "data/settings_default.json").read_text(encoding="utf-8"))
        assert defaults["user_interface"]["enter_transcribes_voice_messages"] is False


@pytest.mark.parametrize("locale", LOCALES)
class TestTheStringsExplainIt:
    def test_the_checkbox_names_both_keys_and_both_ways(self, locale):
        label = load_strings(locale)["ui_enter_transcribes_voice"]
        assert label.count("Alt+Shift+N") == 2
        assert "Enter" in label

    def test_the_f1_line_and_the_menu_say_local(self, locale):
        table = load_strings(locale)
        words = {"tr-TR": ("yerel", "bu bilgisayarda"), "pl": ("lokaln",)}.get(locale, ("local",))
        for key in ("transcribe_inline_menu", "shortcut_alt_shift_n_label"):
            assert any(w in table[key].lower() for w in words), (locale, key)

    def test_the_spoken_sentence_carries_the_text(self, locale):
        assert "{text}" in load_strings(locale)["transcription_inline_spoken"]
