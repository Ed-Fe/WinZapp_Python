"""The Whisper models WinZapp offers, as data.

Models are not shipped inside the installer — a single one of them is bigger
than the whole app — so they are downloaded from Hugging Face on demand. That
makes the catalogue a contract with a remote repository: the revisions, file
names, file sizes and the model.bin digest below were read from the Hugging
Face API on 2026-09-03 and are what the download and the "this model is
complete" check are measured against.

Three details of that contract are easy to get wrong and expensive afterwards:

* **The file list is not uniform.** The Systran tiny..medium conversions ship
  ``vocabulary.txt`` and no ``preprocessor_config.json``, while large-v3 and the
  turbo conversion ship ``vocabulary.json`` *and* ``preprocessor_config.json``.
  Assuming one shape leaves a "complete" model that CTranslate2 refuses to load,
  which is indistinguishable to the user from a corrupted download.
* **Every model is pinned to a revision.** Downloads go to
  ``.../resolve/<revision>/...`` rather than ``/main/``, so a new revision
  published upstream cannot invalidate the byte counts here or swap the weights
  under an install that already verified them. Because the revision is pinned,
  every size below is a constant, not an estimate — which is what lets
  MODEL_CORRUPTED mean "missing or the wrong size" for *every* file rather than
  only for model.bin.
* **The totals are sums, never guesses.** They used to add an approximated
  couple of megabytes for the auxiliary files, and on the turbo conversion that
  guess was 181 KB *under* the real figure — an error in the wrong direction for
  a number that feeds a "does this fit?" gate.

No user-facing text lives here, only ids and i18n keys: the size classes are
what the model picker labels "fast / balanced / accurate", and translating them
is the UI layer's job.
"""

from __future__ import annotations

from dataclasses import dataclass

# Size classes, in the order the picker presents them. These drive the
# "rápido / equilibrado / preciso" labelling, so they describe what the user
# gets, not the byte count — large-v3-turbo is smaller than large-v3 and still
# belongs with it, because its output is a large model's output.
SIZE_SMALL = "small"
SIZE_MEDIUM = "medium"
SIZE_LARGE = "large"

SIZE_CLASSES = (SIZE_SMALL, SIZE_MEDIUM, SIZE_LARGE)

SIZE_CLASS_I18N_KEYS = {
    SIZE_SMALL: "transcription_size_fast",
    SIZE_MEDIUM: "transcription_size_balanced",
    SIZE_LARGE: "transcription_size_accurate",
}


@dataclass(frozen=True)
class WhisperModel:
    """One downloadable CTranslate2 conversion of a Whisper model."""

    id: str
    repo: str
    # The commit the files were measured at. Everything is fetched by this sha,
    # never by branch — see the module docstring.
    revision: str
    # model.bin's digest at that revision, so an integrity check can go past
    # "the right number of bytes" for the one file where a silent corruption
    # costs a multi-gigabyte re-download to discover.
    model_bin_sha256: str
    # (name, exact bytes) for every file the model needs, in the order the
    # repository lists them. A tuple of pairs rather than a dict because the
    # catalogue is shared module state a caller must not be able to edit in
    # place; `dict(model.files)` is the lookup form.
    files: tuple[tuple[str, int], ...]
    # Pulled out of `files` for the callers that only care about the weights.
    model_bin_bytes: int
    # What the download costs, and what it leaves behind — both the exact sum of
    # `files`. They are equal today because the files are written straight into
    # the model folder (nothing is extracted, no separate blob cache is kept),
    # but the picker quotes both to the user, and a future cache layout that
    # duplicates blobs would move only one of them.
    download_bytes: int
    disk_bytes: int
    size_class: str
    # Recommended minimums, not hard requirements: roughly twice the weights,
    # which covers activations, the KV cache at beam size 5 and the CUDA
    # context. Undershooting them does not fail cleanly — CTranslate2 aborts
    # mid-transcription with an allocation error — so auto_select_model() keeps
    # its distance from them (see device.py).
    min_vram_mb: int  # float16 on the GPU
    min_ram_mb: int  # int8 on the CPU


def _model(model_id, repo, revision, model_bin_sha256, files, size_class,
           min_vram_mb, min_ram_mb) -> WhisperModel:
    """One catalogue entry, with everything derivable derived from `files`.

    The totals and the model.bin size are read off the file table instead of
    being restated beside it: six models times three restatements is eighteen
    chances for two numbers describing the same download to disagree.

    Every file name has to be a bare basename, and that is asserted rather than
    assumed: the model store joins these names onto a directory the user may
    point anywhere and deletes exactly them, so a nested layout
    ("snapshots/<sha>/model.bin", which is the shape these repositories use
    internally) or a ".." would have a delete reach outside the models root.
    """
    for name, _size in files:
        # Both separators, not just this platform's: the catalogue is written
        # once and read on Windows, where a name carrying either one escapes
        # the model's own directory.
        assert name and name not in (".", "..") and not set(name) & {"/", "\\"}, (
            f"{model_id}: file names must be bare basenames, got {name!r}"
        )
    total = sum(size for _name, size in files)
    return WhisperModel(
        id=model_id,
        repo=repo,
        revision=revision,
        model_bin_sha256=model_bin_sha256,
        files=files,
        model_bin_bytes=dict(files)["model.bin"],
        download_bytes=total,
        disk_bytes=total,
        size_class=size_class,
        min_vram_mb=min_vram_mb,
        min_ram_mb=min_ram_mb,
    )


