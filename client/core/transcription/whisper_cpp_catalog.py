"""The GGML files whisper.cpp can transcribe with, as data.

model_catalog's counterpart for the second backend, and the same contract with
a remote repository: every file below was listed from the Hugging Face API of
`ggerganov/whisper.cpp` at revision 5359861c739e955e79d9a303bcbc70fb988958b1
(and the voice-activity model from `ggml-org/whisper-vad` at
9ffd54a1e1ee413ddf265af9913beaf518d1639b) on 2026-10-05, twice, with identical
results. The sizes and digests are what model_store downloads and verifies
against, through the same code as the faster-whisper models.

What differs from the faster-whisper catalogue, and why:

* **A model is one file, and a quantization is a different file.** There is no
  folder of tokenizer and config files: `ggml-small-q5_1.bin` is the whole
  model. So choosing a quantization (part 11) is choosing which entry, and an
  entry knows its `base_model` and its `quantization` for exactly that purpose
  — `variant()` and `variants_of()` are the lookups. Only the variants the
  repository actually publishes are here; they are not uniform (tiny..small
  have q5_1, medium and the large ones q5_0, large-v1 nothing but f16, large-v3
  no q8_0), and a variant that is not published is not a variant.

* **Every file carries its own sha256.** Hugging Face states one for every LFS
  file, and each of these is one, so unlike model_catalog — where only
  model.bin has one — nothing here is checked by size alone.

* **The English-only models (`*.en`) are left out, on purpose.** The
  faster-whisper catalogue offers none, and nothing around it could use one
  sensibly: a request's language defaults to "detect it" because WhatsApp
  carries no language for a voice note (see backend.TranscriptionRequest), and
  an `.en` model handed a note in Portuguese does not fail — it answers with
  confident English, which a listener cannot tell from a transcription. Offering
  them would need a "this model only hears English" rule in the picker and in
  the run; until a user asks for that trade, the multilingual models cover the
  same sizes.

* **The CoreML `*-encoder.mlmodelc.zip` files are macOS-only** and not listed.

The ids are the file names without `.bin` ("ggml-small-q5_1"): they double as
the folder name under the models root (model_store.model_dir()), and the
`ggml-` prefix keeps them from ever colliding with a faster-whisper id should
both catalogues one day share a root.

No user-facing text lives here: the size classes reuse model_catalog's, whose
i18n keys already exist, and naming a quantization is the UI layer's job.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from core.transcription.model_catalog import (
    SIZE_CLASSES,
    SIZE_LARGE,
    SIZE_MEDIUM,
    SIZE_SMALL,
)

MODELS_REPO = "ggerganov/whisper.cpp"
MODELS_REVISION = "5359861c739e955e79d9a303bcbc70fb988958b1"

VAD_REPO = "ggml-org/whisper-vad"
VAD_REVISION = "9ffd54a1e1ee413ddf265af9913beaf518d1639b"

# Quantizations, most faithful first. f16 is what the repository calls plain
# `ggml-<model>.bin`: the converter writes half precision unless asked to
# quantize. Ordered so part 11 can offer "the best that fits" by walking it.
QUANT_F16 = "f16"
QUANT_Q8_0 = "q8_0"
QUANT_Q5_1 = "q5_1"
QUANT_Q5_0 = "q5_0"

QUANTIZATIONS = (QUANT_F16, QUANT_Q8_0, QUANT_Q5_1, QUANT_Q5_0)

# The Whisper models in the order the picker presents them, each with the
# size class model_catalog gives the same model — the shared ids are the same
# model, and "fast / balanced / accurate" must not change with the backend.
# large-v1 and large-v2 exist only here; they are large models in every sense.
_BASE_MODELS = (
    ("tiny", SIZE_SMALL),
    ("base", SIZE_SMALL),
    ("small", SIZE_MEDIUM),
    ("medium", SIZE_MEDIUM),
    ("large-v3-turbo", SIZE_LARGE),
    ("large-v1", SIZE_LARGE),
    ("large-v2", SIZE_LARGE),
    ("large-v3", SIZE_LARGE),
)

BASE_MODELS = tuple(name for name, _size_class in _BASE_MODELS)

_FILE_PREFIX = "ggml-"
_FILE_SUFFIX = ".bin"
_QUANT_SUFFIX = re.compile(r"^(?P<base>.+)-(?P<quant>q\d_\d)$")


@dataclass(frozen=True)
class GgmlFile:
    """One downloadable GGML file, shaped so model_store can handle it.

    model_store reads `id`, `repo`, `revision`, `files`, `download_bytes` and
    `sha256_of()`; a WhisperModel answers the same names, which is the whole of
    what lets both catalogues share one downloader, one verifier and one
    remover instead of two copies drifting apart.
    """

    id: str
    repo: str
    # Fetched by this sha, never by branch: see model_store.file_url().
    revision: str
    filename: str
    size_bytes: int
    sha256: str
    # The model_catalog id of the same model where there is one ("small",
    # "large-v3-turbo"); "" for the voice-activity model.
    base_model: str = ""
    # One of QUANTIZATIONS, or "" for the voice-activity model.
    quantization: str = ""
    size_class: str = ""

    @property
    def files(self) -> tuple[tuple[str, int], ...]:
        return ((self.filename, self.size_bytes),)

    @property
    def download_bytes(self) -> int:
        return self.size_bytes

    @property
    def disk_bytes(self) -> int:
        # Written straight into its folder, nothing extracted, nothing cached.
        return self.size_bytes

    def sha256_of(self, name):
        """The digest `name` is checked against: this file's, or None."""
        return self.sha256 if name == self.filename else None


