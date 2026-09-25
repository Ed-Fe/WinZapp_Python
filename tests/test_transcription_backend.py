"""Running a transcription: the backend, the audio it is fed, and the job.

Everything below is a failure the user experiences as the same thing — they
asked for a transcription and something went wrong, out loud, in a window they
cannot see. The mechanisms behind that are specific:

* **`local_files_only=True` is the offline promise of the whole feature.**
  Without it faster-whisper treats the model directory as a Hugging Face
  repository id the moment the directory does not look right, and fetches over
  the network — the audio of a private conversation transcribed by a component
  that phones home. There is a test here whose only job is to fail if that
  argument is ever dropped.

* **CTranslate2's error strings are not an API.** They come out of a C++ layer
  and are reworded between versions, so they are matched by fragment. Each
  family is pinned here, *including* the unknown one: when a message stops
  matching, the answer has to degrade to BACKEND_ERROR — a generic "internal
  error" that still logs the real text — and never to a different diagnosis.
  Telling a user with plenty of VRAM to pick a smaller model, or one with a
  broken driver to buy memory, sends them off to fix what is not broken.

* **The four audio failures are four different sentences.** "It has not been
  downloaded yet" (wait), "this is not audio" (nothing to do), "it is damaged"
  (nothing to do), "the conversion failed" (report it). ffmpeg does not hand
  those over neatly, so the ones that can be told apart are, and the rest fall
  to the generic code on purpose.

* **A cancelled run must leave nothing behind.** The converted WAV can be an
  hour of audio; the ffmpeg process holds the media file open. So cancellation
  is checked in every phase, and every phase is asserted to have deleted its
  temporary and killed its process rather than abandoned it.

* **The log may not carry what was said.** The issue is explicit: backend,
  model, device, languages, durations and technical error text — never the
  transcribed text, and never the audio path, whose file name *is* the WhatsApp
  message id. The last two tests walk this package's logging calls statically
  and then run a whole job with a distinctive sentence in it.

No model is ever loaded, no network is touched and no GPU is required: the
backend gets a fake model factory, and ffmpeg is a small script pretending to
be one.
"""

import ast
import builtins
import inspect
import logging
import os
import re
import sys
import tempfile
import threading
import time

import pytest

from core.transcription import (
    audio_prep,
    backend as backend_module,
    device,
    errors,
    faster_whisper_backend,
    job as job_module,
    model_store,
)


# ── Fakes ────────────────────────────────────────────────────────────────────


class _Segment:
    """What faster-whisper yields: start, end and text, and nothing we need."""

    def __init__(self, start, end, text):
        self.start = start
        self.end = end
        self.text = text


class _Info:
    def __init__(self, language="pt", language_probability=0.99, duration=4.0):
        self.language = language
        self.language_probability = language_probability
        self.duration = duration


class _FakeWhisper:
    """A loaded model that yields prepared segments instead of decoding."""

    def __init__(self, factory, segments, info, transcribe_error, vad_error,
                 on_segment):
        self._factory = factory
        self._segments = segments
        self._info = info
        self._transcribe_error = transcribe_error
        self._vad_error = vad_error
        self._on_segment = on_segment
        self.yielded = 0

    def transcribe(self, audio, language=None, vad_filter=False, **kwargs):
        self._factory.transcribe_calls.append(
            {"audio": audio, "language": language, "vad_filter": vad_filter}
        )
        if vad_filter and self._vad_error is not None:
            raise self._vad_error
        if self._transcribe_error is not None:
            raise self._transcribe_error

        def _generate():
            for segment in self._segments:
                self.yielded += 1
                yield segment
                if self._on_segment is not None:
                    self._on_segment(self.yielded)

        return _generate(), self._info


class _Factory:
    """Stands in for `faster_whisper.WhisperModel`, recording every load."""

    def __init__(self, segments=(), info=None, load_error=None,
                 transcribe_error=None, vad_error=None, on_segment=None):
        self.segments = list(segments)
        self.info = info if info is not None else _Info()
        self.load_error = load_error
        self.transcribe_error = transcribe_error
        self.vad_error = vad_error
        self.on_segment = on_segment
        self.loads = []
        self.transcribe_calls = []
        self.models = []

    def __call__(self, path, **kwargs):
        self.loads.append({"path": path, **kwargs})
        if self.load_error is not None:
            raise self.load_error
        model = _FakeWhisper(
            self, self.segments, self.info, self.transcribe_error,
            self.vad_error, self.on_segment,
        )
        self.models.append(model)
        return model


class _FakeBackend(backend_module.TranscriptionBackend):
    """A backend for the job's tests: no model, no audio, no waiting."""

    id = "fake"

    def __init__(self, result=None, error=None, on_load=None, on_transcribe=None,
                 available=True):
        self._result = result
        self._error = error
        self._on_load = on_load
        self._on_transcribe = on_transcribe
        self._available = available
        self.requests = []

    def is_available(self):
        return self._available

    def load_model(self, request, should_cancel=None):
        if self._on_load is not None:
            self._on_load(request)

    def transcribe(self, request, progress=None, should_cancel=None):
        self.requests.append(request)
        if self._on_transcribe is not None:
            self._on_transcribe(request)
        if should_cancel is not None and should_cancel():
            raise errors.TranscriptionError(errors.CANCELLED, "cancelled")
        if self._error is not None:
            raise self._error
        if progress is not None:
            progress(1.0)
        return self._result


def _request(tmp_path, **overrides):
    fields = {
        "audio_path": str(tmp_path / "prepared.wav"),
        "models_root": str(tmp_path / "models"),
        "model_id": "tiny",
        "device": device.DEVICE_CPU,
        "compute_type": device.COMPUTE_INT8,
        "duration_seconds": 4.0,
    }
    fields.update(overrides)
    return backend_module.TranscriptionRequest(**fields)


def _ready_model_dir(monkeypatch, tmp_path, name="tiny"):
    """Pretend the model store has a complete model, and say where.

    tokenizer.json is written because the backend refuses to load without it —
    see the test that pins why.
    """
    directory = tmp_path / "models" / name
    directory.mkdir(parents=True)
    (directory / "tokenizer.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        model_store, "ensure_ready", lambda root, model_id: str(directory)
    )
    return str(directory)


def _fake_ffmpeg(tmp_path, name="ffmpeg", returncode=0, stderr="", seconds=1.0,
                 sleep=0.0, marker=None):
    """An executable that behaves like ffmpeg for one scenario.

    A generated script behind a launcher, because the code under test spawns a
    real process on purpose: the cancellation path has to kill one, and the
    stderr has to come back through a real pipe.
    """
    script = tmp_path / f"{name}.py"
    script.write_text(
        "import sys, time, wave\n"
        "output = sys.argv[-1]\n"
        f"time.sleep({sleep!r})\n"
        f"sys.stderr.write({stderr!r})\n"
        f"seconds = {seconds!r}\n"
        "if seconds >= 0:\n"
        "    handle = wave.open(output, 'wb')\n"
        "    handle.setnchannels(1)\n"
        "    handle.setsampwidth(2)\n"
        "    handle.setframerate(16000)\n"
        "    handle.writeframes(b'\\x00\\x00' * int(16000 * seconds))\n"
        "    handle.close()\n"
        f"marker = {marker!r}\n"
        "if marker:\n"
        "    open(marker, 'w').write('done')\n"
        f"sys.exit({returncode!r})\n",
        encoding="utf-8",
    )
    if sys.platform == "win32":
        launcher = tmp_path / f"{name}.bat"
        launcher.write_text(
            f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n', encoding="utf-8"
        )
    else:
        launcher = tmp_path / f"{name}.sh"
        launcher.write_text(
            f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding="utf-8"
        )
        os.chmod(launcher, 0o755)
    return str(launcher)


def _voice_note(tmp_path, name="3A1B2C3D4E5F6A7B.wzmedia", data=b"\x00" * 2048):
    """A stand-in media file, named the way WinZapp names them: by message id."""
    path = tmp_path / name
    path.write_bytes(data)
    return str(path)


@pytest.fixture
def own_temp_dir(tmp_path, monkeypatch):
    """Every temporary this package makes, in a directory the test can count."""
    private = tmp_path / "temp"
    private.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(private))
    return private


def _leftovers(temp_dir):
    return sorted(p.name for p in temp_dir.glob("winzapp-transcribe-*"))


# ── The backend seam ─────────────────────────────────────────────────────────