# Single source of truth. Declared in the order the repositories were verified;
# list_models() is what defines presentation order.
MODELS = (
    _model(
        "tiny",
        "Systran/faster-whisper-tiny",
        "d90ca5fe260221311c53c58e660288d3deb8d356",
        "dcb76c6586fc06cbdac6dd21f14cfd129cc4cdd9dce19bf4ffa62e59cbe6e6d1",
        (
            ("config.json", 2_249),
            ("model.bin", 75_538_270),
            ("tokenizer.json", 2_203_239),
            ("vocabulary.txt", 459_861),
        ),
        SIZE_SMALL,
        min_vram_mb=1024,
        min_ram_mb=2048,
    ),
    _model(
        "base",
        "Systran/faster-whisper-base",
        "ebe41f70d5b6dfa9166e2c581c45c9c0cfc57b66",
        "d01c3014881c9c6f3133c182f3d2887eb6ca1c789a7538c5c007196857a0a6a9",
        (
            ("config.json", 2_309),
            ("model.bin", 145_217_532),
            ("tokenizer.json", 2_203_239),
            ("vocabulary.txt", 459_861),
        ),
        SIZE_SMALL,
        min_vram_mb=1024,
        min_ram_mb=2048,
    ),
    _model(
        "small",
        "Systran/faster-whisper-small",
        "536b0662742c02347bc0e980a01041f333bce120",
        "3e305921506d8872816023e4c273e75d2419fb89b24da97b4fe7bce14170d671",
        (
            ("config.json", 2_370),
            ("model.bin", 483_546_902),
            ("tokenizer.json", 2_203_239),
            ("vocabulary.txt", 459_861),
        ),
        SIZE_MEDIUM,
        min_vram_mb=2048,
        min_ram_mb=3072,
    ),
    _model(
        "medium",
        "Systran/faster-whisper-medium",
        "08e178d48790749d25932bbc082711ddcfdfbc4f",
        "9b45e1009dcc4ab601eff815b61d80e60ce3fd8c74c1a14f4a282258286b51ae",
        (
            ("config.json", 2_257),
            ("model.bin", 1_527_906_378),
            ("tokenizer.json", 2_203_239),
            ("vocabulary.txt", 459_861),
        ),
        SIZE_MEDIUM,
        min_vram_mb=4096,
        min_ram_mb=6144,
    ),
    _model(
        "large-v3",
        "Systran/faster-whisper-large-v3",
        "edaa852ec7e145841d8ffdb056a99866b5f0a478",
        "69f74147e3334731bc3a76048724833325d2ec74642fb52620eda87352e3d4f1",
        (
            ("config.json", 2_394),
            ("model.bin", 3_087_284_237),
            ("preprocessor_config.json", 340),
            ("tokenizer.json", 2_480_617),
            ("vocabulary.json", 1_068_114),
        ),
        SIZE_LARGE,
        min_vram_mb=6144,
        min_ram_mb=8192,
    ),
    _model(
        "large-v3-turbo",
        "deepdml/faster-whisper-large-v3-turbo-ct2",
        "4df90f75321148c3a29a9e2351b7ddf8f5b115a8",
        "e76620f83d5f5b69efd3d87e3dc180c1bd21df9fbebacfd4335e5e1efcc018da",
        (
            ("config.json", 2_263),
            ("model.bin", 1_617_884_929),
            ("preprocessor_config.json", 340),
            ("tokenizer.json", 2_710_337),
            ("vocabulary.json", 1_068_114),
        ),
        SIZE_LARGE,
        min_vram_mb=4096,
        min_ram_mb=6144,
    ),
)


def _order(model: WhisperModel) -> tuple[int, int]:
    return (SIZE_CLASSES.index(model.size_class), model.download_bytes)


def list_models() -> tuple[WhisperModel, ...]:
    """Every model, cheapest first.

    Ordered by size class and then by download size, so the two large models
    are listed turbo-first: it is the one to reach for when large-v3 is too
    much, and putting it after would bury it under the 3 GB entry.
    """
    return tuple(sorted(MODELS, key=_order))


def get_model(model_id) -> WhisperModel | None:
    """The model with this id, or None.

    Deliberately not raising: the id comes from settings, where a model that
    was renamed or removed between versions is a normal state, not a fault.
    """
    for model in MODELS:
        if model.id == model_id:
            return model
    return None


def total_bytes(model_id) -> int | None:
    """Bytes a fresh install of `model_id` costs, or None for an unknown id.

    None rather than 0, even though 0 would spare the caller a check. The
    caller that matters here is the free-space gate, and an unknown id is
    exactly what a settings file naming a model some later version dropped
    looks like: answering 0 tells that gate "it fits" and starts a download of
    something that does not exist.
    """
    model = get_model(model_id)
    return model.download_bytes if model else None


def size_class_i18n_key(size_class) -> str | None:
    """The i18n key labelling a size class, or None for an unknown one."""
    return SIZE_CLASS_I18N_KEYS.get(size_class)
