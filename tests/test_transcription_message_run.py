"""The run between "transcribe this message" and the job — and what it must
never leave behind.

`core.transcription.message_run.MessageTranscription` is where a message
becomes a job: settings resolved against a fresh measurement, media fetched,
the encrypted file decrypted into a temporary. Four bug families live here,
and each is silent when it happens:

* **A decrypted recording left in %TEMP%.** The temporary is the audio of a
  private conversation in clear. Success, a failed job, a cancellation at any
  point, a job that never starts and an exception nobody expected must all
  delete it — `TestTheDecryptedTemporaryGoesEveryWayOut` walks every exit.

* **Work done for a run that was always going to fail.** No backend, no model
  that fits, a model that is not downloaded: those answers belong in front of
  the user before a two-minute download or a 2 GB decryption, not after.

* **A cancel that is lost.** It can land before the job exists, between "job
  built" and "job started", or during a blocking call; each has to stop the run
  at the next point that can honour it, and none may start a new wait.

* **A private detail in log.log.** The message id names the media file, so the
  log may carry neither — nor the text of an unexpected exception, which for
  anything touching a file quotes the path.

Everything runs against a fake job with TranscriptionJob's constructor and
callbacks; the job itself has its own suite (test_transcription_backend.py).
"""

import ast
import logging
import os
import re

import pytest

from core.transcription import device, errors, job as job_module, management, message_run
from core.transcription.backend import TranscriptionResult

_LEAKY_ID = "3EB0C0FFEE5EC2E7AB12"

WAV = b"RIFF\x24\x08\x00\x00WAVEfmt " + bytes(64)

RESULT = TranscriptionResult(
    text="zzqq um segredo que ninguem deveria ver yyww",
    language="pt", language_probability=0.98, duration_seconds=4.0,
)


@pytest.fixture
def own_temp_dir(tmp_path, monkeypatch):
    import tempfile

    private = tmp_path / "temp"
    private.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(private))
    return private


def _leftovers(temp_dir):
    return sorted(p.name for p in temp_dir.glob("winzapp-audio-*"))


def _msg(msg_id=_LEAKY_ID, msg_type="audioMessage"):
    return {
        "key": {"id": msg_id, "remoteJid": "5511999990000@s.whatsapp.net", "fromMe": False},
        "messageType": msg_type,
        "message": {msg_type: {"mimetype": "audio/ogg; codecs=opus"}},
    }


class _Watcher:
    """Everything the run reported, in order, and on which thread."""

    def __init__(self):
        self.phases = []
        self.ticks = []
        self.finished = []

    def phase(self, phase):
        self.phases.append(phase)

    def progress(self, tick):
        self.ticks.append(tick)

    def done(self, result, error):
        self.finished.append((result, error))


def _succeed(job):
    job.on_phase(job_module.PHASE_PREPARING_AUDIO)
    job.device, job.device_reason = device.DEVICE_CPU, device.REASON_NO_CUDA_FOUND
    job.on_phase(job_module.PHASE_LOADING_MODEL)
    job.on_phase(job_module.PHASE_TRANSCRIBING)
    job.on_progress(0.5)
    job.on_progress(1.0)
    job.on_phase(job_module.PHASE_DONE)
    job.on_finished(RESULT, None)


def _fail_on_gpu_with_handover(job):
    job.on_phase(job_module.PHASE_PREPARING_AUDIO)
    job.device, job.device_reason = device.DEVICE_CUDA, device.REASON_CUDA_SELECTED
    job.on_phase(job_module.PHASE_LOADING_MODEL)
    job.prepared_handover = job.handover_to_give
    job.on_phase(job_module.PHASE_FAILED)
    job.on_finished(None, errors.TranscriptionError(errors.INSUFFICIENT_VRAM, "out of vram"))


class _FakeJob:
    """TranscriptionJob's constructor and callbacks, running a script instead.

    Runs synchronously inside `start()`: the run under test waits on `join()`
    right after, so the ordering is the real one without a second thread.
    Honours a cancel that arrived before `start()` the way the real job does —
    its first action is a cancel check.
    """

    def __init__(self, script, record):
        self._script = script
        self._record = record

    def __call__(self, audio_path, ffmpeg, models_root, model_id, language=None,
                 device_preference=device.PREFERENCE_AUTO, backend_id=None,
                 prepared=None, on_phase=None, on_progress=None, on_finished=None):
        job = _Job(self._script, audio_path, ffmpeg, models_root, model_id, language,
                   device_preference, backend_id, prepared, on_phase, on_progress,
                   on_finished)
        self._record.append(job)
        return job


