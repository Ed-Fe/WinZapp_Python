"""
Startup exits that used to be silent (client/startup_exit_notice.py)
====================================================================

A few checks in main.py end the process before any account is bound, and so
before setup_logging(): an update still being installed, and this account
already held by another process. Each used to be a bare ``sys.exit(0)``. When
the process they deferred to was stuck, WinZapp "did not open" with nothing on
screen and nothing in any log (reported 2026-10-18 after a multi-account
update: both accounts' log.log ended at the update, hours before the report).

Each exit now leaves a line in the global bootstrap.log, and, unless the
launch is a background one, tells the user why in a native message box —
there is no window yet to speak through.
"""

from __future__ import annotations

import os
import sys
import time

#: An update installer owns the program files (update_state.json).
UPDATE_IN_PROGRESS = "update_in_progress"
#: Another process holds this account and acknowledged the activation.
ACTIVATED = "activated"
#: Another process holds this account but did not answer the activation.
ACCOUNT_NOT_ANSWERING = "account_not_answering"
#: Another process holds this account and the launch was a background one.
ALREADY_RUNNING_BACKGROUND = "already_running_background"

_MESSAGE_KEYS = {
    UPDATE_IN_PROGRESS: "startup_update_in_progress",
    ACCOUNT_NOT_ANSWERING: "startup_account_not_answering",
}

_MB_ICONINFORMATION = 0x40


def message_key(reason: str, background: bool):
    """The i18n key the user is shown for *reason*, or None for no message:
    a background launch never shows one, and neither does a successful
    hand-over to the running process, which is already coming forward."""
    if background:
        return None
    return _MESSAGE_KEYS.get(reason)


def log_line(reason: str, account_id: str = "", pid: int = 0, now: float = None) -> str:
    """The bootstrap.log line for one startup exit."""
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() if now is None else now))
    account = f" account={account_id}" if account_id else ""
    return f"{stamp} [pid={pid or os.getpid()}] startup exit: {reason}{account}\n"


def record(reason: str, account_id: str = "", path: str = "") -> None:
    """Append the exit to the global bootstrap.log. Never raises."""
    try:
        if not path:
            from app_paths import bootstrap_log_path
            path = bootstrap_log_path()
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(log_line(reason, account_id))
    except Exception:
        pass


def leave_startup(reason: str, *, background: bool, account_id: str = "",
                  translate=None, show=None) -> None:
    """Record why this launch stops here, tell the user when there is
    something to tell, and exit with code 0 (a launch that defers to another
    process is not a failure; account_launcher relies on that)."""
    record(reason, account_id)
    key = message_key(reason, background)
    if key:
        try:
            if translate is None:
                from startup_i18n import startup_i18n
                translate = startup_i18n().t
            if show is None:
                import ctypes
                show = lambda text: ctypes.windll.user32.MessageBoxW(
                    0, text, "WinZapp", _MB_ICONINFORMATION)
            show(translate(key))
        except Exception:
            pass
    sys.exit(0)