class TestBackendAvailability:
    """`is_available()` is asked on the machine where the package is missing.

    It is what the settings UI consults before offering the backend at all, so
    it has to answer on an install that has never had faster-whisper — and
    answer without importing it, since the import alone loads hundreds of
    megabytes of CUDA libraries.
    """

    def test_a_missing_package_answers_false_instead_of_raising(self, monkeypatch):
        import importlib.util

        monkeypatch.setattr(
            importlib.util, "find_spec",
            lambda name, *a, **kw: None if name == "faster_whisper" else object(),
        )
        assert faster_whisper_backend.FasterWhisperBackend().is_available() is False

    def test_find_spec_blowing_up_answers_false_too(self, monkeypatch):
        import importlib.util

        def _explode(name, *args, **kwargs):
            raise ValueError("damaged metadata")

        monkeypatch.setattr(importlib.util, "find_spec", _explode)
        assert faster_whisper_backend.FasterWhisperBackend().is_available() is False

    def test_it_says_yes_when_both_packages_are_importable(self, monkeypatch):
        import importlib.util

        monkeypatch.setattr(importlib.util, "find_spec", lambda name, *a, **kw: object())
        assert faster_whisper_backend.FasterWhisperBackend().is_available() is True

    def test_the_backend_is_never_imported_to_answer(self, monkeypatch):
        """A yes/no answer may not cost a multi-hundred-megabyte import."""
        imported = []
        real_import = builtins.__import__

        def _watch(name, *args, **kwargs):
            imported.append(name)
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _watch)
        faster_whisper_backend.FasterWhisperBackend().is_available()
        assert not [n for n in imported if n.split(".")[0] in
                    ("faster_whisper", "ctranslate2")]


class TestBackendRegistry:
    def test_the_same_instance_is_handed_out_every_time(self):
        """The loaded model is cached on the instance — a new one per call
        would reload several gigabytes per transcription."""
        first = backend_module.get_backend(backend_module.BACKEND_FASTER_WHISPER)
        second = backend_module.get_backend(backend_module.BACKEND_FASTER_WHISPER)
        assert first is second

    def test_an_unknown_id_is_none_rather_than_an_error(self):
        assert backend_module.get_backend("whisper.cpp-from-2031") is None
        assert backend_module.get_backend(None) is None

    def test_the_configured_backend_wins_when_it_can_run(self, monkeypatch):
        usable = _FakeBackend(available=True)
        monkeypatch.setitem(backend_module._instances, "fake", usable)
        monkeypatch.setattr(backend_module, "BACKEND_IDS", ("fake",))
        assert backend_module.resolve_backend("fake") is usable

    def test_a_configured_backend_that_cannot_run_falls_through(self, monkeypatch):
        """The settings value outlives the installation it was written on."""
        broken = _FakeBackend(available=False)
        usable = _FakeBackend(available=True)
        monkeypatch.setitem(backend_module._instances, "broken", broken)
        monkeypatch.setitem(backend_module._instances, "fake", usable)
        monkeypatch.setattr(backend_module, "BACKEND_IDS", ("fake",))
        assert backend_module.resolve_backend("broken") is usable

    def test_nothing_usable_is_backend_missing(self, monkeypatch):
        monkeypatch.setitem(backend_module._instances, "fake", _FakeBackend(available=False))
        monkeypatch.setattr(backend_module, "BACKEND_IDS", ("fake",))
        with pytest.raises(errors.TranscriptionError) as caught:
            backend_module.resolve_backend("fake")
        assert caught.value.code == errors.BACKEND_MISSING

    def test_two_threads_asking_at_once_still_get_one_instance(self, monkeypatch):
        """Two transcriptions started together would otherwise load the model
        into memory twice, and a release() on one of them would free half."""
        built = []

        def _slow_construct(backend_id):
            time.sleep(0.05)
            instance = _FakeBackend()
            built.append(instance)
            return instance

        monkeypatch.setattr(backend_module, "_instances", {})
        monkeypatch.setattr(backend_module, "_construct", _slow_construct)
        handed_out = []
        threads = [
            threading.Thread(
                target=lambda: handed_out.append(backend_module.get_backend("fake"))
            )
            for _ in range(4)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)

        assert len(built) == 1
        assert len({id(backend) for backend in handed_out}) == 1

    def test_available_ids_keep_preference_order(self, monkeypatch):
        monkeypatch.setitem(backend_module._instances, "a", _FakeBackend(available=True))
        monkeypatch.setitem(backend_module._instances, "b", _FakeBackend(available=False))
        monkeypatch.setitem(backend_module._instances, "c", _FakeBackend(available=True))
        monkeypatch.setattr(backend_module, "BACKEND_IDS", ("a", "b", "c"))
        assert backend_module.available_backend_ids() == ("a", "c")


# ── Loading the model ────────────────────────────────────────────────────────