def _ggml(filename, size_bytes, sha256) -> GgmlFile:
    """One model entry, with its base model and quantization read off the name.

    Derived rather than restated beside the name: thirty-odd entries times two
    restatements is sixty chances for a row to say "q5_1" next to a q5_0 file.
    The name is asserted to be a bare basename for the reason model_catalog
    gives — the store joins it onto a user-chosen directory and deletes it.
    """
    assert filename.startswith(_FILE_PREFIX) and filename.endswith(_FILE_SUFFIX), filename
    assert not set(filename) & {"/", "\\"}, filename
    stem = filename[len(_FILE_PREFIX):-len(_FILE_SUFFIX)]
    match = _QUANT_SUFFIX.match(stem)
    base, quant = (match["base"], match["quant"]) if match else (stem, QUANT_F16)
    size_class = dict(_BASE_MODELS)[base]
    assert quant in QUANTIZATIONS, filename
    return GgmlFile(
        id=filename[:-len(_FILE_SUFFIX)],
        repo=MODELS_REPO,
        revision=MODELS_REVISION,
        filename=filename,
        size_bytes=size_bytes,
        sha256=sha256,
        base_model=base,
        quantization=quant,
        size_class=size_class,
    )


# Single source of truth, in the order the repository lists them.
MODELS = (
    _ggml("ggml-tiny.bin", 77_691_713,
          "be07e048e1e599ad46341c8d2a135645097a538221678b7acdd1b1919c6e1b21"),
    _ggml("ggml-tiny-q5_1.bin", 32_152_673,
          "818710568da3ca15689e31a743197b520007872ff9576237bda97bd1b469c3d7"),
    _ggml("ggml-tiny-q8_0.bin", 43_537_433,
          "c2085835d3f50733e2ff6e4b41ae8a2b8d8110461e18821b09a15c40c42d1cca"),
    _ggml("ggml-base.bin", 147_951_465,
          "60ed5bc3dd14eea856493d334349b405782ddcaf0028d4b5df4088345fba2efe"),
    _ggml("ggml-base-q5_1.bin", 59_707_625,
          "422f1ae452ade6f30a004d7e5c6a43195e4433bc370bf23fac9cc591f01a8898"),
    _ggml("ggml-base-q8_0.bin", 81_768_585,
          "c577b9a86e7e048a0b7eada054f4dd79a56bbfa911fbdacf900ac5b567cbb7d9"),
    _ggml("ggml-small.bin", 487_601_967,
          "1be3a9b2063867b937e64e2ec7483364a79917e157fa98c5d94b5c1fffea987b"),
    _ggml("ggml-small-q5_1.bin", 190_085_487,
          "ae85e4a935d7a567bd102fe55afc16bb595bdb618e11b2fc7591bc08120411bb"),
    _ggml("ggml-small-q8_0.bin", 264_464_607,
          "49c8fb02b65e6049d5fa6c04f81f53b867b5ec9540406812c643f177317f779f"),
    _ggml("ggml-medium.bin", 1_533_763_059,
          "6c14d5adee5f86394037b4e4e8b59f1673b6cee10e3cf0b11bbdbee79c156208"),
    _ggml("ggml-medium-q5_0.bin", 539_212_467,
          "19fea4b380c3a618ec4723c3eef2eb785ffba0d0538cf43f8f235e7b3b34220f"),
    _ggml("ggml-medium-q8_0.bin", 823_369_779,
          "42a1ffcbe4167d224232443396968db4d02d4e8e87e213d3ee2e03095dea6502"),
    _ggml("ggml-large-v1.bin", 3_094_623_691,
          "7d99f41a10525d0206bddadd86760181fa920438b6b33237e3118ff6c83bb53d"),
    _ggml("ggml-large-v2.bin", 3_094_623_691,
          "9a423fe4d40c82774b6af34115b8b935f34152246eb19e80e376071d3f999487"),
    _ggml("ggml-large-v2-q5_0.bin", 1_080_732_091,
          "3a214837221e4530dbc1fe8d734f302af393eb30bd0ed046042ebf4baf70f6f2"),
    _ggml("ggml-large-v2-q8_0.bin", 1_656_129_691,
          "fef54e6d898246a65c8285bfa83bd1807e27fadf54d5d4e81754c47634737e8c"),
    _ggml("ggml-large-v3.bin", 3_095_033_483,
          "64d182b440b98d5203c4f9bd541544d84c605196c4f7b845dfa11fb23594d1e2"),
    _ggml("ggml-large-v3-q5_0.bin", 1_081_140_203,
          "d75795ecff3f83b5faa89d1900604ad8c780abd5739fae406de19f23ecd98ad1"),
    _ggml("ggml-large-v3-turbo.bin", 1_624_555_275,
          "1fc70f774d38eb169993ac391eea357ef47c88757ef72ee5943879b7e8e2bc69"),
    _ggml("ggml-large-v3-turbo-q5_0.bin", 574_041_195,
          "394221709cd5ad1f40c46e6031ca61bce88931e6e088c188294c6d5a55ffa7e2"),
    _ggml("ggml-large-v3-turbo-q8_0.bin", 874_188_075,
          "317eb69c11673c9de1e1f0d459b253999804ec71ac4c23c17ecf5fbe24e259a1"),
)

