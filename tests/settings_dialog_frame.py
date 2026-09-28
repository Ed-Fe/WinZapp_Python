"""The install-wide half of MainWindow that a SettingsDialog test frame needs.

The dialog refreshes the install-wide keys from the shared file before it
loads them (MainWindow.refresh_global_settings()) and hands the ones the user
changed to MainWindow.choose_global_settings() on OK. A test frame built from
loose attributes has neither, so it gets the real methods bound onto it --
never a lambda standing in for them, which is how a wrong attribute name once
hid a bug here twice. With no `_app_settings` on the frame, the reconciliation
returns early as it does on a legacy install, and a choice lands in
frame.settings, which is what the round-trip tests read back.
"""

import threading
import types

from main import MainWindow

_METHODS = ("choose_global_settings", "refresh_global_settings", "_persist_global_settings")


def give_global_settings(frame):
    frame._save_lock = threading.Lock()
    for name in _METHODS:
        setattr(frame, name, types.MethodType(getattr(MainWindow, name), frame))
    return frame