class TestModelLoading:
    def test_the_model_is_loaded_from_the_directory_the_store_vouched_for(
        self, tmp_path, monkeypatch
    ):
        directory = _ready_model_dir(monkeypatch, tmp_path)
        factory = _Factory(segments=[_Segment(0.0, 1.0, "a")])
        backend = faster_whisper_backend.FasterWhisperBackend(model_factory=factory)

        backend.load_model(_request(tmp_path))

        assert factory.loads[0]["path"] == directory

    def test_the_load_is_offline_and_stays_offline(self, tmp_path, monkeypatch):
        """The one line that keeps a private conversation on this machine.

        Without `local_files_only=True`, faster-whisper falls back to treating
        its first argument as a Hugging Face repository id and downloads —
        which for a feature sold as local transcription is the whole promise
        broken, silently. This test exists to fail if the flag is dropped.
        """
        _ready_model_dir(monkeypatch, tmp_path)
        factory = _Factory()
        backend = faster_whisper_backend.FasterWhisperBackend(model_factory=factory)

        backend.load_model(_request(tmp_path))

        assert factory.loads[0]["local_files_only"] is True
        # Nothing may steer it at a cache or a repository either.
        assert "download_root" not in factory.loads[0]
        assert "repo_id" not in factory.loads[0]

    def test_the_device_and_compute_type_are_passed_through_untouched(
        self, tmp_path, monkeypatch
    ):
        """device.py decides these; the backend may not have a second opinion."""
        _ready_model_dir(monkeypatch, tmp_path)
        factory = _Factory()
        backend = faster_whisper_backend.FasterWhisperBackend(model_factory=factory)

        backend.load_model(
            _request(tmp_path, device=device.DEVICE_CUDA,
                     compute_type=device.COMPUTE_FLOAT16)
        )

        assert factory.loads[0]["device"] == device.DEVICE_CUDA
        assert factory.loads[0]["compute_type"] == device.COMPUTE_FLOAT16

    def test_a_second_transcription_reuses_the_loaded_model(self, tmp_path, monkeypatch):
        _ready_model_dir(monkeypatch, tmp_path)
        factory = _Factory(segments=[_Segment(0.0, 4.0, "a")])
        backend = faster_whisper_backend.FasterWhisperBackend(model_factory=factory)

        backend.transcribe(_request(tmp_path))
        backend.transcribe(_request(tmp_path))

        assert len(factory.loads) == 1

    @pytest.mark.parametrize(
        "changed",
        [
            {"device": device.DEVICE_CUDA},
            {"compute_type": device.COMPUTE_FLOAT32},
        ],
        ids=["device", "compute_type"],
    )
    def test_changing_what_was_loaded_reloads_it(self, tmp_path, monkeypatch, changed):
        _ready_model_dir(monkeypatch, tmp_path)
        factory = _Factory(segments=[_Segment(0.0, 4.0, "a")])
        backend = faster_whisper_backend.FasterWhisperBackend(model_factory=factory)

        backend.load_model(_request(tmp_path))
        backend.load_model(_request(tmp_path, **changed))

        assert len(factory.loads) == 2

    def test_a_different_model_reloads_too(self, tmp_path, monkeypatch):
        directories = {
            "tiny": tmp_path / "models" / "tiny",
            "small": tmp_path / "models" / "small",
        }
        for path in directories.values():
            path.mkdir(parents=True)
            (path / "tokenizer.json").write_text("{}", encoding="utf-8")
        monkeypatch.setattr(
            model_store, "ensure_ready",
            lambda root, model_id: str(directories[model_id]),
        )
        factory = _Factory()
        backend = faster_whisper_backend.FasterWhisperBackend(model_factory=factory)

        backend.load_model(_request(tmp_path, model_id="tiny"))
        backend.load_model(_request(tmp_path, model_id="small"))

        assert [load["path"] for load in factory.loads] == [
            str(directories["tiny"]), str(directories["small"])
        ]

    def test_releasing_drops_the_model_so_the_next_run_reloads(
        self, tmp_path, monkeypatch
    ):
        """Part 6 hands the VRAM back; the next transcription must still work."""
        _ready_model_dir(monkeypatch, tmp_path)
        factory = _Factory()
        backend = faster_whisper_backend.FasterWhisperBackend(model_factory=factory)

        backend.load_model(_request(tmp_path))
        backend.release()
        backend.load_model(_request(tmp_path))

        assert len(factory.loads) == 2

    def test_releasing_while_a_load_is_in_flight_still_frees_it(
        self, tmp_path, monkeypatch
    ):
        """The load runs outside the lock, so release() can land mid-load.

        That is not a corner case, it is the case part 6 has: large-v3 takes
        tens of seconds to come up and the user gives up on it. If the load
        that was already running then wrote itself into the cache, the user
        would have been told the memory came back — so they will not ask a
        second time — while several gigabytes of VRAM stayed held for the rest
        of the session.
        """
        _ready_model_dir(monkeypatch, tmp_path)
        loading = threading.Event()
        released = threading.Event()
        loads = []

        def _slow_factory(path, **kwargs):
            loads.append(path)
            if len(loads) == 1:
                loading.set()
                released.wait(5)
            return object()

        backend = faster_whisper_backend.FasterWhisperBackend(
            model_factory=_slow_factory
        )
        worker = threading.Thread(
            target=backend.load_model, args=(_request(tmp_path),), daemon=True
        )
        worker.start()
        assert loading.wait(5), "the load never started"
        backend.release()
        released.set()
        worker.join(5)

        # Observable proof the cache is empty: the next run has to load again.
        backend.load_model(_request(tmp_path))
        assert len(loads) == 2, "the released model was cached by the late load"

    def test_a_model_missing_its_tokenizer_never_reaches_faster_whisper(
        self, tmp_path, monkeypatch
    ):
        """The one file whose absence sends faster-whisper to the network.

        Its loader answers a missing tokenizer.json with
        Tokenizer.from_pretrained("openai/whisper-<size>"), which downloads and
        does not consult local_files_only at all. The model store would have
        caught this first, but "nothing leaves this machine" is too important
        to rest on another module's invariant, so the backend checks too.
        """
        directory = tmp_path / "models" / "tiny"
        directory.mkdir(parents=True)
        monkeypatch.setattr(
            model_store, "ensure_ready", lambda root, model_id: str(directory)
        )
        factory = _Factory()
        backend = faster_whisper_backend.FasterWhisperBackend(model_factory=factory)

        with pytest.raises(errors.TranscriptionError) as caught:
            backend.load_model(_request(tmp_path))

        assert caught.value.code == errors.MODEL_CORRUPTED
        assert factory.loads == [], "faster-whisper was given the chance to download"

    def test_a_model_that_is_not_installed_says_so(self, tmp_path):
        """Through the real store: the cheap check runs before every run."""
        backend = faster_whisper_backend.FasterWhisperBackend(model_factory=_Factory())
        with pytest.raises(errors.TranscriptionError) as caught:
            backend.load_model(_request(tmp_path, model_id="tiny"))
        assert caught.value.code == errors.MODEL_NOT_INSTALLED

    def test_a_load_failure_is_classified_like_any_other_backend_error(
        self, tmp_path, monkeypatch
    ):
        _ready_model_dir(monkeypatch, tmp_path)
        factory = _Factory(load_error=RuntimeError("CUDA failed with error out of memory"))
        backend = faster_whisper_backend.FasterWhisperBackend(model_factory=factory)

        with pytest.raises(errors.TranscriptionError) as caught:
            backend.load_model(_request(tmp_path, device=device.DEVICE_CUDA))
        assert caught.value.code == errors.INSUFFICIENT_VRAM


# ── The error table ──────────────────────────────────────────────────────────


class TestErrorClassification:
    """CTranslate2's messages, as they actually arrive, mapped to our codes.

    Matched by fragment because they are C++ strings that get reworded between
    versions. The important case is the last one: an unrecognised message has
    to become BACKEND_ERROR, never a guess.
    """

    @pytest.mark.parametrize(
        "message, device_name, expected",
        [
            ("CUDA failed with error out of memory", "cuda", errors.INSUFFICIENT_VRAM),
            ("cublas runtime error : CUBLAS_STATUS_ALLOC_FAILED", "cuda",
             errors.INSUFFICIENT_VRAM),
            # NVIDIA documents this one as "out of memory" too, and it names
            # cuDNN — so the missing-library table would have claimed it and
            # sent a user with a full card off to reinstall a working driver.
            ("cuDNN failed: CUDNN_STATUS_ALLOC_FAILED", "cuda",
             errors.INSUFFICIENT_VRAM),
            ("CUDA out of memory. Tried to allocate 2.00 GiB", "cuda",
             errors.INSUFFICIENT_VRAM),
            ("std::bad_alloc", "cpu", errors.INSUFFICIENT_RAM),
            ("Cannot allocate memory", "cpu", errors.INSUFFICIENT_RAM),
            ("Out of memory", "cpu", errors.INSUFFICIENT_RAM),
            ("Out of memory", "cuda", errors.INSUFFICIENT_VRAM),
            ("no CUDA-capable device is detected", "cuda", errors.CUDA_UNAVAILABLE),
            ("CUDA driver version is insufficient for CUDA runtime version",
             "cuda", errors.CUDA_UNAVAILABLE),
            ("Library cudnn_ops_infer64_8.dll is not found", "cuda",
             errors.CUDA_UNAVAILABLE),
            ("Could not load library cublas64_12.dll", "cuda",
             errors.CUDA_UNAVAILABLE),
            # A card this CTranslate2 build has no kernels for — too new, or
            # older than its compiled -gencode list. Without these two the
            # message fell through to BACKEND_ERROR, which carries no offer to
            # re-run on the processor, where it would have worked.
            ("no kernel image is available for execution on the device", "cuda",
             errors.CUDA_UNAVAILABLE),
            ("CUDA error: invalid device function", "cuda",
             errors.CUDA_UNAVAILABLE),
            ("Invalid model configuration", "cpu", errors.BACKEND_ERROR),
            ("something nobody has ever seen", "cuda", errors.BACKEND_ERROR),
        ],
    )
    def test_each_family_reaches_the_code_the_user_can_act_on(
        self, message, device_name, expected
    ):
        error = faster_whisper_backend.classify_backend_error(
            RuntimeError(message), device_name
        )
        assert error.code == expected

    def test_an_allocation_failure_is_memory_even_though_it_names_cublas(self):
        """CUBLAS_STATUS_ALLOC_FAILED is a full card, not a missing cuBLAS.

        The order of the tables is what decides this, and getting it backwards
        sends a user with a working driver off to reinstall it.
        """
        error = faster_whisper_backend.classify_backend_error(
            RuntimeError("CUBLAS_STATUS_ALLOC_FAILED"), "cuda"
        )
        assert error.code == errors.INSUFFICIENT_VRAM

    def test_a_card_without_kernels_becomes_an_offer_to_use_the_processor(self):
        """The point of classifying it at all: the CPU is the answer.

        The retry allowlist is deliberately short, so a GPU fault the CPU could
        cure has to arrive as one of its two codes rather than as the catch-all.
        """
        for message in ("no kernel image is available for execution on the device",
                        "CUDA error: invalid device function"):
            error = faster_whisper_backend.classify_backend_error(
                RuntimeError(message), "cuda"
            )
            assert device.should_retry_on_cpu(error, device.DEVICE_CUDA) is True

    def test_pythons_own_memory_error_is_recognised(self):
        error = faster_whisper_backend.classify_backend_error(MemoryError(), "cpu")
        assert error.code == errors.INSUFFICIENT_RAM

    def test_the_technical_text_is_kept_for_the_log_and_not_for_the_screen(self):
        error = faster_whisper_backend.classify_backend_error(
            RuntimeError("Invalid model configuration"), "cpu"
        )
        assert "Invalid model configuration" in error.log_line
        assert str(error) == errors.BACKEND_ERROR


