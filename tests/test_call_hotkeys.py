"""Answer, reject and hang up a call from any window (global keys).

The keys are Windows hotkeys held only while they can act: "answer" while a
call that can be answered rings, "end" while one rings or a call is in
progress. Nothing here registers a real hotkey or opens a window: the manager
is replaced by a recorder and the mixin runs on a plain stub.
"""

import inspect

import pytest

import main_window.call_hotkeys as call_hotkeys
from core.call_logic import (
    CALL_ANSWER_HOTKEY,
    CALL_END_HOTKEY,
    CALL_HOTKEY_DEFAULTS,
    call_hotkey_is_valid,
    call_hotkey_setting,
    first_answerable_call,
)
from main_window.call_hotkeys import CallHotkeysMixin
from main_window.calls import CallsMixin


class _Manager:
    created = []

    def __init__(self, vk, mod, callback):
        self.vk, self.mod, self.callback = vk, mod, callback
        self.stopped = False
        _Manager.created.append(self)

    def stop(self, wait=False):
        self.stopped = True


class _I18n:
    def t(self, key):
        return f"<{key}>"


class _Button:
    def __init__(self):
        self.focused = False

    def SetFocus(self):
        self.focused = True


class _CallWindow:
    def __init__(self, shown=True):
        self.shown = shown
        self.raised = False

    def IsShown(self):
        return self.shown

    def GetHandle(self):
        return 0x1234

    def Raise(self):
        self.raised = True


class _Window(CallHotkeysMixin):
    _has_answerable_incoming_call = CallsMixin._has_answerable_incoming_call

    def __init__(self, general=None):
        self._active_incoming_calls = {}
        self._incoming_call_details = {}
        self._active_voice_call = None
        self.settings = {"general": general or {}}
        self.i18n = _I18n()
        self.accepted, self.rejected, self.spoken, self.ended = [], [], [], []
        self.voice_call_window = _CallWindow()
        self.voice_call_window_mute_button = _Button()

    def ring(self, identity, **details):
        self._active_incoming_calls[identity] = "peer"
        self._incoming_call_details[identity] = details

    def accept_incoming_call(self, identity, *, with_video=None):
        self.accepted.append((identity, with_video))

    def reject_incoming_call(self, identity):
        self.rejected.append(identity)

    def end_active_call(self, _event=None):
        self.ended.append(True)

    def output(self, text, interrupt=False):
        self.spoken.append((text, interrupt))


@pytest.fixture(autouse=True)
def recorder(monkeypatch):
    _Manager.created = []
    monkeypatch.setattr(call_hotkeys, "_HotkeyManager", _Manager)
    return _Manager


def _held(window):
    return {name: key for name, (_m, key) in window._call_hotkey_managers.items()}


class TestWhichCall:
    def test_the_oldest_answerable_call_is_chosen(self):
        details = {"a": {}, "b": {}}
        assert first_answerable_call({"a": "x", "b": "y"}, details) == "a"

    def test_a_group_offer_ringing_first_is_skipped(self):
        details = {"g": {"is_group": True}, "a": {}}
        assert first_answerable_call({"g": "x", "a": "y"}, details) == "a"

    def test_only_a_group_offer_leaves_nothing_to_answer(self):
        assert first_answerable_call({"g": "x"}, {"g": {"is_group": True}}) == ""

    def test_no_call_leaves_nothing_to_answer(self):
        assert first_answerable_call({}, {}) == ""
        assert first_answerable_call(None, None) == ""


