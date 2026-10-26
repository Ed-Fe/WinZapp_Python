"""Repairs a pairing that WhatsApp refused after the QR was scanned.

whatsmeow only writes the refusal in its log. Two lines matter:

- `Failed to pair device: invalid device identity HMAC`: the QR on screen no
  longer matches the keys the client holds (the server rotated the ADV key, or
  a pairing code was asked for in the same connection). Scanning it can never
  work; the connection has to start again with new keys to get a QR that does.
- `Failed to handle code pair notification`: the same for a pairing code.

TeleZapp lives with the same whatsmeow and fixed it by listening to that log,
wiping the session keys and starting over, at most 3 times (its CHANGELOG,
2026.10.21). Here the QR failure restarts the pairing and the code failure only
tells the client: a new code would be minted for a number nobody asked about,
and the per-number quota outlives the process (docs/traps/pairing-flow.md).
Pure functions and a log handler; the restart itself is `Session`'s.
"""
import logging
import os

MAX_REPAIRS = 3
QR, CODE = "qr", "code"

_FILES = ("session.db", "session.db-wal", "session.db-shm", "session.db-journal")


def failure_kind(text):
    """QR or CODE when the whatsmeow log line says pairing was refused, else None."""
    if "Failed to handle code pair notification" in text:
        return CODE
    if "Failed to pair device" in text:
        return QR
    return None


class Budget:
    """How many times in a row a pairing may be restarted. A connected session
    gives it back: the limit is per pairing attempt, not per process."""

    def __init__(self, limit=MAX_REPAIRS):
        self.limit = limit
        self.used = 0

    def take(self) -> bool:
        if self.used >= self.limit:
            return False
        self.used += 1
        return True

    def reset(self):
        self.used = 0


class FailureHandler(logging.Handler):
    """Calls `on_failure(kind)` for each refusal that whatsmeow logs."""

    def __init__(self, on_failure):
        super().__init__(logging.ERROR)
        self.on_failure = on_failure

    def emit(self, record):
        try:
            kind = failure_kind(record.getMessage())
            if kind:
                self.on_failure(kind)
        except Exception:
            self.handleError(record)


def wipe_session_keys(folder) -> bool:
    """Deletes the WhatsApp keys of a session (never the message store). True
    when none of the files is left."""
    for name in _FILES:
        path = os.path.join(folder, name)
        try:
            if os.path.lexists(path):
                os.remove(path)
        except OSError:
            return False
    return not any(os.path.lexists(os.path.join(folder, n)) for n in _FILES)