# ── Transcribing ─────────────────────────────────────────────────────────────


class TestTranscribe:
    def test_the_result_carries_the_text_the_language_and_the_times(
        self, tmp_path, monkeypatch
    ):
        _ready_model_dir(monkeypatch, tmp_path)
        factory = _Factory(
            segments=[_Segment(0.0, 2.0, " primeira "), _Segment(2.0, 4.0, "segunda")],
            info=_Info(language="pt", language_probability=0.87, duration=4.0),
        )
        backend = faster_whisper_backend.FasterWhisperBackend(model_factory=factory)

        result = backend.transcribe(_request(tmp_path))

        assert result.text == "primeira segunda"
        assert result.language == "pt"
        assert result.language_probability == 0.87
        assert result.duration_seconds == 4.0
        assert [(s.start, s.end) for s in result.segments] == [(0.0, 2.0), (2.0, 4.0)]
        assert result.backend == backend_module.BACKEND_FASTER_WHISPER
        assert (result.model_id, result.device, result.compute_type) == (
            "tiny", device.DEVICE_CPU, device.COMPUTE_INT8
        )

    def test_progress_only_moves_forward_and_ends_at_one(self, tmp_path, monkeypatch):
        """A screen reader reads every number it is given, including a
        percentage that went backwards. The last segment can also end past the
        duration we measured, because the model pads the tail."""
        _ready_model_dir(monkeypatch, tmp_path)
        factory = _Factory(
            segments=[_Segment(0.0, 1.0, "a"), _Segment(1.0, 2.0, "b"),
                      _Segment(2.0, 8.0, "c")],
        )
        backend = faster_whisper_backend.FasterWhisperBackend(model_factory=factory)
        seen = []

        backend.transcribe(_request(tmp_path, duration_seconds=4.0), progress=seen.append)

        assert seen == sorted(seen)
        assert all(0.0 <= value <= 1.0 for value in seen)
        assert seen[0] == pytest.approx(0.25)
        assert seen[-1] == 1.0

    def test_an_unknown_duration_still_finishes_at_one(self, tmp_path, monkeypatch):
        _ready_model_dir(monkeypatch, tmp_path)
        factory = _Factory(
            segments=[_Segment(0.0, 1.0, "a")], info=_Info(duration=None)
        )
        backend = faster_whisper_backend.FasterWhisperBackend(model_factory=factory)
        seen = []

        backend.transcribe(_request(tmp_path, duration_seconds=None), progress=seen.append)

        assert seen == [1.0]

    def test_cancelling_stops_consuming_the_generator(self, tmp_path, monkeypatch):
        """Between two segments is the only moment the C++ call is not on the
        stack, so it is the only place a cancellation can be honoured — and
        honouring it has to mean stopping the generator, since that generator
        *is* the decoding."""
        _ready_model_dir(monkeypatch, tmp_path)
        cancelled = []
        factory = _Factory(
            segments=[_Segment(0.0, 1.0, "a"), _Segment(1.0, 2.0, "b"),
                      _Segment(2.0, 3.0, "c")],
            # Flipped when the generator is asked for its second segment, which
            # is the earliest point a run can be cancelled mid-decode.
            on_segment=lambda count: cancelled.append(True),
        )
        backend = faster_whisper_backend.FasterWhisperBackend(model_factory=factory)

        with pytest.raises(errors.TranscriptionError) as caught:
            backend.transcribe(_request(tmp_path), should_cancel=lambda: bool(cancelled))

        assert caught.value.code == errors.CANCELLED
        assert factory.models[0].yielded == 2, "the third segment was still decoded"

    def test_a_cancel_before_the_first_segment_never_loads_anything(
        self, tmp_path, monkeypatch
    ):
        _ready_model_dir(monkeypatch, tmp_path)
        factory = _Factory(segments=[_Segment(0.0, 1.0, "a")])
        backend = faster_whisper_backend.FasterWhisperBackend(model_factory=factory)

        with pytest.raises(errors.TranscriptionError) as caught:
            backend.transcribe(_request(tmp_path), should_cancel=lambda: True)

        assert caught.value.code == errors.CANCELLED
        assert factory.loads == []

    def test_the_voice_activity_filter_is_asked_for(self, tmp_path, monkeypatch):
        """Whisper answers silence with invented sentences, and a voice note is
        mostly silence at the end. A listener cannot tell an invented sentence
        from a real one."""
        _ready_model_dir(monkeypatch, tmp_path)
        factory = _Factory(segments=[_Segment(0.0, 1.0, "a")])
        backend = faster_whisper_backend.FasterWhisperBackend(model_factory=factory)

        result = backend.transcribe(_request(tmp_path))

        assert factory.transcribe_calls[0]["vad_filter"] is True
        assert result.vad_used is True

    def test_a_filter_that_cannot_load_costs_the_filter_not_the_transcription(
        self, tmp_path, monkeypatch
    ):
        """onnxruntime is a separate binary in the frozen build, and the user
        cannot install it into one. Without the filter the transcription is
        worse; without the transcription there is nothing at all."""
        _ready_model_dir(monkeypatch, tmp_path)
        factory = _Factory(
            segments=[_Segment(0.0, 1.0, "ola")],
            vad_error=RuntimeError("Failed to load onnxruntime providers"),
        )
        backend = faster_whisper_backend.FasterWhisperBackend(model_factory=factory)

        result = backend.transcribe(_request(tmp_path))

        assert [call["vad_filter"] for call in factory.transcribe_calls] == [True, False]
        assert result.text == "ola"
        # The downgrade may not be invisible: without the filter, a note ending
        # in silence comes back with an invented sentence on the end, and a
        # listener has no way to tell it from a real one. A warning in the log
        # is not a signal to somebody who cannot see the log.
        assert result.vad_used is False

    def test_a_real_failure_is_not_retried_without_the_filter(
        self, tmp_path, monkeypatch
    ):
        _ready_model_dir(monkeypatch, tmp_path)
        factory = _Factory(transcribe_error=RuntimeError("Invalid model configuration"))
        backend = faster_whisper_backend.FasterWhisperBackend(model_factory=factory)

        with pytest.raises(errors.TranscriptionError) as caught:
            backend.transcribe(_request(tmp_path))

        assert caught.value.code == errors.BACKEND_ERROR
        assert len(factory.transcribe_calls) == 1

    def test_nothing_said_comes_back_as_an_empty_result_not_an_error(
        self, tmp_path, monkeypatch
    ):
        """With the filter on, a note holding only noise yields no segments.

        It is not an error and must not become one — but part 6 has to know,
        because a window with nothing in it is indistinguishable from a crash
        to somebody listening to it.
        """
        _ready_model_dir(monkeypatch, tmp_path)
        backend = faster_whisper_backend.FasterWhisperBackend(
            model_factory=_Factory(segments=[])
        )

        result = backend.transcribe(_request(tmp_path))

        assert result.text == ""
        assert result.is_empty is True
        assert result.segments == ()

    def test_a_result_with_text_is_not_empty(self):
        assert _result("alguma coisa").is_empty is False
        assert _result("   ").is_empty is True

    def test_a_failure_while_decoding_is_classified_too(self, tmp_path, monkeypatch):
        """The generator does the work, so most failures arrive mid-loop."""
        _ready_model_dir(monkeypatch, tmp_path)

        def _explode(count):
            raise RuntimeError("std::bad_alloc")

        factory = _Factory(
            segments=[_Segment(0.0, 1.0, "a"), _Segment(1.0, 2.0, "b")],
            on_segment=_explode,
        )
        backend = faster_whisper_backend.FasterWhisperBackend(model_factory=factory)

        with pytest.raises(errors.TranscriptionError) as caught:
            backend.transcribe(_request(tmp_path))
        assert caught.value.code == errors.INSUFFICIENT_RAM


# ── Preparing the audio ──────────────────────────────────────────────────────