class TestTheSavedKeys:
    def test_the_shipped_keys(self):
        assert CALL_ANSWER_HOTKEY == (0x0D, 0x0007)   # Ctrl+Alt+Shift+Enter
        assert CALL_END_HOTKEY == (0x08, 0x0007)      # Ctrl+Alt+Shift+Backspace

    def test_nothing_saved_gives_the_shipped_key(self):
        assert call_hotkey_setting({}, "call_answer_hotkey") == CALL_ANSWER_HOTKEY
        assert call_hotkey_setting(None, "call_end_hotkey") == CALL_END_HOTKEY

    def test_a_saved_key_wins(self):
        general = {"call_answer_hotkey": {"vk": 0x41, "mod": 0x0003}}
        assert call_hotkey_setting(general, "call_answer_hotkey") == (0x41, 0x0003)

    def test_a_removed_key_is_none(self):
        general = {"call_end_hotkey": {"vk": 0, "mod": 0}}
        assert call_hotkey_setting(general, "call_end_hotkey") is None

    @pytest.mark.parametrize("junk", [
        None, "x", [], {}, {"vk": 13}, {"vk": "13", "mod": 7}, {"vk": True, "mod": 7},
        {"vk": 13, "mod": 0}, {"vk": 13, "mod": 4}, {"vk": 999, "mod": 7},
        {"vk": 13, "mod": 0x8000}, {"vk": 0, "mod": 7},
    ])
    def test_an_unusable_value_falls_back_to_the_shipped_key(self, junk):
        assert not call_hotkey_is_valid(junk)
        assert call_hotkey_setting({"call_answer_hotkey": junk}, "call_answer_hotkey") \
            == CALL_ANSWER_HOTKEY

    def test_every_shipped_key_is_valid(self):
        for vk, mod in CALL_HOTKEY_DEFAULTS.values():
            assert call_hotkey_is_valid({"vk": vk, "mod": mod})


class TestHoldingTheKeys:
    def test_nothing_is_registered_while_idle(self, recorder):
        window = _Window()
        window._sync_call_hotkeys()
        assert recorder.created == []

    def test_a_ringing_call_registers_both_keys(self, recorder):
        window = _Window()
        window.ring("a")
        window._sync_call_hotkeys()
        assert _held(window) == {"answer": CALL_ANSWER_HOTKEY, "end": CALL_END_HOTKEY}

    def test_a_call_in_progress_holds_only_the_end_key(self, recorder):
        window = _Window()
        window._active_voice_call = {"call_id": "c"}
        window._sync_call_hotkeys()
        assert _held(window) == {"end": CALL_END_HOTKEY}

    def test_answering_leaves_the_end_key_in_place(self, recorder):
        window = _Window()
        window.ring("a")
        window._sync_call_hotkeys()
        end_manager = window._call_hotkey_managers["end"][0]
        window._active_incoming_calls.clear()
        window._active_voice_call = {"call_id": "a"}
        window._sync_call_hotkeys()
        assert _held(window) == {"end": CALL_END_HOTKEY}
        assert window._call_hotkey_managers["end"][0] is end_manager
        assert not end_manager.stopped

    def test_a_second_sync_does_not_register_again(self, recorder):
        window = _Window()
        window.ring("a")
        window._sync_call_hotkeys()
        window._sync_call_hotkeys()
        assert len(recorder.created) == 2

    def test_the_keys_are_released_when_the_call_is_over(self, recorder):
        window = _Window()
        window.ring("a")
        window._sync_call_hotkeys()
        window._active_incoming_calls.clear()
        window._sync_call_hotkeys()
        assert all(m.stopped for m in recorder.created)
        assert _held(window) == {}

    def test_a_group_offer_alone_takes_nothing(self, recorder):
        window = _Window()
        window.ring("g", is_group=True)
        window._sync_call_hotkeys()
        assert recorder.created == []

    def test_changed_settings_move_the_key_of_a_call_that_rings(self, recorder):
        window = _Window()
        window.ring("a")
        window._sync_call_hotkeys()
        window.settings["general"]["call_answer_hotkey"] = {"vk": 0x41, "mod": 0x0003}
        window._sync_call_hotkeys()
        assert _held(window)["answer"] == (0x41, 0x0003)
        assert recorder.created[0].stopped          # the old answer key is released
        assert _held(window)["end"] == CALL_END_HOTKEY

    def test_a_removed_key_is_not_registered(self, recorder):
        window = _Window({"call_answer_hotkey": {"vk": 0, "mod": 0}})
        window.ring("a")
        window._sync_call_hotkeys()
        assert _held(window) == {"end": CALL_END_HOTKEY}

    def test_a_registration_error_does_not_break_the_call_alert(self, monkeypatch):
        def boom(*_args):
            raise OSError("no message loop")
        monkeypatch.setattr(call_hotkeys, "_HotkeyManager", boom)
        window = _Window()
        window.ring("a")
        window._sync_call_hotkeys()


