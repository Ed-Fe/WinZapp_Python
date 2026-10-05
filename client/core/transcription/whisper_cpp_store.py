"""Where the whisper.cpp GGML files live, kept by model_store's own code.

A GgmlFile (whisper_cpp_catalog) answers the names model_store reads, so the
download, the resume, the digest, the `.part`-then-rename publish, the
free-space gate, the cross-process lock and the remove-only-what-you-named
rule are model_store's, unchanged — there is no second downloader to drift.
Call it directly with an entry:

* `model_store.download_model(entry, root, ...)` and `repair_model(...)`;
* `model_store.verify_model(root, entry, ...)`, which hashes the whole file —
  every GGML file has a digest, not only a model.bin;
* `model_store.installation_state(root, entry)` and
  `model_store.remaining_download_bytes(root, entry)`.

What lives here is only what differs: ids are looked up in the whisper.cpp
catalogue rather than faster-whisper's, and a model is a *file*, so the ready
answer is the path of that file, which is what `whisper-cli -m` takes.

**The root is a folder of its own for now.** `default_models_dir()` is a
sibling of the faster-whisper models, not inside them, because two things
there only know the faster-whisper catalogue: `model_store.list_unknown_dirs()`
would list every GGML folder as a leftover of a retired model, and
`move_models()` would leave them behind when the user moves the folder. Every
function takes the root as an argument, so putting both under the user's
chosen folder is a decision part 9b can make — together with teaching those
two about this catalogue.
"""

from __future__ import annotations

import logging
import os

from app_paths import global_dir
from core.transcription import errors, model_store, whisper_cpp_catalog

# Subdirectory of the global data dir holding every GGML file, one folder each.
MODELS_DIRNAME = "whisper_cpp_models"


def default_models_dir() -> str:
    """Where the GGML files live unless told otherwise. Global, like the models.

    Global rather than per account for model_store's reason: large-v3 is 3 GB,
    and one account per process would otherwise download it once per account.
    """
    return global_dir(MODELS_DIRNAME)


def model_path(root, model) -> str:
    """The path of `model`'s one file under `root` — what `-m` is handed."""
    return os.path.join(model_store.model_dir(root, model.id), model.filename)


def list_installed(root) -> tuple[str, ...]:
    """Ids of the complete model files under `root`, in the catalogue's order.

    The voice-activity model is not a transcription model and is not listed;
    `vad_model_path()` is its own question.
    """
    return tuple(
        model.id
        for model in whisper_cpp_catalog.list_models()
        if model_store.is_installed(root, model)
    )


def ensure_ready(root, model_id) -> str:
    """The file to hand whisper-cli for `model_id`, or the right error code.

    model_store.ensure_ready()'s answer for this catalogue: the cheap name and
    exact-size check, MODEL_NOT_INSTALLED or MODEL_CORRUPTED, never a hash.
    """
    model = whisper_cpp_catalog.get_model(model_id)
    if model is None or model not in whisper_cpp_catalog.MODELS:
        # A settings value naming a file this version dropped: "not installed"
        # is true, and the one thing the UI can act on. The voice-activity
        # model is in the catalogue too, and is no transcription model: handed
        # to `-m`, it would fail inside whisper-cli as a "corrupted" model.
        raise errors.TranscriptionError(errors.MODEL_NOT_INSTALLED, str(model_id))
    model_store.ensure_model_ready(root, model)
    return model_path(root, model)


def vad_model_path(root):
    """The installed voice-activity model's file under `root`, or None.

    None is an ordinary answer, not an error: the backend then transcribes
    without the filter and says so (TranscriptionResult.vad_used), exactly as
    faster-whisper does when its filter cannot load.
    """
    model = whisper_cpp_catalog.VAD_MODEL
    if not model_store.is_installed(root, model):
        return None
    return model_path(root, model)


def remove_model(root, model_id, should_cancel=None) -> bool:
    """Delete one GGML file (and its `.part`) from `root`. True if any went.

    Looked up in this catalogue, then model_store.remove_model_files(): the
    same lock, the same rmdir that refuses a folder holding anything else,
    and MODELS_BUSY when another window has the folder.
    """
    model = whisper_cpp_catalog.get_model(model_id)
    if model is None:
        logging.info("[transcription] not removing unknown GGML id %s", model_id)
        return False
    return model_store.remove_model_files(root, model, should_cancel)