class TestAudioPreparation:
    def test_a_conversion_yields_a_file_and_its_length(self, tmp_path, own_temp_dir):
        """The length is read from the converted file's own header — exact,
        and free next to the ffprobe call it replaces."""
        source = _voice_note(tmp_path)
        ffmpeg = _fake_ffmpeg(tmp_path, seconds=2.0)

        with audio_prep.prepared_audio(ffmpeg, source) as prepared:
            assert os.path.isfile(prepared.path)
            assert prepared.duration_seconds == pytest.approx(2.0, abs=0.01)
        assert _leftovers(own_temp_dir) == []

    def test_the_command_carries_the_target_format(self):
        """Pinned on the command itself: a conversion at the wrong rate is not
        an error anywhere, it is simply a worse transcription."""
        source = inspect.getsource(audio_prep._run_ffmpeg)
        assert '"-ar", str(TARGET_SAMPLE_RATE)' in source
        assert '"-ac", str(TARGET_CHANNELS)' in source
        assert audio_prep.TARGET_SAMPLE_RATE == 16000
        assert audio_prep.TARGET_CHANNELS == 1

    def test_a_missing_file_is_a_download_that_has_not_finished(self, tmp_path):
        ffmpeg = _fake_ffmpeg(tmp_path)
        with pytest.raises(errors.TranscriptionError) as caught:
            audio_prep.prepare_audio(ffmpeg, str(tmp_path / "nothing.wzmedia"))
        assert caught.value.code == errors.MEDIA_NOT_DOWNLOADED

    def test_an_empty_file_is_the_same_answer(self, tmp_path):
        ffmpeg = _fake_ffmpeg(tmp_path)
        source = _voice_note(tmp_path, data=b"")
        with pytest.raises(errors.TranscriptionError) as caught:
            audio_prep.prepare_audio(ffmpeg, source)
        assert caught.value.code == errors.MEDIA_NOT_DOWNLOADED

    def test_a_file_ffmpeg_cannot_parse_is_an_unsupported_format(
        self, tmp_path, own_temp_dir
    ):
        ffmpeg = _fake_ffmpeg(
            tmp_path, returncode=1, seconds=-1,
            stderr="Invalid data found when processing input\n",
        )
        with pytest.raises(errors.TranscriptionError) as caught:
            audio_prep.prepare_audio(ffmpeg, _voice_note(tmp_path))
        assert caught.value.code == errors.UNSUPPORTED_AUDIO_FORMAT
        assert _leftovers(own_temp_dir) == []

    def test_a_truncated_file_is_told_apart_from_an_unreadable_one(
        self, tmp_path, own_temp_dir
    ):
        """A send that died mid-upload prints both kinds of line, which is why
        the truncation markers are checked first."""
        ffmpeg = _fake_ffmpeg(
            tmp_path, returncode=1, seconds=-1,
            stderr="moov atom not found\nInvalid data found when processing input\n",
        )
        with pytest.raises(errors.TranscriptionError) as caught:
            audio_prep.prepare_audio(ffmpeg, _voice_note(tmp_path))
        assert caught.value.code == errors.AUDIO_INCOMPLETE
        assert _leftovers(own_temp_dir) == []

    def test_a_conversion_that_produced_no_samples_is_damaged_audio(
        self, tmp_path, own_temp_dir
    ):
        ffmpeg = _fake_ffmpeg(tmp_path, returncode=0, seconds=0.0)
        with pytest.raises(errors.TranscriptionError) as caught:
            audio_prep.prepare_audio(ffmpeg, _voice_note(tmp_path))
        assert caught.value.code == errors.AUDIO_INCOMPLETE
        assert _leftovers(own_temp_dir) == []

    def test_any_other_failure_stays_generic(self, tmp_path, own_temp_dir):
        """A wrong diagnosis sends a blind user to fix what is not broken."""
        ffmpeg = _fake_ffmpeg(
            tmp_path, returncode=1, seconds=-1, stderr="Error opening output file\n"
        )
        with pytest.raises(errors.TranscriptionError) as caught:
            audio_prep.prepare_audio(ffmpeg, _voice_note(tmp_path))
        assert caught.value.code == errors.FFMPEG_FAILED
        assert _leftovers(own_temp_dir) == []

    def test_a_missing_ffmpeg_is_reported_as_such(self, tmp_path):
        with pytest.raises(errors.TranscriptionError) as caught:
            audio_prep.prepare_audio(
                str(tmp_path / "no-ffmpeg-here.exe"), _voice_note(tmp_path)
            )
        assert caught.value.code == errors.FFMPEG_FAILED

    def test_our_own_file_names_never_reach_the_error_detail(self, tmp_path):
        """ffmpeg echoes the path it read, and that file's name is the message
        id — which the issue forbids the log to carry."""
        source = _voice_note(tmp_path)
        ffmpeg = _fake_ffmpeg(
            tmp_path, returncode=1, seconds=-1,
            stderr=f"Error opening input file {source}.\n",
        )
        with pytest.raises(errors.TranscriptionError) as caught:
            audio_prep.prepare_audio(ffmpeg, source)
        assert os.path.basename(source) not in caught.value.log_line
        assert "<audio>" in caught.value.log_line

    def test_cancelling_kills_the_conversion_and_deletes_the_temporary(
        self, tmp_path, own_temp_dir, monkeypatch
    ):
        """Abandoning the process would keep the media file open for the rest
        of the session, and leave an hour of WAV in %TEMP%.

        The process is inspected through `_kill` rather than through a marker
        the fake would write: the fake is a script behind a launcher, so on
        Windows the thing WinZapp spawns is the launcher and the interpreter is
        its child. Real ffmpeg is a direct child, which is what makes the kill
        below the whole story there.
        """
        ffmpeg = _fake_ffmpeg(tmp_path, sleep=1.5, marker=str(tmp_path / "finished"))
        killed = []
        real_kill = audio_prep._kill

        def _spy(process):
            killed.append(process)
            real_kill(process)

        monkeypatch.setattr(audio_prep, "_kill", _spy)

        # False the first time, so the run gets as far as spawning ffmpeg: the
        # check before that one is the cheap "already cancelled?" guard, and it
        # would answer without there being a process to kill.
        answers = iter([False])

        started = time.monotonic()
        with pytest.raises(errors.TranscriptionError) as caught:
            audio_prep.prepare_audio(
                ffmpeg, _voice_note(tmp_path),
                should_cancel=lambda: next(answers, True),
            )
        elapsed = time.monotonic() - started

        assert caught.value.code == errors.CANCELLED
        assert elapsed < 1.5, "the cancellation waited for the conversion to end"
        assert killed and killed[0].poll() is not None
        assert _leftovers(own_temp_dir) == []

    def test_the_temporary_is_not_named_after_the_message(self, tmp_path, own_temp_dir):
        ffmpeg = _fake_ffmpeg(tmp_path, seconds=1.0)
        source = _voice_note(tmp_path)
        with audio_prep.prepared_audio(ffmpeg, source) as prepared:
            assert os.path.basename(source) not in prepared.path
            assert os.path.splitext(os.path.basename(source))[0] not in prepared.path

    def test_the_temporary_is_gone_once_the_block_ends(self, tmp_path, own_temp_dir):
        ffmpeg = _fake_ffmpeg(tmp_path, seconds=1.0)
        with audio_prep.prepared_audio(ffmpeg, _voice_note(tmp_path)) as prepared:
            path = prepared.path
        assert not os.path.exists(path)
        assert _leftovers(own_temp_dir) == []


# ── The job ──────────────────────────────────────────────────────────────────


def _result(text="tudo certo"):
    return backend_module.TranscriptionResult(
        text=text, language="pt", language_probability=0.9, duration_seconds=1.0,
        segments=(backend_module.TranscriptionSegment(0.0, 1.0, text),),
        backend="fake", model_id="tiny", device="cpu", compute_type="int8",
    )


class _Watcher:
    """Collects what part 6 will turn into announcements."""

    def __init__(self):
        self.phases = []
        self.progress = []
        self.finished = []

    def on_phase(self, phase):
        self.phases.append(phase)

    def on_progress(self, fraction):
        self.progress.append(fraction)

    def on_finished(self, result, error):
        self.finished.append((result, error))


def _run_job(tmp_path, backend, ffmpeg=None, watcher=None, **kwargs):
    watcher = watcher or _Watcher()
    job = job_module.TranscriptionJob(
        audio_path=kwargs.pop("audio_path", _voice_note(tmp_path)),
        ffmpeg=ffmpeg if ffmpeg is not None else _fake_ffmpeg(tmp_path, seconds=1.0),
        models_root=str(tmp_path / "models"),
        model_id="tiny",
        backend=backend,
        on_phase=watcher.on_phase,
        on_progress=watcher.on_progress,
        on_finished=watcher.on_finished,
        probe=kwargs.pop("probe", lambda: device.HardwareProbe(total_ram_mb=16384,
                                                              available_ram_mb=8192)),
        **kwargs,
    )
    job.start()
    job.join(30)
    assert job.phase in job_module.TERMINAL_PHASES, "the job never finished"
    return job, watcher


