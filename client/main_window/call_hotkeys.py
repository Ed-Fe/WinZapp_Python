"""CallHotkeysMixin — part of MainWindow (see main_window/__init__.py).

Answer, reject or hang up a call from any window. Two system-wide keys,
changeable in Settings > Keyboard shortcuts (shipped as Ctrl+Alt+Shift+Enter
and Ctrl+Alt+Shift+Backspace):

* answer: answers the call that is ringing, brings the call window forward and
  puts the focus in it. A video call is answered the way its Answer button
  does, with the camera on.
* end: rejects the call that is ringing; with no call ringing, hangs up the one
  in progress.

WinZapp used to need its own window in front (or the global "show WinZapp" key
first) before a call could be answered, which for a screen-reader user means
leaving what they are doing.

The keys are registered with Windows only while they can do something --
``answer`` while a call that can be answered rings, ``end`` while one rings or
a call is in progress -- so they are free for every other program the rest of
the time. ``_sync_call_hotkeys`` runs from every change of the incoming-call
state and of the call in progress.
"""

import logging

import wx

from core.call_logic import (
    call_hotkey_setting,
    first_answerable_call,
)
from main_window.win32_helpers import _HotkeyManager, _bring_window_to_front


class CallHotkeysMixin:
    def _wanted_call_hotkeys(self) -> dict:
        """name -> ((vk, mod), callback) for the keys that can act right now."""
        ringing = self._has_answerable_incoming_call()
        in_call = bool(getattr(self, "_active_voice_call", None))
        general = getattr(self, "settings", {}).get("general", {})
        wanted = {}
        if ringing:
            key = call_hotkey_setting(general, "call_answer_hotkey")
            if key:
                wanted["answer"] = (key, self._answer_call_from_hotkey)
        if ringing or in_call:
            key = call_hotkey_setting(general, "call_end_hotkey")
            if key:
                wanted["end"] = (key, self._end_call_from_hotkey)
        return wanted

    def _sync_call_hotkeys(self):
        """Hold exactly the keys that can act now, on the keys set in Settings."""
        managers = getattr(self, "_call_hotkey_managers", None)
        if managers is None:
            managers = self._call_hotkey_managers = {}
        wanted = self._wanted_call_hotkeys()
        for name in list(managers):
            manager, key = managers[name]
            if name not in wanted or wanted[name][0] != key:
                manager.stop(wait=True)
                del managers[name]
        for name, (key, callback) in wanted.items():
            if name in managers:
                continue
            try:
                managers[name] = (_HotkeyManager(key[0], key[1], callback), key)
            except Exception:
                logging.exception("[call_hotkeys] could not register the %s key", name)

    def _ringing_call_for_hotkey(self):
        identity = first_answerable_call(
            getattr(self, "_active_incoming_calls", {}),
            getattr(self, "_incoming_call_details", {}),
        )
        details = getattr(self, "_incoming_call_details", {}).get(identity, {})
        return identity, details

    def _answer_call_from_hotkey(self):
        identity, details = self._ringing_call_for_hotkey()
        if not identity:
            return
        logging.info("[call_hotkeys] answer key pressed video=%s", bool(details.get("is_video")))
        self.accept_incoming_call(identity)
        # After accept_incoming_call's own queued refresh of the call window,
        # so the window is on screen by the time it is brought forward.
        wx.CallAfter(self._focus_call_window)

    def _end_call_from_hotkey(self):
        identity, _details = self._ringing_call_for_hotkey()
        if identity:
            logging.info("[call_hotkeys] end key pressed: rejecting the ringing call")
            self.reject_incoming_call(identity)
            self.output(self.i18n.t("incoming_call_rejected"), interrupt=True)
            return
        if getattr(self, "_active_voice_call", None):
            logging.info("[call_hotkeys] end key pressed: hanging up the call")
            self.end_active_call()

    def _focus_call_window(self):
        """Bring the call window in front of any program and focus its first button."""
        window = getattr(self, "voice_call_window", None)
        if window is None or not window.IsShown():
            return
        try:
            _bring_window_to_front(window.GetHandle())
        except Exception:
            logging.exception("[call_hotkeys] could not bring the call window forward")
            window.Raise()
        button = getattr(self, "voice_call_window_mute_button", None)
        if button is not None:
            button.SetFocus()
