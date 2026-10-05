"""The small helpers every store of this package repeated, defined once.

model_store, cuda_runtime, whisper_cpp_runtime and whisper_cpp_backend each
carried their own copy of these four. They import them under their old private
names, so nothing about how they read changed — only that a fix to one (say,
the cancellation wording) can no longer reach three of the four.
"""

from __future__ import annotations

import os

from core.transcription import errors


def check_cancel(should_cancel) -> None:
    """Raise CANCELLED if the user asked to stop."""
    if should_cancel is not None and should_cancel():
        raise errors.TranscriptionError(errors.CANCELLED, "cancelled by the user")


def report(progress, done, total) -> None:
    if progress is not None:
        progress(done, total)


def unlink(path) -> bool:
    """Remove a file; True if it went, False if it was not there or would not."""
    try:
        os.remove(path)
        return True
    except OSError:
        return False


def remove_empty_dir(directory) -> None:
    """rmdir, which is a no-op on any directory that still holds a file."""
    try:
        os.rmdir(directory)
    except OSError:
        pass