class TestJob:
    def test_the_phases_arrive_in_the_order_the_user_will_hear_them(
        self, tmp_path, own_temp_dir
    ):
        backend = _FakeBackend(result=_result())
        _job, watcher = _run_job(tmp_path, backend)
        assert watcher.phases == [
            job_module.PHASE_PREPARING_AUDIO,
            job_module.PHASE_LOADING_MODEL,
            job_module.PHASE_TRANSCRIBING,
            job_module.PHASE_DONE,
        ]

    def test_the_result_is_reported_exactly_once(self, tmp_path, own_temp_dir):
        """The UI holds a row and a spoken "transcribing…" until it hears back."""
        expected = _result()
        _job, watcher = _run_job(tmp_path, _FakeBackend(result=expected))
        assert watcher.finished == [(expected, None)]

    def test_the_backend_receives_the_prepared_audio_and_the_decisions(
        self, tmp_path, own_temp_dir
    ):
        backend = _FakeBackend(result=_result())
        job, _watcher = _run_job(tmp_path, backend, language="pt")
        request = backend.requests[0]
        assert request.audio_path.endswith(".wav")
        assert request.duration_seconds == pytest.approx(1.0, abs=0.01)
        assert request.language == "pt"
        assert (request.device, request.compute_type) == (job.device, job.compute_type)

    def test_the_prepared_file_is_deleted_when_the_run_ends(
        self, tmp_path, own_temp_dir
    ):
        backend = _FakeBackend(result=_result())
        _job, _watcher = _run_job(tmp_path, backend)
        assert not os.path.exists(backend.requests[0].audio_path)
        assert _leftovers(own_temp_dir) == []

    def test_the_hardware_is_probed_when_the_decision_is_made(
        self, tmp_path, own_temp_dir
    ):
        """device.py is explicit: a probe taken at startup describes a machine
        that no longer exists by the time a model is loaded, because free
        memory is what is being planned against."""
        probes = []

        def _probe():
            probes.append(time.monotonic())
            return device.HardwareProbe(total_ram_mb=16384, available_ram_mb=8192)

        backend = _FakeBackend(result=_result())
        job, _watcher = _run_job(tmp_path, backend, probe=_probe)
        assert len(probes) == 1
        assert job.device == device.DEVICE_CPU
        assert job.device_reason == device.REASON_NO_CUDA_FOUND
        assert job.compute_type == device.COMPUTE_INT8

    def test_the_device_is_readable_when_the_loading_phase_is_announced(
        self, tmp_path, own_temp_dir
    ):
        """Part 6 says which processor is being used from inside this callback.

        Asserting job.device after join() is not the same assertion: it passed
        happily while the phase was announced *before* the probe ran, leaving
        the attributes None at the only moment anyone reads them — and
        device_reason_i18n_key(None) does not fail, it falls back to "you asked
        for the CPU". A user on a GPU machine would then hear the app name the
        wrong reason, confidently, while the model loads onto CUDA.
        """
        seen = {}

        # Built here rather than through _run_job() because the callback has to
        # be able to see the job while it is still running, not after it ends.
        def _on_phase(phase):
            # setdefault, not assignment: what part 6 reacts to is the FIRST
            # time it hears the phase, so a later, better-populated repeat of
            # the same announcement must not be able to paper over it.
            if phase == job_module.PHASE_LOADING_MODEL:
                seen.setdefault("device", job.device)
                seen.setdefault("reason", job.device_reason)
                seen.setdefault("compute", job.compute_type)

        job = job_module.TranscriptionJob(
            audio_path=_voice_note(tmp_path),
            ffmpeg=_fake_ffmpeg(tmp_path, seconds=1.0),
            models_root=str(tmp_path / "models"),
            model_id="tiny",
            backend=_FakeBackend(result=_result()),
            on_phase=_on_phase,
            probe=lambda: device.HardwareProbe(
                total_ram_mb=16384, available_ram_mb=8192
            ),
        )
        job.start()
        job.join(30)
        assert job.phase == job_module.PHASE_DONE

        assert seen["device"] == device.DEVICE_CPU
        assert seen["reason"] == device.REASON_NO_CUDA_FOUND
        assert seen["compute"] == device.COMPUTE_INT8

    def test_a_failure_becomes_a_failed_phase_and_an_error(
        self, tmp_path, own_temp_dir
    ):
        error = errors.TranscriptionError(errors.BACKEND_ERROR, "something technical")
        _job, watcher = _run_job(tmp_path, _FakeBackend(error=error))
        assert watcher.phases[-1] == job_module.PHASE_FAILED
        assert watcher.finished == [(None, error)]

    def test_an_unexpected_exception_still_reports_something(
        self, tmp_path, own_temp_dir
    ):
        """A worker thread that dies quietly leaves the UI waiting forever."""
        backend = _FakeBackend(error=ValueError("a programming mistake"))
        _job, watcher = _run_job(tmp_path, backend)
        result, error = watcher.finished[0]
        assert result is None
        assert error.code == errors.BACKEND_ERROR
        assert watcher.phases[-1] == job_module.PHASE_FAILED

    def test_a_callback_that_raises_does_not_cost_the_final_report(
        self, tmp_path, own_temp_dir
    ):
        watcher = _Watcher()

        def _bad_phase(phase):
            watcher.phases.append(phase)
            raise RuntimeError("the panel was already destroyed")

        watcher.on_phase = _bad_phase
        _job, watcher = _run_job(tmp_path, _FakeBackend(result=_result()), watcher=watcher)
        assert len(watcher.finished) == 1

    @pytest.mark.parametrize("phase", ["preparing", "loading", "transcribing"])
    def test_cancelling_in_any_phase_ends_cancelled_and_leaves_nothing_behind(
        self, tmp_path, own_temp_dir, phase
    ):
        """Three different waits, one guarantee: no process, no temporary."""
        holder = {}
        ffmpeg = _fake_ffmpeg(tmp_path, seconds=1.0)

        if phase == "preparing":
            ffmpeg = _fake_ffmpeg(tmp_path, name="slow", seconds=1.0, sleep=2.0)
            backend = _FakeBackend(result=_result())
        elif phase == "loading":
            backend = _FakeBackend(
                result=_result(), on_load=lambda request: holder["job"].cancel()
            )
        else:
            backend = _FakeBackend(
                result=_result(), on_transcribe=lambda request: holder["job"].cancel()
            )

        watcher = _Watcher()
        job = job_module.TranscriptionJob(
            audio_path=_voice_note(tmp_path),
            ffmpeg=ffmpeg,
            models_root=str(tmp_path / "models"),
            model_id="tiny",
            backend=backend,
            on_phase=watcher.on_phase,
            on_finished=watcher.on_finished,
            probe=lambda: device.HardwareProbe(total_ram_mb=16384, available_ram_mb=8192),
        )
        holder["job"] = job
        job.start()
        if phase == "preparing":
            # From the caller's thread, while ffmpeg is still sleeping: that is
            # the only way to reach the branch that has to kill a process.
            job.cancel()
        job.join(30)

        assert watcher.phases[-1] == job_module.PHASE_CANCELLED
        result, error = watcher.finished[0]
        assert result is None and error.code == errors.CANCELLED
        assert _leftovers(own_temp_dir) == []

    def test_a_prepared_file_is_reused_and_never_deleted(
        self, tmp_path, own_temp_dir
    ):
        """Part 4 retries a failed GPU run on the CPU with the same audio.

        Converting it again is the whole wait a second time — 40 minutes of
        audio is 40 minutes of ffmpeg — and the file belongs to whoever passed
        it, so this job neither creates nor removes it.
        """
        already = tmp_path / "already-converted.wav"
        already.write_bytes(b"RIFF")
        prepared = audio_prep.PreparedAudio(path=str(already), duration_seconds=12.0)
        backend = _FakeBackend(result=_result())
        watcher = _Watcher()

        job = job_module.TranscriptionJob(
            audio_path=_voice_note(tmp_path),
            # Would raise FFMPEG_FAILED if the conversion were attempted, which
            # is the point: nothing may run ffmpeg on this path.
            ffmpeg=str(tmp_path / "must-not-run.exe"),
            models_root=str(tmp_path / "models"),
            model_id="tiny",
            backend=backend,
            prepared=prepared,
            on_phase=watcher.on_phase,
            on_finished=watcher.on_finished,
            probe=lambda: device.HardwareProbe(total_ram_mb=16384, available_ram_mb=8192),
        )
        job.start()
        job.join(30)

        assert watcher.phases == [
            job_module.PHASE_LOADING_MODEL,
            job_module.PHASE_TRANSCRIBING,
            job_module.PHASE_DONE,
        ]
        assert backend.requests[0].audio_path == str(already)
        assert backend.requests[0].duration_seconds == 12.0
        assert already.exists(), "the job deleted a file it does not own"

    def test_a_gpu_failure_worth_redoing_hands_the_audio_over(
        self, tmp_path, own_temp_dir
    ):
        """The converted audio outlives the run that failed on the GPU.

        Re-converting is the whole wait a second time — 40 minutes of audio is
        40 minutes of ffmpeg — for a file that is already correct. So the job
        hands it to the caller instead of deleting it, and from that point the
        caller is the one who deletes it.
        """
        backend = _FakeBackend(
            error=errors.TranscriptionError(errors.INSUFFICIENT_VRAM, "alloc failed")
        )
        job, watcher = _run_job(
            tmp_path, backend,
            probe=lambda: device.HardwareProbe(
                cuda_available=True, cuda_device_count=1, compute_capability=(8, 6),
                free_vram_mb=8192, total_vram_mb=8192,
            ),
        )

        assert job.device == device.DEVICE_CUDA
        assert watcher.finished[0][1].code == errors.INSUFFICIENT_VRAM
        assert job.prepared_handover is not None
        assert os.path.isfile(job.prepared_handover.path)
        assert job.prepared_handover.duration_seconds > 0
        # Still in the temp directory, on purpose: it is a leak only until the
        # caller discards it, which is the other half of the contract.
        assert _leftovers(own_temp_dir) != []
        audio_prep.discard(job.prepared_handover)
        assert _leftovers(own_temp_dir) == []

    def test_the_handed_over_file_transcribes_without_ffmpeg_running_again(
        self, tmp_path, own_temp_dir
    ):
        """The point of the handover: the CPU run reuses the same audio."""
        failing = _FakeBackend(
            error=errors.TranscriptionError(errors.CUDA_UNAVAILABLE, "no cublas")
        )
        gpu_job, _watcher = _run_job(
            tmp_path, failing,
            probe=lambda: device.HardwareProbe(
                cuda_available=True, cuda_device_count=1, compute_capability=(8, 6),
                free_vram_mb=8192, total_vram_mb=8192,
            ),
        )
        prepared = gpu_job.prepared_handover
        assert prepared is not None

        second = _FakeBackend(result=_result())
        cpu_job, cpu_watcher = _run_job(
            tmp_path, second,
            # Would raise FFMPEG_FAILED if the conversion were attempted, which
            # is exactly what must not happen a second time.
            ffmpeg=str(tmp_path / "must-not-run.exe"),
            prepared=prepared,
            probe=lambda: device.HardwareProbe(total_ram_mb=16384,
                                               available_ram_mb=8192),
        )

        assert cpu_watcher.finished[0][1] is None
        assert cpu_job.device == device.DEVICE_CPU
        assert second.requests[0].audio_path == prepared.path
        # The second job did not take ownership either — it never created the
        # file — so the caller is still the one holding it.
        assert cpu_job.prepared_handover is None
        assert os.path.isfile(prepared.path)
        audio_prep.discard(prepared)
        assert _leftovers(own_temp_dir) == []

    @pytest.mark.parametrize("code", [
        errors.CANCELLED,
        errors.MODEL_NOT_INSTALLED,
        errors.MODEL_CORRUPTED,
        errors.BACKEND_ERROR,
    ])
    def test_a_failure_the_cpu_cannot_cure_deletes_the_audio_as_before(
        self, tmp_path, own_temp_dir, code
    ):
        # The path that hands nothing over is the path that used to exist, and
        # it must keep cleaning up after itself.
        backend = _FakeBackend(error=errors.TranscriptionError(code, "detail"))
        job, _watcher = _run_job(
            tmp_path, backend,
            probe=lambda: device.HardwareProbe(
                cuda_available=True, cuda_device_count=1, compute_capability=(8, 6),
                free_vram_mb=8192, total_vram_mb=8192,
            ),
        )
        assert job.prepared_handover is None
        assert _leftovers(own_temp_dir) == []

    def test_a_handover_with_nobody_to_hand_it_to_is_not_made(
        self, tmp_path, own_temp_dir
    ):
        """A job with no finished callback has no caller to become the owner.

        The transfer presumes a reader. Without one, nothing would ever learn
        the file exists, and a WAV of the whole recording would sit in %TEMP%
        unowned for good — a leak that grows by one recording per failure.
        """
        backend = _FakeBackend(
            error=errors.TranscriptionError(errors.INSUFFICIENT_VRAM, "alloc failed")
        )
        job = job_module.TranscriptionJob(
            audio_path=_voice_note(tmp_path),
            ffmpeg=_fake_ffmpeg(tmp_path, seconds=1.0),
            models_root=str(tmp_path / "models"),
            model_id="tiny",
            backend=backend,
            on_finished=None,
            probe=lambda: device.HardwareProbe(
                cuda_available=True, cuda_device_count=1, compute_capability=(8, 6),
                free_vram_mb=8192, total_vram_mb=8192,
            ),
        )
        job.start()
        job.join(30)

        assert job.phase == job_module.PHASE_FAILED
        assert job.device == device.DEVICE_CUDA
        assert job.prepared_handover is None
        assert _leftovers(own_temp_dir) == []

    def test_the_re_run_needs_the_preference_and_not_just_the_audio(
        self, tmp_path, own_temp_dir
    ):
        """Handing the audio in does not imply the processor — the flag does.

        After INSUFFICIENT_VRAM the card is still counted and its libraries
        still load, so a re-run built without PREFERENCE_CPU re-decides its way
        straight back to CUDA and fails identically, having paid for the model
        load a second time. The probe here says CUDA precisely so that only the
        preference can be what moves it.
        """
        prepared = audio_prep.PreparedAudio(
            path=str(tmp_path / "already-converted.wav"), duration_seconds=12.0
        )
        (tmp_path / "already-converted.wav").write_bytes(b"RIFF")
        cuda_probe = lambda: device.HardwareProbe(  # noqa: E731 - one line, one use
            cuda_available=True, cuda_device_count=1, compute_capability=(8, 6),
            free_vram_mb=8192, total_vram_mb=8192,
        )

        went_back, _watcher = _run_job(
            tmp_path, _FakeBackend(result=_result()),
            ffmpeg=str(tmp_path / "must-not-run.exe"),
            prepared=prepared, probe=cuda_probe,
        )
        assert went_back.device == device.DEVICE_CUDA, (
            "the audio alone was enough to move the run off the GPU"
        )

        forced, _watcher = _run_job(
            tmp_path, _FakeBackend(result=_result()),
            ffmpeg=str(tmp_path / "must-not-run.exe"),
            prepared=prepared, probe=cuda_probe,
            device_preference=device.PREFERENCE_CPU,
        )
        assert forced.device == device.DEVICE_CPU
        # Part 6 announces this one: a forced re-run says "you asked for the
        # processor", which this user did not — that is the announcement to
        # suppress on the retry path.
        assert forced.device_reason == device.REASON_CPU_REQUESTED

    def test_a_gpu_failure_on_a_run_that_was_on_the_cpu_hands_nothing_over(
        self, tmp_path, own_temp_dir
    ):
        # Same error code, CPU run: there is nothing to fall back to, so
        # keeping the file would leak it for an offer nobody can make.
        backend = _FakeBackend(
            error=errors.TranscriptionError(errors.INSUFFICIENT_VRAM, "alloc failed")
        )
        job, _watcher = _run_job(tmp_path, backend)
        assert job.device == device.DEVICE_CPU
        assert job.prepared_handover is None
        assert _leftovers(own_temp_dir) == []

    def test_a_successful_run_hands_nothing_over(self, tmp_path, own_temp_dir):
        job, watcher = _run_job(tmp_path, _FakeBackend(result=_result()))
        assert watcher.finished[0][1] is None
        assert job.prepared_handover is None
        assert _leftovers(own_temp_dir) == []

    def test_the_handover_is_readable_from_the_finished_callback(
        self, tmp_path, own_temp_dir
    ):
        """Part 6 reads it from there, so it has to be set before the report.

        The callback is where the offer to re-run is decided, and the finished
        report is the only thing the UI is waiting on — a handover published
        after it would be invisible.
        """
        seen = {}
        backend = _FakeBackend(
            error=errors.TranscriptionError(errors.INSUFFICIENT_VRAM, "alloc failed")
        )
        holder = {}

        def _on_finished(result, error):
            seen["handover"] = holder["job"].prepared_handover
            seen["offer"] = device.cpu_retry_i18n_key(error, holder["job"].device)

        job = job_module.TranscriptionJob(
            audio_path=_voice_note(tmp_path),
            ffmpeg=_fake_ffmpeg(tmp_path, seconds=1.0),
            models_root=str(tmp_path / "models"),
            model_id="tiny",
            backend=backend,
            on_finished=_on_finished,
            probe=lambda: device.HardwareProbe(
                cuda_available=True, cuda_device_count=1, compute_capability=(8, 6),
                free_vram_mb=8192, total_vram_mb=8192,
            ),
        )
        holder["job"] = job
        job.start()
        job.join(30)

        assert seen["handover"] is not None
        assert seen["offer"] == "transcription_retry_on_cpu_vram"
        audio_prep.discard(seen["handover"])
        assert _leftovers(own_temp_dir) == []

    def test_the_configured_backend_is_resolved_when_none_is_handed_in(
        self, tmp_path, own_temp_dir, monkeypatch
    ):
        backend = _FakeBackend(result=_result())
        monkeypatch.setitem(backend_module._instances, "fake", backend)
        monkeypatch.setattr(backend_module, "BACKEND_IDS", ("fake",))
        _job, watcher = _run_job(tmp_path, None, backend_id="fake")
        assert watcher.finished[0][1] is None

    def test_no_usable_backend_fails_the_job_rather_than_the_thread(
        self, tmp_path, own_temp_dir, monkeypatch
    ):
        monkeypatch.setitem(
            backend_module._instances, "fake", _FakeBackend(available=False)
        )
        monkeypatch.setattr(backend_module, "BACKEND_IDS", ("fake",))
        _job, watcher = _run_job(tmp_path, None, backend_id="fake")
        assert watcher.finished[0][1].code == errors.BACKEND_MISSING
        assert _leftovers(own_temp_dir) == []


