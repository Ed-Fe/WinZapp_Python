"""Everything that can go wrong in a transcription, as one closed set of codes.

Transcription fails in a lot of ways that look identical from the outside —
nothing appears — and the user is listening rather than reading a traceback.
So a failure travels as a *code*, and only the UI layer turns it into a
sentence, in the user's own language.

The split between `code` and `detail` is the point of this module.  `detail` is
technical (a CTranslate2 message, an ffmpeg exit code, a path) and belongs in
log.log; it is never spoken and never shown. Putting a backend string on screen
would say nothing useful to a blind user in Polish, and it is also where file
paths and message ids leak.
"""

from __future__ import annotations

# The chosen model is not on disk yet.
MODEL_NOT_INSTALLED = "model_not_installed"
# It is on disk, but a file is missing or the wrong size — an interrupted
# download that was never cleaned up. The promise is checkable because the
# catalogue pins a revision and, with it, an exact size for every file and a
# sha256 for model.bin (see model_catalog).
MODEL_CORRUPTED = "model_corrupted"
NO_DISK_SPACE = "no_disk_space"
CUDA_UNAVAILABLE = "cuda_unavailable"
INSUFFICIENT_VRAM = "insufficient_vram"
INSUFFICIENT_RAM = "insufficient_ram"
UNSUPPORTED_AUDIO_FORMAT = "unsupported_audio_format"
FFMPEG_FAILED = "ffmpeg_failed"
# The file is there but truncated: a send that died mid-upload, or a media
# file WinZapp wrote while the app was closed.
AUDIO_INCOMPLETE = "audio_incomplete"
# WhatsApp's own media download for this message has not finished, so there is
# no local file to read at all.
MEDIA_NOT_DOWNLOADED = "media_not_downloaded"
CANCELLED = "cancelled"
SAVE_FAILED = "save_failed"
# faster-whisper/CTranslate2 is not installed in this copy of WinZapp.
BACKEND_MISSING = "backend_missing"
MODEL_DOWNLOAD_FAILED = "model_download_failed"
# Moving the models folder failed part way. Deliberately not MODEL_CORRUPTED:
# nothing is corrupted — the move copies and verifies before it deletes, so the
# models are still whole in the folder they were in — and "download it again"
# would be 3 GB of advice for a problem whose answer is to pick another folder.
MODEL_MOVE_FAILED = "model_move_failed"
# Another account's process is holding the shared models directory. Its own
# code because the alternatives all say something false: the download did not
# fail, the connection is fine, and nothing is corrupted — another window is
# simply busy with the same folder, and the answer is to wait rather than to
# retry anything.
MODELS_BUSY = "models_busy"
# The cuBLAS wheel could not be fetched. Its own code rather than
# MODEL_DOWNLOAD_FAILED because that sentence names the transcription *model*,
# and a user who was told their model failed to download will go and repair a
# model that is perfectly fine.
CUDA_RUNTIME_DOWNLOAD_FAILED = "cuda_runtime_download_failed"
# The installed CUDA libraries are missing, the wrong size or hash differently
# than the wheel's own RECORD says. Same split as MODEL_CORRUPTED against
# MODEL_DOWNLOAD_FAILED: "the files on your disk are damaged, install them
# again" is a different instruction from "the download did not get through".
CUDA_RUNTIME_CORRUPTED = "cuda_runtime_corrupted"
# Another account's process is holding the CUDA libraries directory. Distinct
# from MODELS_BUSY only in which folder it names, which is the whole point: a
# user who clicked "download the CUDA libraries" and is told another window is
# busy with the *models* has been sent to look at the wrong thing.
CUDA_RUNTIME_BUSY = "cuda_runtime_busy"
# The catch-all: the backend raised something we have no specific answer for.
BACKEND_ERROR = "backend_error"

# Single source of truth for "every code there is" — the i18n map below and the
# test that pins it both derive from this, so a new code cannot be added
# without a translation.
ERROR_CODES = (
    MODEL_NOT_INSTALLED,
    MODEL_CORRUPTED,
    NO_DISK_SPACE,
    CUDA_UNAVAILABLE,
    INSUFFICIENT_VRAM,
    INSUFFICIENT_RAM,
    UNSUPPORTED_AUDIO_FORMAT,
    FFMPEG_FAILED,
    AUDIO_INCOMPLETE,
    MEDIA_NOT_DOWNLOADED,
    CANCELLED,
    SAVE_FAILED,
    BACKEND_MISSING,
    MODEL_DOWNLOAD_FAILED,
    MODEL_MOVE_FAILED,
    MODELS_BUSY,
    CUDA_RUNTIME_DOWNLOAD_FAILED,
    CUDA_RUNTIME_CORRUPTED,
    CUDA_RUNTIME_BUSY,
    BACKEND_ERROR,
)

ERROR_I18N_KEYS = {code: f"transcription_error_{code}" for code in ERROR_CODES}


class TranscriptionError(Exception):
    """A transcription failure carrying a code the UI can translate.

    `detail` is for the log only, and neither `str(exc)` nor `exc.args` carries
    it. That is not tidiness: `wx.MessageBox(str(exc), ...)` is an idiom this
    repository already uses (`client/ui/media_viewer.py`), so anything `__str__`
    returns is one careless handler away from being read out, character by
    character, to a blind user — including the file path a backend error message
    usually contains. Logging wants the pair, so logging asks for `log_line`.
    """

    #: Ids a partially completed `move_models()` did manage to move before it
    #: failed. Declared here, empty, rather than only where that path attaches
    #: it: a caller writing `except TranscriptionError as exc: ... exc.moved`
    #: around a move would otherwise hit AttributeError on every *other* way a
    #: move can fail — the busy-folder path raises its own error and never
    #: passes through the attach.
    moved: tuple = ()

    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail

    def __str__(self):
        return str(self.code)

    @property
    def log_line(self) -> str:
        """Code and technical detail on one line — for `logging.*`, only."""
        return f"{self.code}: {self.detail}" if self.detail else str(self.code)

    @property
    def i18n_key(self) -> str:
        return error_i18n_key(self.code)


def error_i18n_key(code) -> str:
    """The i18n key for an error code.

    An unrecognised code resolves to the internal-error message rather than to
    a key of its own: I18n.t() falls back to the raw key name, so inventing
    "transcription_error_<whatever>" here would have a screen reader read the
    code out loud instead of a sentence.
    """
    return ERROR_I18N_KEYS.get(code, ERROR_I18N_KEYS[BACKEND_ERROR])
