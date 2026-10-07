"""Plaintext a crashed run left in %TEMP%, removed at the next start.

A transcription writes the decrypted voice note, its converted WAV and
whisper.cpp's output into the system temp folder under random names, and
deletes them when it ends. A process killed in the middle (a crash, a power
cut, Task Manager) never gets to, and %TEMP% is not a private place. The hour
of grace is what keeps this from touching a run another WinZapp account on
the same machine is in the middle of.
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
import time

#: What message_audio, audio_prep and whisper_cpp_backend name their temporaries.
PREFIXES = ("winzapp-audio-", "winzapp-transcribe-", "winzapp-whisper-")

MAX_AGE_SECONDS = 3600


def sweep_stale_temporaries(temp_dir=None, now=None, max_age_seconds=MAX_AGE_SECONDS) -> int:
    """Delete this package's temporaries older than `max_age_seconds`.

    Returns how many entries went. Never raises, and logs counts only: a
    temporary's name is random, but nothing else about it is worth saying.
    """
    removed = 0
    try:
        root = temp_dir or tempfile.gettempdir()
        now = time.time() if now is None else now
        names = os.listdir(root)
    except OSError:
        return 0
    for name in names:
        if not name.startswith(PREFIXES):
            continue
        path = os.path.join(root, name)
        try:
            if now - os.path.getmtime(path) < max_age_seconds:
                continue
            if os.path.isdir(path) and not os.path.islink(path):
                shutil.rmtree(path)
            else:
                os.remove(path)
            removed += 1
        except OSError as exc:
            logging.warning(
                "[transcription] could not remove a stale temporary (%s)",
                type(exc).__name__,
            )
    if removed:
        logging.info("[transcription] removed %d stale temporary file(s)", removed)
    return removed