# ── Privacy of the log ───────────────────────────────────────────────────────


# What the issue forbids the log to carry, as identifiers a logging argument
# might name. `text`/`segments` are the transcription itself; the paths are the
# audio file, whose name is the WhatsApp message id and therefore identifies
# the message and the conversation behind it.
_FORBIDDEN_IN_LOG = (
    r"\btext\b", r"\.text\b", r"\bsegments\b", r"\baudio_path\b",
    r"\bsource_path\b", r"\bpath\b", r"\bjid\b", r"\bmessage_id\b",
    r"\bcontact\b", r"\bphone\b",
)

# Part 3's own modules. Restricted to them deliberately: the model store logs
# the models directory the user chose, which is neither a message nor a
# conversation, and rewriting its rules is not this part's business.
#
# management.py joins them: it logs the outcome of every action the settings
# tab runs, and nothing it logs may ever reach past the model id. So does part
# 6b's message_run.py, the one module here that holds a message record — its
# stricter scan (and the UI modules') is in test_transcription_message_run.py.
_PART3_MODULES = (
    "audio_prep.py", "backend.py", "faster_whisper_backend.py", "job.py",
    "management.py", "message_run.py",
)


def _logging_arguments(path):
    """Every expression handed to a `logging.*` call in one module."""
    with open(path, "r", encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        target = node.func
        if not (isinstance(target, ast.Attribute)
                and isinstance(target.value, ast.Name)
                and target.value.id == "logging"):
            continue
        for argument in list(node.args) + [kw.value for kw in node.keywords]:
            found.append((target.attr, ast.unparse(argument)))
    return found


class TestLogPrivacy:
    """The issue's own requirement, enforced rather than remembered.

    A transcription is the content of a private conversation. The log may say
    how the machine did the work — backend, model, device, compute type,
    languages, durations, the technical text of a failure — and nothing about
    what was said or who said it. That includes the audio path: WinZapp names
    media files after the message id.
    """

    def test_no_logging_call_can_be_handed_the_transcribed_text(self):
        package = os.path.dirname(job_module.__file__)
        offenders = []
        for name in _PART3_MODULES:
            for level, expression in _logging_arguments(os.path.join(package, name)):
                for pattern in _FORBIDDEN_IN_LOG:
                    if re.search(pattern, expression):
                        offenders.append(f"{name}: logging.{level}({expression})")
        assert offenders == [], f"the log could carry private content: {offenders}"

    def test_a_whole_run_never_writes_what_was_said(
        self, tmp_path, own_temp_dir, caplog
    ):
        """Belt and braces: the static check cannot see through a helper."""
        spoken = "zzqq um segredo que ninguem deveria ver no log yyww"
        source = _voice_note(tmp_path, name="ABCDEF0123456789.wzmedia")
        caplog.set_level(logging.DEBUG)

        backend = _FakeBackend(result=_result(spoken))
        _run_job(tmp_path, backend, audio_path=source)

        for word in ("zzqq", "segredo", "yyww"):
            assert word not in caplog.text
        assert os.path.basename(source) not in caplog.text
        assert source not in caplog.text

    def test_a_failing_conversion_never_writes_the_file_name_either(
        self, tmp_path, own_temp_dir, caplog
    ):
        source = _voice_note(tmp_path, name="FEDCBA9876543210.wzmedia")
        ffmpeg = _fake_ffmpeg(
            tmp_path, returncode=1, seconds=-1,
            stderr=(
                f"Input #0, ogg, from '{source}':\n"
                f"{source}: Invalid data found when processing input\n"
            ),
        )
        caplog.set_level(logging.DEBUG)

        _job, watcher = _run_job(
            tmp_path, _FakeBackend(result=_result()), ffmpeg=ffmpeg, audio_path=source
        )

        assert watcher.finished[0][1].code == errors.UNSUPPORTED_AUDIO_FORMAT
        assert os.path.basename(source) not in caplog.text
        assert source not in caplog.text


def test_nothing_here_reaches_for_the_hugging_face_cache():
    """The weights come from the folder the model store filled, and only there.

    A frozen WinZapp has no HF_HOME, and even where one exists it is not what
    the user chose in the settings or what the store verified. Anything reading
    it would work on the developer's machine and download, silently, on the
    user's.
    """
    package = os.path.dirname(job_module.__file__)
    offenders = []
    for name in _PART3_MODULES:
        with open(os.path.join(package, name), "r", encoding="utf-8") as handle:
            source = handle.read()
        for token in ("HF_HOME", "HUGGINGFACE", "TRANSFORMERS_CACHE", "download_root"):
            if token in source:
                offenders.append(f"{name}: {token}")
    assert offenders == []


def test_the_build_ships_what_a_local_transcription_needs():
    """A frozen WinZapp has no pip: the backend is either in the build or gone.

    faster-whisper reaches ctranslate2, av, tokenizers and huggingface_hub
    through imports PyInstaller's static analysis does not follow, and the
    voice-activity filter's own model is a data file inside the package —
    `--collect-all` is what brings each package's binaries and data along with
    its modules.

    onnxruntime is collected the same way even though a hidden import would be
    enough on paper (contrib ships a hook for its provider DLLs): if it fails to
    import in a frozen build only, the filter falls back silently and Whisper
    starts inventing sentences in a voice note's trailing silence, which the
    person this feature is for cannot detect. See the comment in build.py.
    """
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "build.py"), "r", encoding="utf-8") as handle:
        source = handle.read()
    collect_all = source.split("collect_all = [", 1)[1].split("]", 1)[0]
    for package in ("faster_whisper", "ctranslate2", "av", "tokenizers",
                    "huggingface_hub", "truststore"):
        assert f'"{package}"' in collect_all, f"{package} is not collected by build.py"
    assert '"--collect-all", "onnxruntime"' in source


def test_the_requirements_pin_the_backend():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "requirements.txt"), "r", encoding="utf-8") as handle:
        requirements = handle.read()
    for line in ("faster-whisper==", "ctranslate2==", "av==", "onnxruntime==",
                 "tokenizers==", "truststore=="):
        assert line in requirements