class _Job:
    def __init__(self, script, audio_path, ffmpeg, models_root, model_id, language,
                 device_preference, backend_id, prepared, on_phase, on_progress,
                 on_finished):
        self._script = script
        self.audio_path = audio_path
        self.ffmpeg = ffmpeg
        self.models_root = models_root
        self.model_id = model_id
        self.language = language
        self.device_preference = device_preference
        self.backend_id = backend_id
        self.prepared = prepared
        self.on_phase = on_phase
        self.on_progress = on_progress
        self.on_finished = on_finished
        self.device = None
        self.device_reason = None
        self.prepared_handover = None
        self.handover_to_give = None
        self.started = False
        self.cancel_requests = []
        self.audio_existed_while_running = None

    def cancel(self):
        self.cancel_requests.append("before start" if not self.started else "while running")

    def start(self):
        self.started = True
        self.audio_existed_while_running = (
            os.path.isfile(self.audio_path) if self.audio_path else None
        )
        if self.cancel_requests:
            self.on_phase(job_module.PHASE_CANCELLED)
            self.on_finished(None, errors.TranscriptionError(errors.CANCELLED, "cancelled"))
            return
        self._script(self)

    def join(self, timeout=None):
        return None


def _build(tmp_path, fernet_key, fernet, script=_succeed, cached=True, online=True,
           fetch=None, settings=None, installed=("small",), backends=("faster_whisper",),
           decrypt=None, msg=None, on_make=None):
    voice = tmp_path / "voice_messages"
    media = tmp_path / "media"
    voice.mkdir(exist_ok=True)
    media.mkdir(exist_ok=True)
    msg = msg or _msg()
    if cached:
        (voice / f"{_LEAKY_ID}.msv").write_bytes(fernet.encrypt(WAV))
    watcher = _Watcher()
    jobs = []
    factory = _FakeJob(script, jobs)
    probes = []

    def _probe():
        probes.append("probed")
        return device.HardwareProbe(total_ram_mb=16000, available_ram_mb=8000)

    def _make(*args, **kwargs):
        job = factory(*args, **kwargs)
        if on_make is not None:
            on_make(job)
        return job

    run = message_run.MessageTranscription(
        msg,
        settings if settings is not None else {"transcription": {"model": "small"}},
        fernet_key,
        str(voice),
        str(media),
        stored_models_dir=str(tmp_path / "models"),
        ui_language="pt-BR",
        find_ffmpeg=lambda: "ffmpeg.exe",
        is_online=lambda: online,
        fetch_media=fetch,
        on_phase=watcher.phase,
        on_progress=watcher.progress,
        on_finished=watcher.done,
        probe=_probe,
        list_installed=lambda root: installed,
        available_backends=lambda: backends,
        make_job=_make,
        decrypt=decrypt,
        clock=lambda: 0.0,
    )
    run.probes = probes
    run.jobs = jobs
    run.media_file = str(voice / f"{_LEAKY_ID}.msv")
    return run, watcher


def _go(run):
    run.start()
    run.join(10)
    return run


class TestExactlyOneReport:
    def test_a_successful_run_reports_its_result_once(self, tmp_path, own_temp_dir, fernet_key, fernet):
        run, watcher = _build(tmp_path, fernet_key, fernet)
        _go(run)
        assert watcher.finished == [(RESULT, None)]
        assert run.job is run.jobs[0]

    def test_a_job_that_never_reports_is_a_failure_not_an_empty_success(
        self, tmp_path, own_temp_dir, fernet_key, fernet
    ):
        run, watcher = _build(tmp_path, fernet_key, fernet, script=lambda job: None)
        _go(run)
        [(result, error)] = watcher.finished
        assert result is None
        assert error.code == errors.BACKEND_ERROR


