"""Whisper models the user already has, in folders WinZapp did not create.

A user who already transcribes with faster-whisper from a script of their own
has the model on disk — in the Hugging Face cache, or wherever their script put
it — and asking them to download the same 3 GB again into WinZapp's own folder
is what this module exists to avoid. It answers four questions, with no wx and
no network: is this folder a model at all, which model is it, where are the
ones already on this machine, and — once the user has said "use that one" —
where does a run load it from.

The decisions are worth more than the code:

* **A folder of the user's is never copied, moved or deleted.** Everything the
  model store does to its own root (repair, remove, move) is out of the
  question here: the folder belongs to another program, which may still be
  using it. Forgetting a reference removes the record in app.json and touches
  no file — and a reference inside WinZapp's own models root is refused
  outright, so that remove_model() on the root can never delete a model the
  user believes is "external", and a reference can never be left pointing at a
  folder WinZapp is about to move.

* **Only the digest makes a folder "the catalogue's model X".** The model store
  can trust names and exact sizes because it wrote the files itself, after
  hashing them on the way in; a folder somebody else filled has had no such
  check. So `identify()` calls a folder a *candidate* for X when every file of
  X is there at its exact size, and *verified* only when model.bin hashes to
  X's pinned sha256 — and only a verified folder is ever stored as X. This
  matters beyond pedantry: a fine-tune of large-v3 converted the same way has a
  model.bin of exactly the same size (same tensors, same dtype), and treating
  it as large-v3 would hand the automatic choice a model that is not the one it
  budgeted memory for.

* **After the check, the weights are recognised by their identity, not by
  their size.** The model store believes sizes because nobody else writes to
  its root; a folder of the user's is written to by whatever filled it, and the
  case that exists is a script re-running ``ct2-transformers-converter --force``
  into the same folder with a fine-tune: every size identical, weights not. So
  a reference records the *identity mark* of the file model.bin resolves to —
  its size and its st_mtime_ns — taken before the digest (or the trial load)
  and compared after it: a file that changed while it was being checked is not
  stored at all (ACCEPT_CHANGED_WHILE_CHECKED), and one that changes later
  makes the reference REF_CHANGED, never REF_READY. The mark is the file that
  was checked, so the gap between the check and the write to app.json is
  covered too: a change there is a mark that no longer matches. A reference
  without a mark — written by hand, or before the mark existed — is
  REF_UNVERIFIED, never ready. The file id (st_ino) is deliberately not part
  of it: on exFAT and FAT32, what a USB disk usually is, and on an SMB share,
  Windows does not promise it survives a remount, and a mark that changed on
  every replug would send the user to re-hash 3 GB for nothing until they
  learned to ignore the warning. A rewrite through any ordinary tool moves the
  mtime; the Hugging Face cache, whose blobs are named by their content, never
  rewrites one in place. The cost of the mark is in the safe direction: a
  ``touch``, or FAT32's one-hour shift of every mtime when daylight saving
  starts, asks for one more verification and never loads the wrong model.

* **The Hugging Face cache path is a shortcut to a candidate, not a verdict.**
  A snapshot folder names its repository and commit
  (``models--<org>--<repo>/snapshots/<commit>``), and when those are a
  catalogue entry's `repo` and `revision` the folder is the obvious candidate —
  but a folder name is not evidence of its contents, so the sizes are checked
  exactly as for any other folder, and the digest is still what verifies it.
  Sizes are measured on the file a name *resolves* to: in that cache the
  snapshot entries are symbolic links into ``blobs/``, or — on a Windows
  account without the symlink privilege, which huggingface_hub works around —
  plain copies, and both have to read the same.

* **A model the catalogue does not know is a choice, never a default.** Its
  shape can be checked and the backend can be asked to open it
  (`trial_load()`), but nothing says how much memory it needs, which is the
  whole question the automatic choice answers (device.auto_select_model()). So
  such a model is only ever used because the user picked it by name;
  `usable_catalogue_ids()` — what part 10b hands preferences.resolve() as
  `installed_ids` — never contains one.

* **WinZapp's own copy wins over an external one of the same model.** Both are
  the same verified bytes, so choosing costs nothing in quality, and the root
  copy is the one nobody else can change: it sits under the models lock, and no
  other program prunes it, updates it to a new revision or leaves it on a disk
  that is unplugged. The external copy is used when the root has no complete
  copy — including when the root's copy is incomplete, since a usable model
  beats an error.

* **A folder that is gone is its own state.** An external disk that is not
  plugged in, or a cache another program pruned, is neither "not installed"
  (the user knows they have it) nor "corrupted" (nothing was damaged, and
  "download it again" is 3 GB of wrong advice). `reference_state()` answers
  REF_FOLDER_MISSING and a run raises EXTERNAL_MODEL_MISSING. A folder that is
  there and changed is not "corrupted" either: REF_CHANGED raises
  EXTERNAL_MODEL_CHANGED, whose answer is to check it again, since "download
  it" is impossible for a custom model and beside the point for the rest.

* **The references are install-wide, in app.json**, beside the models folder:
  the files belong to the machine, not to an account, and one account using a
  model its sibling cannot see is not a state that means anything. Two account
  processes can add to the list at the same moment, so every write is one
  locked read-modify-write (`AppSettings.update()`); get() then set() would
  let one of them silently drop the other's addition.

**Log rule.** Folder paths go to the log — they are the diagnosis (which drive,
which cache, which spelling) and they name no message. Nothing else this module
touches could name one.

**For the UI layer on progress.** `identify()` and `accept_catalogue_folder()`
report `progress(done, total)` once per megabyte of model.bin, exactly like
model_store.verify_model() — around 3000 calls for large-v3 — so they go through
management.ProgressThrottle before any wx.CallAfter or spoken percentage. Both
hash for up to a minute and `trial_load()` loads for tens of seconds: they run
on a worker thread, never on the UI thread.

**Left for part 10b**, found in review of this part and not yet answered:

* `discover_hf_cache()`, `reference_state()` (and so `usable_catalogue_ids()`
  and `model_directory()`) and `reference_for_path()` touch folders that may be
  on a network share that is down, where one `realpath` or `stat` can wait out
  the SMB timeout. Call them off the wx thread like the hash.
* Changing the models root to a folder that *contains* an external reference
  would put the user's folder under the root, where part 5's "Remove" deletes.
  The root change has to refuse, or warn, when that would happen; `_inside()`
  only guards the other direction, at accept time.
* `accept_*()` can raise the raw `LockTimeout` of `AppSettings.update()` after
  a minute of hashing (another account holding app.json for 10 s); nothing
  turns it into a sentence yet.
* EXTERNAL_MODEL_MISSING and EXTERNAL_MODEL_CHANGED are not in
  `transcription_flow._SETTINGS_OFFER_CODES`, so a failed run does not offer
  the Transcription tab where both are resolved.
* `message_run._decide()` rejects a `model_id` that is not in `installed`,
  which a `custom_choice()` never is; `_model_for()` still calls
  `model_store.ensure_ready()` instead of `model_directory()`; and
  `TranscriptionRequest` has no field for the folder a run loads from.
* A model setting of ``"external:<id>"`` whose reference was *forgotten* is
  10b's to decide, in `preferences.sanitize_section()`/`_resolve_model()`: the
  natural path is to treat it as a retired model — replace it with the
  automatic choice, with the substitution warning the tab already gives.
  Until then the MODEL_NOT_INSTALLED that `_custom_directory()` answers for
  it, and
  `test_a_custom_choice_whose_reference_was_forgotten_is_not_installed`,
  are **provisional**: that code's sentence tells the user to download the
  model, which is impossible for a custom one.
* The Transcription tab needs a **check again** action for an external
  reference, catalogue and custom alike — it is `accept_catalogue_folder()` /
  `accept_custom_folder()` called again on the reference's folder.
  EXTERNAL_MODEL_CHANGED's sentence sends the user to verify the model in
  that tab, and today the tab's "Verify" only covers catalogue models inside
  WinZapp's own root (`model_store.verify_model()`).
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import os
import stat
import time
import uuid
from dataclasses import dataclass

from coord_locks import canonical_dir
from core.transcription import errors, model_catalog, model_store, preferences

#: The install-wide key holding the references, in app_settings.py's own
#: `_DEFAULTS`. Spelled out rather than imported, for the reason
#: preferences.MODELS_DIR_SETTING gives; a test pins the two spellings together.
EXTERNAL_MODELS_SETTING = "transcription_external_models"

# ── What a Whisper model folder has to hold ──────────────────────────────────

# What faster-whisper cannot load without. tokenizer.json above all: without
# it the loader fetches a tokenizer from Hugging Face whatever
# `local_files_only` says (see faster_whisper_backend._TOKENIZER_FILE).
REQUIRED_FILES = ("config.json", "model.bin", "tokenizer.json")
# One or the other, never a fixed name: the Systran tiny..medium conversions
# ship vocabulary.txt and large-v3 and the turbo ship vocabulary.json (see
# model_catalog's docstring on why assuming one shape is expensive).
VOCABULARY_FILES = ("vocabulary.txt", "vocabulary.json")
# How the missing vocabulary is named in FolderShape.missing, since it is
# neither of the two names in particular.
VOCABULARY_PATTERN = "vocabulary.*"

# config.json is a couple of kilobytes. A folder whose "config.json" is larger
# than this is not a model's, and reading it whole to find out would be reading
# whatever the user happened to point at.
_CONFIG_MAX_BYTES = 1024 * 1024

# The same slice model_store hashes in: small enough that Cancel answers at
# once, large enough that 3 GB is not millions of iterations.
_CHUNK_BYTES = 1024 * 1024

# Why a folder cannot be used, as codes part 10b turns into sentences. Codes and
# not errors.TranscriptionError: these are answers to "can I use this folder?",
# asked while the user is choosing, and none of them is a failure of anything.
REFUSED_FOLDER_MISSING = "folder_missing"
REFUSED_NOT_A_FOLDER = "not_a_folder"
REFUSED_UNREADABLE = "unreadable"
REFUSED_FILES_MISSING = "files_missing"
REFUSED_BAD_CONFIG = "bad_config"
# The folder is WinZapp's own models root or something inside it. See the
# module docstring: a model there is already managed, and a reference to it
# would be deleted or moved from under itself.
REFUSED_INSIDE_MODELS_ROOT = "inside_models_root"

REFUSAL_CODES = (
    REFUSED_FOLDER_MISSING,
    REFUSED_NOT_A_FOLDER,
    REFUSED_UNREADABLE,
    REFUSED_FILES_MISSING,
    REFUSED_BAD_CONFIG,
    REFUSED_INSIDE_MODELS_ROOT,
)

# ── What a folder was identified as ──────────────────────────────────────────

# A valid model no catalogue entry claims: the "personalizado" of part 10b.
MATCH_NONE = "none"
# Every file of one catalogue entry at its exact size; model.bin not hashed yet.
MATCH_CANDIDATE = "candidate"
# ...and model.bin's sha256 is that entry's. The only state stored as the model.
MATCH_VERIFIED = "verified"
# The sizes of an entry and different weights: a damaged copy, or a model
# derived from that one (a fine-tune converted the same way is byte-for-byte
# the same size). Either way not that model, and usable only as a custom one.
MATCH_DIGEST_MISMATCH = "digest_mismatch"

# How a candidate was found — for the log and for discovery, which marks the
# snapshots the Hugging Face shortcut recognised.
VIA_HF_CACHE = "hf_cache"
VIA_SIZES = "sizes"

# ── What accepting a folder came to ──────────────────────────────────────────

ACCEPT_ADDED = "added"
# The folder was already referenced (under this spelling or another); its
# record was refreshed rather than duplicated.
ACCEPT_UPDATED = "updated"
# Valid, but no catalogue entry claims it: the caller may offer it as custom.
ACCEPT_NOT_IDENTIFIED = "not_identified"
# The sizes of a catalogue model and different weights; nothing was stored as
# that model (and a record that said it was is marked unverified).
ACCEPT_DIGEST_MISMATCH = "digest_mismatch"
# model.bin was a different file after the digest or the trial load than before
# it — whatever filled the folder rewrote it meanwhile — so the check vouches
# for nothing and nothing was stored. Checking again is the way forward.
ACCEPT_CHANGED_WHILE_CHECKED = "changed_while_checked"

# ── What a stored reference is right now (cheap) ─────────────────────────────

REF_READY = "ready"
# The folder is not there: an unplugged disk, a pruned cache.
REF_FOLDER_MISSING = "folder_missing"
# The folder is there and no longer holds what was accepted: a file missing,
# a size that moved, or a model.bin whose identity mark is not the one that
# was checked. Checking it again is the way forward.
REF_CHANGED = "changed"
# A later check failed the digest, the reference names a catalogue model this
# version no longer knows, or it carries no identity mark to recognise the
# checked weights by. Kept, so the user sees it and decides.
REF_UNVERIFIED = "unverified"

#: The per-account model setting's spelling of "the custom model with this
#: reference id" — the one kind of model that cannot be named by a catalogue
#: id. Defined here, with the resolution that reads it, so part 10b's picker
#: and preferences cannot come to disagree on it.
CUSTOM_CHOICE_PREFIX = "external:"


@dataclass(frozen=True)
class FolderShape:
    """What a folder holds, as far as a Whisper model is concerned.

    `sizes` has every file that matters to identification — the required ones
    and every name the catalogue uses — at the size of the file each name
    resolves to. `missing` names what `refusal` REFUSED_FILES_MISSING is about.
    """

    path: str
    refusal: str | None
    missing: tuple[str, ...] = ()
    sizes: tuple[tuple[str, int], ...] = ()

    @property
    def ok(self) -> bool:
        return self.refusal is None


@dataclass(frozen=True)
class Identification:
    """Which catalogue model a folder is, and how sure that is."""

    path: str
    match: str
    model_id: str | None = None
    via: str | None = None
    shape: FolderShape | None = None


@dataclass(frozen=True)
class ExternalReference:
    """One folder the user told WinZapp to use, as stored in app.json.

    `model_id` is the catalogue id it was verified as, or None for a custom
    model. `verified` is whether the check that kind of model gets passed: the
    digest for a catalogue model, the trial load for a custom one.
    `weights_mark` is the identity mark of the model.bin that check read —
    (size, st_mtime_ns), see the module docstring — or None, which
    reference_state() reads as unverified. `key` is the folder's canonical_dir()
    taken when it was accepted, so that telling two spellings of one folder
    apart inside the app.json lock is a string comparison and never a realpath
    on a disk that may not answer.

    Stored as ``{"id", "path", "key", "model_id", "verified", "weights_mark":
    {"size", "mtime_ns"} | null}``; `_parse()` is what reads it back.
    """

    id: str
    path: str
    model_id: str | None
    verified: bool
    weights_mark: tuple[int, int] | None = None
    key: str = ""

    @property
    def is_custom(self) -> bool:
        return self.model_id is None

    def as_dict(self) -> dict:
        mark = None
        if self.weights_mark is not None:
            size, mtime_ns = self.weights_mark
            mark = {"size": size, "mtime_ns": mtime_ns}
        return {
            "id": self.id,
            "path": self.path,
            "key": self.key,
            "model_id": self.model_id,
            "verified": self.verified,
            "weights_mark": mark,
        }


@dataclass(frozen=True)
class AcceptOutcome:
    """What `accept_*_folder()` did. `code` is an ACCEPT_* or a REFUSED_*."""

    code: str
    reference: ExternalReference | None = None
    identification: Identification | None = None
    shape: FolderShape | None = None


@dataclass(frozen=True)
class CacheSnapshot:
    """One Whisper model found in the Hugging Face cache."""

    path: str
    repo: str
    revision: str
    identification: Identification

    @property
    def model_id(self) -> str | None:
        """The catalogue entry it is a candidate for, or None. Not verified:
        discovery never reads model.bin."""
        if self.identification.match == MATCH_CANDIDATE:
            return self.identification.model_id
        return None


# ── Inspecting a folder ──────────────────────────────────────────────────────


def inspect_folder(path) -> FolderShape:
    """Whether `path` has the shape of a CTranslate2 Whisper model. Cheap.

    Names, sizes and whether config.json parses — nothing is loaded and
    model.bin is not read. A zero-byte file counts as missing: no file any of
    these models needs is empty, and an interrupted copy leaves exactly that.
    preprocessor_config.json is not required here, because only some models
    ship it; for a catalogue model it is one of the files identification
    checks by size.
    """
    path = str(path)
    if not os.path.isdir(path):
        refusal = REFUSED_NOT_A_FOLDER if os.path.exists(path) else REFUSED_FOLDER_MISSING
        return FolderShape(path, refusal)
    try:
        os.listdir(path)
    except OSError:
        return FolderShape(path, REFUSED_UNREADABLE)

    sizes = []
    for name in _names_that_matter():
        size = _resolved_size(os.path.join(path, name))
        if size is not None:
            sizes.append((name, size))
    found = dict(sizes)

    missing = [name for name in REQUIRED_FILES if not found.get(name)]
    if not any(found.get(name) for name in VOCABULARY_FILES):
        missing.append(VOCABULARY_PATTERN)
    if missing:
        return FolderShape(path, REFUSED_FILES_MISSING, tuple(missing), tuple(sizes))
    if not _config_is_an_object(os.path.join(path, "config.json")):
        return FolderShape(path, REFUSED_BAD_CONFIG, (), tuple(sizes))
    return FolderShape(path, None, (), tuple(sizes))


def hf_cache_coordinates(path):
    """(repo, revision) when `path` is a Hugging Face cache snapshot, else None.

    The cache names a repository's folder ``models--<org>--<repo>`` (Hugging
    Face forbids "--" inside a name, so the split is unambiguous) and keeps one
    ``snapshots/<commit>`` folder per revision downloaded. Read off the path
    alone: nothing here says the folder holds what its name claims.
    """
    normalized = os.path.normpath(os.path.abspath(str(path)))
    revision = os.path.basename(normalized)
    snapshots = os.path.dirname(normalized)
    repo = _repo_of_cache_folder(os.path.basename(os.path.dirname(snapshots)))
    if os.path.basename(snapshots).casefold() != "snapshots":
        return None
    if repo is None or not revision:
        return None
    return repo, revision


def identify_quick(path) -> Identification:
    """Which catalogue model `path` could be, from names and sizes only.

    Never reads model.bin, so it is safe to call while a folder picker is
    open; `identify()` is the expensive answer that can say "verified".
    """
    path = str(path)
    shape = inspect_folder(path)
    if not shape.ok:
        return Identification(path, MATCH_NONE, shape=shape)

    coordinates = hf_cache_coordinates(path)
    if coordinates is not None:
        model = _catalogue_entry_at(*coordinates)
        # The name picks the entry; the sizes still have to agree with it.
        if model is not None and _sizes_match(shape, model):
            return Identification(path, MATCH_CANDIDATE, model.id, VIA_HF_CACHE, shape)

    weights = dict(shape.sizes).get("model.bin")
    for model in model_catalog.list_models():
        if model.model_bin_bytes == weights and _sizes_match(shape, model):
            return Identification(path, MATCH_CANDIDATE, model.id, VIA_SIZES, shape)
    return Identification(path, MATCH_NONE, None, None, shape)


def identify(path, progress=None, should_cancel=None) -> Identification:
    """Which catalogue model `path` is, hashing model.bin when it could be one.

    Up to 3 GB read for a candidate: `progress(done, total)` counts model.bin's
    bytes and `should_cancel()` is consulted between chunks (CANCELLED). A
    folder that disappears while it is being read raises
    EXTERNAL_MODEL_MISSING; one that cannot be read otherwise, MODEL_CORRUPTED,
    as model_store.verify_model() does.
    """
    started = time.monotonic()
    quick = identify_quick(path)
    if quick.match != MATCH_CANDIDATE:
        return quick
    model = model_catalog.get_model(quick.model_id)
    digest = _hash_file(
        os.path.join(quick.path, "model.bin"),
        model.model_bin_bytes,
        progress,
        should_cancel,
        quick.path,
    )
    if digest != model.model_bin_sha256:
        logging.info(
            "[transcription] %s has the sizes of %s (via %s) and not its weights: "
            "model.bin sha256 %s, expected %s",
            quick.path, model.id, quick.via, digest, model.model_bin_sha256,
        )
        return dataclasses.replace(quick, match=MATCH_DIGEST_MISMATCH)
    logging.info(
        "[transcription] %s verified as %s (via %s) in %.1fs",
        quick.path, model.id, quick.via, time.monotonic() - started,
    )
    return dataclasses.replace(quick, match=MATCH_VERIFIED)


def trial_load(path, backend, device, compute_type, should_cancel=None) -> None:
    """Have `backend` open the model in `path` once, and let it go again.

    What a custom model is checked with, since there is no size or digest to
    compare it against: its shape, then "the backend can load it". Nothing is
    transcribed and nothing stays in memory (see the backend's own
    `trial_load()`). `device` and `compute_type` are decided by the caller
    through device.py, the way the job decides them — the test is only worth
    something on the device the model will run on.

    Raises EXTERNAL_MODEL_MISSING for a folder that is not there,
    MODEL_CORRUPTED for one that is not shaped like a model (the picker has
    normally said so already, through `inspect_folder()`), and whatever the
    backend's load raised otherwise.
    """
    shape = inspect_folder(path)
    if shape.refusal == REFUSED_FOLDER_MISSING:
        raise errors.TranscriptionError(errors.EXTERNAL_MODEL_MISSING, str(path))
    if not shape.ok:
        raise errors.TranscriptionError(
            errors.MODEL_CORRUPTED,
            f"{path}: {shape.refusal} {', '.join(shape.missing)}".rstrip(),
        )
    backend.trial_load(str(path), device, compute_type, should_cancel=should_cancel)


def candidate_folders(path) -> tuple[str, ...]:
    """The model folders a folder the user picked may stand for.

    Somebody browsing the Hugging Face cache stops at the repository's folder
    (``models--Systran--faster-whisper-large-v3``) far more often than at the
    commit folder two levels below it, which is where the files are. That pick
    stands for its snapshots; any other folder stands for itself.
    """
    path = str(path)
    snapshots = os.path.join(path, "snapshots")
    is_repository = os.path.basename(os.path.normpath(path)).casefold().startswith(
        "models--"
    )
    if is_repository and os.path.isdir(snapshots):
        return tuple(os.path.join(snapshots, name) for name in _sorted_dirs(snapshots))
    return (path,)


# ── The Hugging Face cache ───────────────────────────────────────────────────


def hf_cache_dir(environ=None) -> str:
    """Where huggingface_hub keeps its cache on this machine.

    The same precedence huggingface_hub.constants applies — HF_HUB_CACHE, then
    the legacy HUGGINGFACE_HUB_CACHE, then HF_HOME\\hub, then
    XDG_CACHE_HOME\\huggingface\\hub, then ~\\.cache\\huggingface\\hub — worked out
    here rather than imported: huggingface_hub arrives with faster-whisper,
    which an install may not have, and a user who has neither can still have a
    cache another program filled.
    """
    env = os.environ if environ is None else environ

    def _expand(value):
        return os.path.expandvars(os.path.expanduser(value))

    for name in ("HF_HUB_CACHE", "HUGGINGFACE_HUB_CACHE"):
        if env.get(name):
            return _expand(env[name])
    if env.get("HF_HOME"):
        return os.path.join(_expand(env["HF_HOME"]), "hub")
    cache_home = env.get("XDG_CACHE_HOME") or os.path.join(
        os.path.expanduser("~"), ".cache"
    )
    return os.path.join(_expand(cache_home), "huggingface", "hub")


def discover_hf_cache(cache_dir=None) -> tuple[CacheSnapshot, ...]:
    """Every Whisper model snapshot in the Hugging Face cache, cheaply.

    Names and sizes only — model.bin is never read — so this can run whenever
    the picker opens. Listed: snapshots of a repository with "whisper" in its
    name that have a model's shape. The name filter keeps CTranslate2
    conversions of other models out of a list the user is reading entry by
    entry, and the shape filter keeps out snapshots an interrupted download left
    without its weights. Recognised ones carry the catalogue id they are a
    candidate for (`CacheSnapshot.model_id`).
    """
    root = cache_dir or hf_cache_dir()
    found = []
    for folder in _sorted_dirs(root):
        repo = _repo_of_cache_folder(folder)
        if repo is None or "whisper" not in repo.casefold():
            continue
        snapshots = os.path.join(root, folder, "snapshots")
        for revision in _sorted_dirs(snapshots):
            path = os.path.join(snapshots, revision)
            identification = identify_quick(path)
            if identification.shape is None or not identification.shape.ok:
                continue
            found.append(CacheSnapshot(path, repo, revision, identification))
    logging.info(
        "[transcription] Hugging Face cache %s: %d Whisper snapshot(s), "
        "%d recognised",
        root, len(found), sum(1 for entry in found if entry.model_id),
    )
    return tuple(found)


# ── The references ───────────────────────────────────────────────────────────


def load_references(app_settings) -> tuple[ExternalReference, ...]:
    """The stored references, in the order they were added.

    Tolerant of anything, like preferences.read_section(): the value is a list
    in a file a user can edit, and an entry that makes no sense is dropped
    rather than allowed to keep the transcription tab from opening. `None` —
    an account-less window with no app settings — reads as "none stored".
    """
    if app_settings is None:
        return ()
    return _parse(app_settings.get(EXTERNAL_MODELS_SETTING))


def find_reference(references, reference_id):
    """The reference with this id, or None."""
    for reference in references:
        if reference.id == reference_id:
            return reference
    return None


def reference_for_path(references, path):
    """The reference to the folder `path` names, under any spelling, or None.

    For marking what discovery lists that is already in use, by the same rule
    `_store()` applies when refusing a duplicate: the canonical folder,
    compared with the `key` stored at accept time. Resolving `path` touches
    the disk (see "Left for part 10b" above).
    """
    key = canonical_dir(str(path))
    for reference in references:
        if reference.key == key:
            return reference
    return None


def accept_catalogue_folder(app_settings, path, models_root, progress=None,
                            should_cancel=None) -> AcceptOutcome:
    """Verify `path` as a catalogue model and, only if it is one, remember it.

    The hash runs here, in the same call as the write, so no caller can store
    a catalogue reference that was not verified — a candidate is not enough.
    A folder whose weights are not the catalogue's comes back as
    ACCEPT_DIGEST_MISMATCH and one nobody claims as ACCEPT_NOT_IDENTIFIED;
    either may still be offered as custom (`accept_custom_folder()`).
    Cancelling (CANCELLED) stores nothing, and neither does a model.bin that
    was rewritten while it was being hashed (ACCEPT_CHANGED_WHILE_CHECKED).
    Accepting a folder again is also how it is checked again: the record is
    refreshed, identity mark included.
    """
    path = str(path)
    shape = inspect_folder(path)
    if not shape.ok:
        return AcceptOutcome(shape.refusal, shape=shape)
    if _inside(path, models_root):
        return AcceptOutcome(REFUSED_INSIDE_MODELS_ROOT, shape=shape)

    before = _weights_mark(path)
    identification = identify(path, progress=progress, should_cancel=should_cancel)
    mark = _weights_mark(path)
    if mark is None or mark != before:
        return _changed_while_checked(path, before, mark, identification, shape)
    if identification.match == MATCH_VERIFIED:
        code, reference = _store(
            app_settings, path, identification.model_id, True, mark
        )
        return AcceptOutcome(code, reference, identification, shape)
    if identification.match == MATCH_DIGEST_MISMATCH:
        # A folder already stored as this model has just been shown not to be
        # it: it stops counting as that model, and is kept so the user sees it
        # rather than wondering where it went.
        _store(app_settings, path, identification.model_id, False, mark,
               only_if_present=True)
        return AcceptOutcome(ACCEPT_DIGEST_MISMATCH, None, identification, shape)
    return AcceptOutcome(ACCEPT_NOT_IDENTIFIED, None, identification, shape)


def accept_custom_folder(app_settings, path, models_root, backend, device,
                         compute_type, should_cancel=None) -> AcceptOutcome:
    """Trial-load `path` and, if the backend opened it, remember it as custom.

    For a folder the catalogue does not claim — part 10b calls this only after
    the user explicitly chose to use it anyway. The trial's own failure is
    raised as it came (INSUFFICIENT_VRAM, BACKEND_ERROR, ...) and stores
    nothing; a record that already exists is left as it was, because a trial
    can fail for a reason that is not the folder's (another model holding the
    card) and one bad moment must not unmake the user's choice. A cancel that
    arrives during the load is honoured once the load returns: nothing stored.
    Nor is anything stored when model.bin was a different file after the load
    than before it (ACCEPT_CHANGED_WHILE_CHECKED): the trial vouched for
    weights that are no longer there.
    """
    path = str(path)
    shape = inspect_folder(path)
    if not shape.ok:
        return AcceptOutcome(shape.refusal, shape=shape)
    if _inside(path, models_root):
        return AcceptOutcome(REFUSED_INSIDE_MODELS_ROOT, shape=shape)

    before = _weights_mark(path)
    trial_load(path, backend, device, compute_type, should_cancel=should_cancel)
    _check_cancel(should_cancel)
    mark = _weights_mark(path)
    if mark is None or mark != before:
        return _changed_while_checked(path, before, mark, None, shape)
    code, reference = _store(app_settings, path, None, True, mark)
    return AcceptOutcome(code, reference, None, shape)


def forget_reference(app_settings, reference_id) -> bool:
    """Drop one reference from app.json. True if there was one to drop.

    The folder and every file in it are left exactly as they are — that is the
    whole of "remove" for a model WinZapp does not own (see the module
    docstring).
    """
    forgotten = []

    def change(current):
        kept = []
        for reference in _parse(current):
            if reference.id == reference_id:
                forgotten.append(reference)
            else:
                kept.append(reference.as_dict())
        return kept

    app_settings.update(EXTERNAL_MODELS_SETTING, change)
    for reference in forgotten:
        logging.info(
            "[transcription] forgot the external model at %s (its files were "
            "left in place)",
            reference.path,
        )
    return bool(forgotten)


def reference_state(reference) -> str:
    """REF_READY, REF_FOLDER_MISSING, REF_CHANGED or REF_UNVERIFIED. Cheap.

    Names, sizes and model.bin's identity mark, never a hash — this is asked
    before every transcription, exactly like model_store.installation_state().
    Its promise is narrower than that one's, because another program writes
    here: the files still have the catalogue's sizes *and* model.bin is still
    the very file that was checked (see the module docstring). A reference
    with no mark to compare is never ready.
    """
    shape = inspect_folder(reference.path)
    if shape.refusal == REFUSED_FOLDER_MISSING:
        return REF_FOLDER_MISSING
    if not shape.ok:
        return REF_CHANGED
    if not reference.is_custom:
        model = model_catalog.get_model(reference.model_id)
        if model is None:
            return REF_UNVERIFIED
        if not _sizes_match(shape, model):
            return REF_CHANGED
    if reference.weights_mark is None:
        return REF_UNVERIFIED
    if _weights_mark(reference.path) != reference.weights_mark:
        return REF_CHANGED
    return REF_READY if reference.verified else REF_UNVERIFIED


# ── Where a model is loaded from ─────────────────────────────────────────────


def custom_choice(reference) -> str:
    """The model-setting value that selects this custom reference."""
    return f"{CUSTOM_CHOICE_PREFIX}{reference.id}"


def custom_reference_id(choice):
    """The reference id a model-setting value selects, or None for a catalogue id."""
    if isinstance(choice, str) and choice.startswith(CUSTOM_CHOICE_PREFIX):
        return choice[len(CUSTOM_CHOICE_PREFIX):] or None
    return None


def usable_catalogue_ids(models_root, references=()) -> tuple[str, ...]:
    """Catalogue ids a run could load right now, in the catalogue's order.

    What part 10b passes preferences.resolve() as `installed_ids`, in place of
    model_store.list_installed(): complete in the models root, or referenced
    externally and REF_READY. Never a custom model — see the module docstring
    on why the automatic choice cannot budget for one — and never a reference
    whose folder is missing, which would have the automatic choice pick a model
    the run then cannot find.
    """
    ready = {
        reference.model_id
        for reference in references
        if not reference.is_custom and reference_state(reference) == REF_READY
    }
    return tuple(
        model.id
        for model in model_catalog.list_models()
        if model.id in ready or model_store.is_installed(models_root, model)
    )


def model_directory(models_root, choice, references=()) -> str:
    """The folder to load the model `choice` names from, or the right error.

    `choice` is a catalogue id or `custom_choice(reference)`. For a catalogue
    id WinZapp's own complete copy wins, then the first external reference
    that is REF_READY (see the module docstring for why); with no reference
    at all this answers exactly what model_store.ensure_ready() answers, which
    is what lets part 10b swap it in without changing anything for a user who
    never pointed WinZapp anywhere.

    Failures: MODEL_CORRUPTED when the root's copy is incomplete,
    EXTERNAL_MODEL_MISSING when the only copy is in a folder that is not
    there, EXTERNAL_MODEL_CHANGED when it is there and changed since it was
    checked, MODEL_NOT_INSTALLED otherwise. A custom choice fails with the
    same two external codes, and EXTERNAL_MODEL_CHANGED also when its
    reference is unverified (see `_custom_directory()`).

    An *unverified* catalogue reference is left to MODEL_NOT_INSTALLED: the
    way WinZapp writes one is a digest that did not match, where checking again
    fails again and WinZapp's own download is what gives the user that model.
    A reference with no `weights_mark` (written by hand, or before the mark
    existed) is unverified as well and lands here too, although for that one
    checking again can pass.
    """
    reference_id = custom_reference_id(choice)
    if reference_id is not None:
        return _custom_directory(find_reference(references, reference_id), choice)

    model = model_catalog.get_model(choice)
    if model is None:
        return model_store.ensure_ready(models_root, choice)
    if model_store.is_installed(models_root, model):
        return model_store.model_dir(models_root, model.id)

    states = []
    for reference in references:
        if reference.model_id != model.id:
            continue
        state = reference_state(reference)
        if state == REF_READY:
            logging.info(
                "[transcription] model %s is loaded from the external folder %s",
                model.id, reference.path,
            )
            return reference.path
        states.append((state, reference.path))

    if model_store.installation_state(models_root, model).state != model_store.STATE_ABSENT:
        # The root's copy is incomplete: its own sentence ("download it again")
        # is the right one, and it is what ensure_ready() says.
        return model_store.ensure_ready(models_root, model.id)
    for state, folder in states:
        if state == REF_FOLDER_MISSING:
            raise errors.TranscriptionError(
                errors.EXTERNAL_MODEL_MISSING, f"{model.id}: {folder}"
            )
    for state, folder in states:
        if state == REF_CHANGED:
            raise errors.TranscriptionError(
                errors.EXTERNAL_MODEL_CHANGED,
                f"{model.id}: {folder} changed since it was verified",
            )
    return model_store.ensure_ready(models_root, model.id)


# ── Internals ────────────────────────────────────────────────────────────────


def _custom_directory(reference, choice) -> str:
    if reference is None or not reference.is_custom:
        # A reference forgotten from another window, or a hand-edited setting.
        # Provisional: this code's sentence says "download the model", which
        # is impossible for a custom one. What a forgotten custom choice
        # becomes is part 10b's decision — see the item on it under "Left for
        # part 10b" in the module docstring.
        raise errors.TranscriptionError(errors.MODEL_NOT_INSTALLED, str(choice))
    state = reference_state(reference)
    if state == REF_READY:
        return reference.path
    if state == REF_FOLDER_MISSING:
        raise errors.TranscriptionError(errors.EXTERNAL_MODEL_MISSING, reference.path)
    if state == REF_CHANGED:
        raise errors.TranscriptionError(
            errors.EXTERNAL_MODEL_CHANGED, f"{reference.path} changed since its trial load"
        )
    # Unverified: the same code as a change, on purpose. The code is all the
    # flow receives, and for a custom model "download it" (MODEL_NOT_INSTALLED)
    # is impossible advice; the one thing that fixes it is what fixes a change
    # — check the folder again, or choose another model. And WinZapp never
    # writes an unverified custom record: one exists only without an identity
    # mark (written by hand, or before the mark existed), where nothing can
    # tell whether the weights are still the ones the trial loaded — which,
    # as far as the user can act on it, is a change.
    raise errors.TranscriptionError(
        errors.EXTERNAL_MODEL_CHANGED,
        f"{reference.path} has no passed trial load to vouch for it",
    )


def _store(app_settings, path, model_id, verified, weights_mark,
           only_if_present=False):
    """Add or refresh the reference to `path`, in one locked step.

    The comparison with what is already stored happens inside the lock, on
    canonical folders: two accounts adding the same folder at once end with
    one record, and so does one account adding it under two spellings (case,
    a junction). Returns (ACCEPT_ADDED or ACCEPT_UPDATED, the reference) —
    or (None, None) when `only_if_present` and there was nothing to refresh.

    Only strings are compared inside the lock. The new folder's canonical form
    is worked out before taking it, and every stored one was worked out when
    it was accepted (`ExternalReference.key`): a realpath of each stored folder
    in here would, for one on a share that is down, hold app.json through the
    SMB timeout, and every other account's save would hit its 10 s LockTimeout.
    """
    key = canonical_dir(path)
    outcome = []

    def change(current):
        references = list(_parse(current))
        for index, existing in enumerate(references):
            if existing.key == key:
                if only_if_present and existing.is_custom:
                    # A folder the user already keeps as custom was never
                    # claimed to be the catalogue's model, so a digest that
                    # says it is not one changes nothing about it.
                    outcome[:] = [(None, None)]
                    break
                refreshed = ExternalReference(
                    existing.id, path, model_id, verified, weights_mark, key
                )
                references[index] = refreshed
                outcome[:] = [(ACCEPT_UPDATED, refreshed)]
                break
        else:
            if only_if_present:
                outcome[:] = [(None, None)]
                return [reference.as_dict() for reference in references]
            created = ExternalReference(
                _new_reference_id(), path, model_id, verified, weights_mark, key
            )
            references.append(created)
            outcome[:] = [(ACCEPT_ADDED, created)]
        return [reference.as_dict() for reference in references]

    app_settings.update(EXTERNAL_MODELS_SETTING, change)
    code, reference = outcome[0]
    if reference is not None:
        logging.info(
            "[transcription] external model %s: %s as %s (verified=%s)",
            code, path, model_id or "custom", verified,
        )
    return code, reference


def _new_reference_id() -> str:
    """A short random id. Random rather than counted, so that two processes
    adding at once cannot both mint "the next one"."""
    return uuid.uuid4().hex[:12]


def _parse(value) -> tuple[ExternalReference, ...]:
    """The stored list as references, without anything that makes no sense.

    Never mutates `value`: it is the list AppSettings.update() is in the
    middle of replacing, and the new one is built beside it.

    A record from before `key` was stored gets the cheap spelling of its path
    (normcase + abspath, no disk access) rather than a realpath, since this
    runs inside the app.json lock; the worst that costs is a second record for
    a folder first stored under another spelling. A record without a
    well-formed `weights_mark` gets None, which reference_state() reads as
    unverified — never as ready.
    """
    if not isinstance(value, list):
        return ()
    references = []
    seen = set()
    for entry in value:
        if not isinstance(entry, dict):
            continue
        reference_id = entry.get("id")
        path = entry.get("path")
        if not (isinstance(reference_id, str) and reference_id
                and isinstance(path, str) and path) or reference_id in seen:
            continue
        model_id = entry.get("model_id")
        key = entry.get("key")
        references.append(ExternalReference(
            id=reference_id,
            path=path,
            model_id=model_id if isinstance(model_id, str) and model_id else None,
            # Anything but a literal True is unverified: a hand edit writing
            # "yes" must not turn an unchecked folder into a trusted one.
            verified=entry.get("verified") is True,
            weights_mark=_parse_mark(entry.get("weights_mark")),
            key=key if isinstance(key, str) and key
            else os.path.normcase(os.path.abspath(path)),
        ))
        seen.add(reference_id)
    return tuple(references)


def _inside(path, models_root) -> bool:
    """Whether `path` is WinZapp's models root or anything under it.

    Canonical on both sides — the same reason as model_store.move_models():
    abspath neither folds case nor resolves a junction, and a spelling that
    slipped past this check is a reference remove_model() can delete.

    An empty `models_root` is the stored "default folder" (see
    preferences.resolve_models_dir()), not "no root": it is resolved by the
    rule every other caller uses, or a folder inside the default root would
    be accepted as external the moment a caller passed the stored value.
    """
    root = preferences.resolve_models_dir(models_root)
    child = canonical_dir(path)
    parent = canonical_dir(root)
    return child == parent or child.startswith(parent.rstrip(os.sep) + os.sep)


def _names_that_matter() -> tuple[str, ...]:
    """Every file name identification can ask about, each once."""
    names = list(REQUIRED_FILES) + list(VOCABULARY_FILES)
    for model in model_catalog.list_models():
        names.extend(name for name, _size in model.files)
    return tuple(dict.fromkeys(names))


def _resolved_size(path):
    """The size of the file `path` resolves to, or None if there is none.

    Resolved explicitly: in the Hugging Face cache a snapshot entry is usually
    a symbolic link into ``blobs/``, and the link's own size (what lstat says)
    is a few dozen bytes. A link whose blob is gone answers None — missing, as
    it is for faster-whisper.
    """
    info = _resolved_stat(path)
    return None if info is None else info.st_size


def _weights_mark(folder):
    """(size, st_mtime_ns) of the file `folder`'s model.bin resolves to, or None.

    The identity mark of the module docstring. Resolved like `_resolved_size()`
    and for the same reason: through a Hugging Face snapshot link it is the
    blob that was hashed, and the blob that would be rewritten.
    """
    info = _resolved_stat(os.path.join(str(folder), "model.bin"))
    return None if info is None else (info.st_size, info.st_mtime_ns)


def _resolved_stat(path):
    try:
        info = os.stat(os.path.realpath(path))
    except (OSError, ValueError):
        return None
    return info if stat.S_ISREG(info.st_mode) else None


def _parse_mark(value):
    """A stored `weights_mark` as the tuple `_weights_mark()` returns, or None.

    Only whole numbers make a mark (a bool does not): anything else is a hand
    edit or a shape this version does not know, and must read as unverified.
    """
    if not isinstance(value, dict):
        return None
    size, mtime_ns = value.get("size"), value.get("mtime_ns")
    for number in (size, mtime_ns):
        if not isinstance(number, int) or isinstance(number, bool):
            return None
    return size, mtime_ns


def _changed_while_checked(path, before, after, identification, shape):
    logging.info(
        "[transcription] %s: model.bin changed while it was being checked "
        "(size, mtime_ns %s -> %s); nothing stored",
        path, before, after,
    )
    return AcceptOutcome(ACCEPT_CHANGED_WHILE_CHECKED, None, identification, shape)


def _repo_of_cache_folder(name):
    """"org/repo" for a Hugging Face cache folder ``models--org--repo``, or None.

    One reading for the path shortcut and for discovery, so the two cannot
    disagree on which folders are repositories: "--" is the separator (Hugging
    Face forbids it inside a name), and a part it leaves empty means the name
    is not one the Hub wrote.
    """
    if not name.casefold().startswith("models--"):
        return None
    parts = name[len("models--"):].split("--")
    if not all(parts):
        return None
    return "/".join(parts)


def _config_is_an_object(path) -> bool:
    try:
        if os.path.getsize(path) > _CONFIG_MAX_BYTES:
            return False
        with open(path, "r", encoding="utf-8") as fh:
            return isinstance(json.load(fh), dict)
    except (OSError, ValueError):
        return False


def _catalogue_entry_at(repo, revision):
    """The catalogue entry pinned at exactly this repository and commit.

    The repository compared without case (the cache keeps whatever case the
    downloading script typed, and the Hub resolves either); the commit
    exactly, since it is a hash.
    """
    for model in model_catalog.list_models():
        if model.repo.casefold() == repo.casefold() and model.revision == revision.lower():
            return model
    return None


def _sizes_match(shape, model) -> bool:
    """Whether every file of `model` is in `shape` at its exact size."""
    found = dict(shape.sizes)
    return all(found.get(name) == size for name, size in model.files)


def _sorted_dirs(path) -> list:
    try:
        entries = os.listdir(path)
    except OSError:
        return []
    return sorted(name for name in entries if os.path.isdir(os.path.join(path, name)))


def _hash_file(path, total_bytes, progress, should_cancel, folder) -> str:
    """sha256 of `path`, reported and cancellable as it goes."""
    digest = hashlib.sha256()
    read = 0
    try:
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(_CHUNK_BYTES), b""):
                _check_cancel(should_cancel)
                digest.update(chunk)
                read += len(chunk)
                _report(progress, read, total_bytes)
    except OSError as exc:
        if not os.path.isdir(folder):
            # Unplugged or deleted while it was being read: the folder's own
            # state, not damage (see errors.EXTERNAL_MODEL_MISSING).
            raise errors.TranscriptionError(
                errors.EXTERNAL_MODEL_MISSING, f"{folder}: {exc}"
            ) from exc
        raise errors.TranscriptionError(errors.MODEL_CORRUPTED, f"{path}: {exc}") from exc
    return digest.hexdigest()


def _check_cancel(should_cancel) -> None:
    if should_cancel is not None and should_cancel():
        raise errors.TranscriptionError(errors.CANCELLED, "cancelled by the user")


def _report(progress, done, total) -> None:
    if progress is not None:
        progress(done, total)