# The voice-activity model whisper-cli's `--vad` loads. Two are published at
# the pinned revision, 885,098 bytes each: silero v5.1.2 and v6.2.0. v6.2.0 is
# Silero's newer release, and nothing measured here prefers v5.1.2: at the
# same size they cost the same, so the newer one wins by default. That it
# loads in release b4938 is unverified (see whisper_cpp_backend: a VAD that
# fails to load costs the filter, never the transcription, and the result
# says so) — and if a real run disagrees, swapping is this one row.
VAD_MODEL = GgmlFile(
    id="ggml-silero-v6.2.0",
    repo=VAD_REPO,
    revision=VAD_REVISION,
    filename="ggml-silero-v6.2.0.bin",
    size_bytes=885_098,
    sha256="2aa269b785eeb53a82983a20501ddf7c1d9c48e33ab63a41391ac6c9f7fb6987",
)


def _order(model: GgmlFile) -> tuple[int, int, int]:
    return (
        SIZE_CLASSES.index(model.size_class),
        BASE_MODELS.index(model.base_model),
        QUANTIZATIONS.index(model.quantization),
    )


def list_models() -> tuple[GgmlFile, ...]:
    """Every model file, by size class, then base model, then fidelity."""
    return tuple(sorted(MODELS, key=_order))


def get_model(model_id) -> GgmlFile | None:
    """The entry with this id, the voice-activity model included, or None.

    None rather than raising, as model_catalog.get_model() answers: the id
    comes from settings, where a file a later version dropped is a normal
    state.
    """
    for model in MODELS + (VAD_MODEL,):
        if model.id == model_id:
            return model
    return None


def variants_of(base_model) -> tuple[GgmlFile, ...]:
    """Every published quantization of `base_model`, most faithful first."""
    return tuple(model for model in list_models() if model.base_model == base_model)


def variant(base_model, quantization) -> GgmlFile | None:
    """The file holding `base_model` at `quantization`, or None if unpublished.

    The question part 11's quantization choice asks: "small at q5_1" is a file,
    and a combination the repository does not publish (large-v3 at q8_0) has
    to read as "not offered" rather than fall back to some other file.
    """
    for model in MODELS:
        if model.base_model == base_model and model.quantization == quantization:
            return model
    return None