class TestPressingTheKeys:
    @pytest.fixture(autouse=True)
    def _no_win32(self, monkeypatch):
        self.fronted = []
        monkeypatch.setattr(call_hotkeys, "_bring_window_to_front", self.fronted.append)
        self.queued = []
        monkeypatch.setattr(call_hotkeys.wx, "CallAfter",
                            lambda fn, *a: self.queued.append((fn, a)))

    def test_answer_accepts_the_ringing_call(self):
        window = _Window()
        window.ring("a")
        window._answer_call_from_hotkey()
        assert window.accepted == [("a", None)]

    def test_a_video_call_is_answered_like_its_answer_button(self):
        """None = the camera starts, exactly what the Answer button does."""
        window = _Window()
        window.ring("a", is_video=True)
        window._answer_call_from_hotkey()
        assert window.accepted == [("a", None)]

    def test_answer_then_brings_the_call_window_forward(self):
        window = _Window()
        window.ring("a")
        window._answer_call_from_hotkey()
        assert [fn.__name__ for fn, _a in self.queued] == ["_focus_call_window"]
        window._focus_call_window()
        assert self.fronted == [0x1234]
        assert window.voice_call_window_mute_button.focused

    def test_a_hidden_call_window_is_left_alone(self):
        window = _Window()
        window.voice_call_window.shown = False
        window._focus_call_window()
        assert self.fronted == []

    def test_answer_skips_a_group_offer(self):
        window = _Window()
        window.ring("g", is_group=True)
        window.ring("a")
        window._answer_call_from_hotkey()
        assert window.accepted == [("a", None)]

    def test_answer_after_the_call_ended_does_nothing(self):
        window = _Window()
        window._answer_call_from_hotkey()
        assert window.accepted == [] and self.queued == []

    def test_end_rejects_a_ringing_call_and_says_so(self):
        window = _Window()
        window.ring("a")
        window._end_call_from_hotkey()
        assert window.rejected == ["a"] and window.ended == []
        assert window.spoken == [("<incoming_call_rejected>", True)]

    def test_end_with_nothing_ringing_hangs_up_the_call_in_progress(self):
        window = _Window()
        window._active_voice_call = {"call_id": "c"}
        window._end_call_from_hotkey()
        assert window.ended == [True] and window.rejected == []

    def test_a_ringing_call_is_decided_before_the_one_in_progress(self):
        window = _Window()
        window._active_voice_call = {"call_id": "c"}
        window.ring("a")
        window._end_call_from_hotkey()
        assert window.rejected == ["a"] and window.ended == []

    def test_end_with_no_call_at_all_does_nothing(self):
        window = _Window()
        window._end_call_from_hotkey()
        assert window.rejected == [] and window.ended == [] and window.spoken == []


def test_every_change_of_the_calls_syncs_the_keys():
    """The funnels every change of the ringing set / the call in progress passes through."""
    assert "_sync_call_hotkeys" in inspect.getsource(CallsMixin._sync_incoming_call_bar)
    assert "_sync_call_hotkeys" in inspect.getsource(CallsMixin._sync_voice_call_bar)
    assert "_sync_call_hotkeys" in inspect.getsource(CallsMixin._stop_voice_call_audio)


def test_the_bar_sync_still_works_on_a_window_without_the_mixin():
    class _Bare(CallsMixin):
        _active_incoming_calls = {}

        def Layout(self):
            pass

    _Bare()._sync_incoming_call_bar()