class TestDecidingBeforeStarting:
    """The answers the user acts on in Settings come before any slow work."""

    def test_the_probe_is_taken_by_the_run_not_before_it(self, tmp_path, own_temp_dir, fernet_key, fernet):
        run, _watcher = _build(tmp_path, fernet_key, fernet)
        assert run.probes == []
        _go(run)
        assert run.probes == ["probed"]

    def test_the_models_are_listed_from_the_folder_in_force(self, tmp_path, own_temp_dir, fernet_key, fernet):
        seen = []
        run, _watcher = _build(tmp_path, fernet_key, fernet)
        run._list_installed = lambda root: seen.append(root) or ("small",)
        _go(run)
        assert seen == [str(tmp_path / "models")]
        assert run.jobs[0].models_root == str(tmp_path / "models")

    def test_the_resolution_reaches_the_job(self, tmp_path, own_temp_dir, fernet_key, fernet):
        settings = {"transcription": {"model": "small", "device": "cpu",
                                      "auto_detect_language": False, "language": "pl"}}
        run, _watcher = _build(tmp_path, fernet_key, fernet, settings=settings)
        _go(run)
        job = run.jobs[0]
        assert (job.model_id, job.device_preference, job.language, job.backend_id) == (
            "small", device.PREFERENCE_CPU, "pl", "faster_whisper")
        assert job.ffmpeg == "ffmpeg.exe"

    @pytest.mark.parametrize(
        "kwargs, code",
        [
            ({"backends": ()}, errors.BACKEND_MISSING),
            # Automatic, memory unknown, nothing installed: no model at all.
            ({"settings": {}, "installed": ()}, errors.MODEL_NOT_INSTALLED),
            # A model that is known and chosen, and not downloaded.
            ({"installed": ("tiny",)}, errors.MODEL_NOT_INSTALLED),
        ],
        ids=["no-backend", "no-model", "model-not-downloaded"],
    )
    def test_a_run_that_cannot_start_touches_neither_the_media_nor_the_disk(
        self, kwargs, code, tmp_path, own_temp_dir, fernet_key, fernet
    ):
        fetched = []
        run, watcher = _build(tmp_path, fernet_key, fernet, cached=False,
                              fetch=lambda msg, path: fetched.append(path), **kwargs)
        _go(run)
        [(_result, error)] = watcher.finished
        assert error.code == code
        assert fetched == []
        assert run.jobs == []
        assert _leftovers(own_temp_dir) == []
        assert message_run.PHASE_DOWNLOADING_MEDIA not in watcher.phases

    def test_no_model_keeps_the_reason_for_the_sentence(self, tmp_path, own_temp_dir, fernet_key, fernet):
        run, _watcher = _build(tmp_path, fernet_key, fernet, settings={}, installed=())
        run._probe = lambda: device.HardwareProbe()
        _go(run)
        assert run.resolution.model_id is None
        assert run.resolution.model_none_reason is not None


class TestTheMedia:
    def test_media_already_on_disk_is_not_downloaded(self, tmp_path, own_temp_dir, fernet_key, fernet):
        fetched = []
        run, watcher = _build(tmp_path, fernet_key, fernet,
                              fetch=lambda msg, path: fetched.append(path))
        _go(run)
        assert fetched == []
        assert run.media_status == message_run.MEDIA_PRESENT
        assert message_run.PHASE_DOWNLOADING_MEDIA not in watcher.phases

    def test_offline_is_told_apart_and_nothing_is_attempted(self, tmp_path, own_temp_dir, fernet_key, fernet):
        fetched = []
        run, watcher = _build(tmp_path, fernet_key, fernet, cached=False, online=False,
                              fetch=lambda msg, path: fetched.append(path))
        _go(run)
        assert watcher.finished[0][1].code == errors.MEDIA_NOT_DOWNLOADED
        assert run.media_status == message_run.MEDIA_OFFLINE
        assert fetched == []

    def test_a_download_that_produces_nothing_is_a_failed_download(
        self, tmp_path, own_temp_dir, fernet_key, fernet
    ):
        run, watcher = _build(tmp_path, fernet_key, fernet, cached=False,
                              fetch=lambda msg, path: False)
        _go(run)
        assert watcher.finished[0][1].code == errors.MEDIA_NOT_DOWNLOADED
        assert run.media_status == message_run.MEDIA_FAILED
        assert run.jobs == []

    def test_a_download_that_raises_is_a_failed_download(self, tmp_path, own_temp_dir, fernet_key, fernet):
        def _boom(msg, path):
            raise RuntimeError(f"cannot reach {path}")

        run, watcher = _build(tmp_path, fernet_key, fernet, cached=False, fetch=_boom)
        _go(run)
        assert run.media_status == message_run.MEDIA_FAILED
        assert len(watcher.finished) == 1

    def test_a_download_is_announced_before_it_starts_and_then_used(
        self, tmp_path, own_temp_dir, fernet_key, fernet
    ):
        order = []

        def _fetch(msg, path):
            order.append(("fetch", list(watcher.phases)))
            with open(path, "wb") as handle:
                handle.write(fernet.encrypt(WAV))
            return True

        run, watcher = _build(tmp_path, fernet_key, fernet, cached=False, fetch=_fetch)
        _go(run)
        assert order == [("fetch", [message_run.PHASE_DOWNLOADING_MEDIA])]
        assert run.media_status == message_run.MEDIA_PRESENT
        assert watcher.finished == [(RESULT, None)]


class TestTheDecryptedTemporaryGoesEveryWayOut:
    """A private recording in clear. Every exit from the run deletes it."""

    def test_the_job_is_handed_a_real_decrypted_file(self, tmp_path, own_temp_dir, fernet_key, fernet):
        run, _watcher = _build(tmp_path, fernet_key, fernet)
        _go(run)
        job = run.jobs[0]
        assert job.audio_existed_while_running is True
        assert os.path.basename(job.audio_path).startswith("winzapp-audio-")
        assert _LEAKY_ID not in job.audio_path

    def test_after_success(self, tmp_path, own_temp_dir, fernet_key, fernet):
        run, _watcher = _build(tmp_path, fernet_key, fernet)
        _go(run)
        assert _leftovers(own_temp_dir) == []

    def test_after_the_job_fails(self, tmp_path, own_temp_dir, fernet_key, fernet):
        def _fail(job):
            job.on_finished(None, errors.TranscriptionError(errors.FFMPEG_FAILED, "x"))

        run, watcher = _build(tmp_path, fernet_key, fernet, script=_fail)
        _go(run)
        assert watcher.finished[0][1].code == errors.FFMPEG_FAILED
        assert _leftovers(own_temp_dir) == []

    def test_after_a_cancel_during_the_job(self, tmp_path, own_temp_dir, fernet_key, fernet):
        def _cancelled_midway(job):
            job.on_phase(job_module.PHASE_TRANSCRIBING)
            run.cancel()
            assert job.cancel_requests == ["while running"]
            job.on_finished(None, errors.TranscriptionError(errors.CANCELLED, "x"))

        run, watcher = _build(tmp_path, fernet_key, fernet, script=_cancelled_midway)
        _go(run)
        assert watcher.finished[0][1].code == errors.CANCELLED
        assert _leftovers(own_temp_dir) == []

    def test_after_a_cancel_during_the_decryption(self, tmp_path, own_temp_dir, fernet_key, fernet):
        """Decrypting is one blocking call: the cancel is honoured as it
        returns, before any job is built — and the file it wrote goes."""
        from core.utils import decrypt_bytes

        def _decrypt(data, key):
            run.cancel()
            return decrypt_bytes(data, key)

        run, watcher = _build(tmp_path, fernet_key, fernet, decrypt=_decrypt)
        _go(run)
        assert watcher.finished[0][1].code == errors.CANCELLED
        assert run.jobs == []
        assert _leftovers(own_temp_dir) == []

    def test_after_a_cancel_during_the_download(self, tmp_path, own_temp_dir, fernet_key, fernet):
        """The download cannot be interrupted; nothing after it may start."""
        def _fetch(msg, path):
            run.cancel()
            with open(path, "wb") as handle:
                handle.write(fernet.encrypt(WAV))
            return True

        run, watcher = _build(tmp_path, fernet_key, fernet, cached=False, fetch=_fetch)
        _go(run)
        assert watcher.finished[0][1].code == errors.CANCELLED
        assert job_module.PHASE_PREPARING_AUDIO not in watcher.phases
        assert _leftovers(own_temp_dir) == []

    def test_after_an_exception_nobody_expected(self, tmp_path, own_temp_dir, fernet_key, fernet):
        def _explode(*args, **kwargs):
            raise RuntimeError("the factory broke")

        run, watcher = _build(tmp_path, fernet_key, fernet)
        run._make_job = _explode
        _go(run)
        [(_result, error)] = watcher.finished
        assert error.code == errors.BACKEND_ERROR
        assert _leftovers(own_temp_dir) == []

    def test_after_a_job_that_cannot_start(self, tmp_path, own_temp_dir, fernet_key, fernet):
        def _no_thread(job):
            raise RuntimeError("can't start new thread")

        run, watcher = _build(tmp_path, fernet_key, fernet, script=_no_thread)
        _go(run)
        assert watcher.finished[0][1].code == errors.BACKEND_ERROR
        assert _leftovers(own_temp_dir) == []


class TestCancellation:
    def test_a_cancel_before_start_does_nothing_at_all(self, tmp_path, own_temp_dir, fernet_key, fernet):
        run, watcher = _build(tmp_path, fernet_key, fernet)
        run.cancel()
        _go(run)
        assert watcher.finished[0][1].code == errors.CANCELLED
        assert run.probes == []
        assert run.jobs == []

    def test_a_cancel_during_the_probe_is_a_cancel_even_offline(
        self, tmp_path, own_temp_dir, fernet_key, fernet
    ):
        """The probe is the longest part of deciding. Offline, with the audio
        not downloaded, the next thing after it is the media check — and a
        cancel not looked at in between comes back as "wait for the
        connection" to someone who pressed Cancel."""
        fetched = []
        run, watcher = _build(tmp_path, fernet_key, fernet, cached=False, online=False,
                              fetch=lambda msg, path: fetched.append(path))

        def _probe_then_cancel():
            run.cancel()
            return device.HardwareProbe(total_ram_mb=16000, available_ram_mb=8000)

        run._probe = _probe_then_cancel
        _go(run)
        assert watcher.finished[0][1].code == errors.CANCELLED
        assert run.media_status is None
        assert fetched == []
        assert run.jobs == []

    def test_a_cancel_between_building_the_job_and_starting_it_reaches_it(
        self, tmp_path, own_temp_dir, fernet_key, fernet
    ):
        """The window the lock closes: cancel() read `self.job` as None, and
        the job built a moment later would otherwise run to the end."""
        run, watcher = _build(tmp_path, fernet_key, fernet, on_make=lambda job: run.cancel())
        _go(run)
        assert run.jobs[0].cancel_requests == ["before start"]
        assert watcher.finished[0][1].code == errors.CANCELLED


class TestWhatIsAnnounced:
    def test_the_phases_in_order_without_repeats_or_endings(self, tmp_path, own_temp_dir, fernet_key, fernet):
        run, watcher = _build(tmp_path, fernet_key, fernet)
        _go(run)
        # PREPARING_AUDIO once — this run said it for the decryption and the
        # job says it again for the conversion — and no terminal phase: the
        # finished report speaks for those.
        assert watcher.phases == [
            job_module.PHASE_PREPARING_AUDIO,
            job_module.PHASE_LOADING_MODEL,
            job_module.PHASE_TRANSCRIBING,
        ]

    def test_the_device_is_readable_from_inside_the_loading_phase(
        self, tmp_path, own_temp_dir, fernet_key, fernet
    ):
        seen = {}
        run, watcher = _build(tmp_path, fernet_key, fernet)
        run._on_phase = lambda phase: seen.setdefault(phase, (run.device, run.device_reason))
        _go(run)
        assert seen[job_module.PHASE_PREPARING_AUDIO] == (None, None)
        assert seen[job_module.PHASE_LOADING_MODEL] == (device.DEVICE_CPU, device.REASON_NO_CUDA_FOUND)

    def test_waits_without_a_fraction_move_the_bar_anyway(self, tmp_path, own_temp_dir, fernet_key, fernet):
        run, watcher = _build(tmp_path, fernet_key, fernet)
        _go(run)
        pulses = [t for t in watcher.ticks if t.percent is None]
        assert pulses and all(t.update_bar and not t.speak for t in pulses)

    def test_the_fraction_becomes_the_ticks_the_dialog_reads(self, tmp_path, own_temp_dir, fernet_key, fernet):
        run, watcher = _build(tmp_path, fernet_key, fernet)
        _go(run)
        measured = [t for t in watcher.ticks if t.percent is not None]
        assert [t.percent for t in measured] == [50, 100]
        assert all(isinstance(t, management.ProgressTick) for t in measured)
        # 100 is the finished report's to say, never a spoken percentage.
        assert not measured[-1].speak

    def test_nonsense_fractions_are_dropped(self, tmp_path, own_temp_dir, fernet_key, fernet):
        def _odd(job):
            job.on_progress(float("nan"))
            job.on_progress("half")
            job.on_progress(7.0)
            job.on_finished(RESULT, None)

        run, watcher = _build(tmp_path, fernet_key, fernet, script=_odd)
        _go(run)
        assert [t.percent for t in watcher.ticks if t.percent is not None] == [100]


class TestTheProcessorReRun:
    def _first(self, tmp_path, fernet_key, fernet, own_temp_dir):
        handover = own_temp_dir / "converted.wav"
        handover.write_bytes(b"RIFF")

        def _script(job):
            job.handover_to_give = handover
            _fail_on_gpu_with_handover(job)

        run, watcher = _build(tmp_path, fernet_key, fernet, script=_script)
        _go(run)
        return run, watcher, handover

    def test_the_first_run_passes_the_handover_on_untouched(self, tmp_path, own_temp_dir, fernet_key, fernet):
        run, watcher, handover = self._first(tmp_path, fernet_key, fernet, own_temp_dir)
        assert watcher.finished[0][1].code == errors.INSUFFICIENT_VRAM
        assert run.prepared_handover is handover
        assert run.device == device.DEVICE_CUDA
        assert handover.exists()

    def test_the_re_run_uses_the_converted_audio_on_the_processor(
        self, tmp_path, own_temp_dir, fernet_key, fernet
    ):
        first, _watcher, handover = self._first(tmp_path, fernet_key, fernet, own_temp_dir)
        jobs = []
        watcher = _Watcher()
        retry = message_run.MessageTranscription.retry_on_cpu(
            first, on_phase=watcher.phase, on_progress=watcher.progress,
            on_finished=watcher.done, make_job=_FakeJob(_succeed, jobs),
        )
        probes_before = list(first.probes)
        _go(retry)
        [job] = jobs
        assert job.prepared is handover
        assert job.device_preference == device.PREFERENCE_CPU
        assert job.audio_path is None
        assert job.model_id == first.model_id
        assert first.probes == probes_before
        assert watcher.finished == [(RESULT, None)]
        # Not the re-run's to delete: whoever made the offer owns the file.
        assert handover.exists()


class TestTheLogCarriesNothingPrivate:
    def test_a_whole_run_and_its_failures_never_write_the_message(
        self, tmp_path, own_temp_dir, fernet_key, fernet, caplog
    ):
        caplog.set_level(logging.DEBUG)
        runs = []
        run, _watcher = _build(tmp_path, fernet_key, fernet)
        runs.append(run)
        run, _watcher = _build(tmp_path, fernet_key, fernet, cached=False,
                               fetch=lambda msg, path: (_ for _ in ()).throw(
                                   OSError(5, f"cannot write {path}")))
        runs.append(run)
        run, _watcher = _build(tmp_path, fernet_key, fernet)

        def _explode(*args, **kwargs):
            raise RuntimeError(f"broken while handling {run.media_file}")

        run._make_job = _explode
        runs.append(run)
        settings = {"transcription": {"model": "retired-model"}}
        run, _watcher = _build(tmp_path, fernet_key, fernet, settings=settings)
        runs.append(run)
        for each in runs:
            _go(each)
        text = caplog.text
        for secret in (_LEAKY_ID, str(tmp_path), RESULT.text):
            assert secret not in text

    def test_no_logging_call_is_handed_anything_that_names_the_message(self):
        offenders = _scan(message_run.__file__)
        assert offenders == [], offenders


# What a logging argument may not mention in the new modules: the message and
# its id, the paths (named after the id), the transcribed text, the contact.
_FORBIDDEN_IN_LOG = (
    r"_path\b", r"\bpath\b", r"_id\b", r"\bid\b", r"\bmsg\b", r"\btext\b",
    r"\bresult\b", r"\bname\b", r"\bjid\b", r"\bcontact\b", r"\bphone\b",
    r"\bdestination\b", r"\bexc\b(?!\))", r"str\(",
)


def _scan(path):
    with open(path, "r", encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        target = node.func
        if not (isinstance(target, ast.Attribute)
                and isinstance(target.value, ast.Name)
                and target.value.id in ("logging", "log")):
            continue
        for argument in list(node.args) + [kw.value for kw in node.keywords]:
            if isinstance(argument, ast.Constant):
                # A literal format string names nothing at run time; the
                # values handed in beside it are what the scan is about.
                continue
            expression = ast.unparse(argument)
            # The allowed readings of an exception: its class, its numbers,
            # its log_line (code + a detail built without paths) and its
            # frames — never its text.
            cleaned = re.sub(r"type\(exc\)\.__name__|exc\.(errno|log_line)"
                             r"|getattr\(exc, 'winerror', None\)"
                             r"|traceback\.format_tb\(exc\.__traceback__\)", "", expression)
            for pattern in _FORBIDDEN_IN_LOG:
                if re.search(pattern, cleaned):
                    offenders.append(f"{os.path.basename(path)}: {target.attr}({expression})")
    return offenders


def test_nothing_here_imports_wx():
    with open(message_run.__file__, "r", encoding="utf-8") as handle:
        source = handle.read()
    assert re.search(r"^\s*(import wx|from wx)", source, re.M) is None
