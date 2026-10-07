"""The startup sweep of plaintext a crashed transcription left in %TEMP%."""

import logging
import os

from core.transcription import _fileops, audio_prep, job as job_module, temp_sweep


def _make(root, name, age_seconds, directory=False):
    path = root / name
    if directory:
        path.mkdir()
        (path / "out.json").write_text("secret")
    else:
        path.write_text("secret")
    stamp = 1_000_000.0 - age_seconds
    os.utime(path, (stamp, stamp))
    return path


class TestSweep:
    def test_stale_files_and_folders_of_this_package_go(self, tmp_path):
        old_file = _make(tmp_path, "winzapp-audio-abc.ogg", 7200)
        old_wav = _make(tmp_path, "winzapp-transcribe-abc.wav", 7200)
        old_dir = _make(tmp_path, "winzapp-whisper-abc", 7200, directory=True)

        removed = temp_sweep.sweep_stale_temporaries(str(tmp_path), now=1_000_000.0)

        assert removed == 3
        assert not old_file.exists() and not old_wav.exists() and not old_dir.exists()

    def test_a_recent_temporary_may_belong_to_a_running_account(self, tmp_path):
        fresh = _make(tmp_path, "winzapp-audio-abc.ogg", 60)
        assert temp_sweep.sweep_stale_temporaries(str(tmp_path), now=1_000_000.0) == 0
        assert fresh.exists()

    def test_what_is_not_ours_is_left_alone_however_old(self, tmp_path):
        other = _make(tmp_path, "somebody-elses.tmp", 99999)
        assert temp_sweep.sweep_stale_temporaries(str(tmp_path), now=1_000_000.0) == 0
        assert other.exists()

    def test_a_missing_folder_is_not_an_error(self, tmp_path):
        assert temp_sweep.sweep_stale_temporaries(str(tmp_path / "nope")) == 0

    def test_a_temporary_that_will_not_go_is_logged_without_its_path(
        self, tmp_path, monkeypatch, caplog
    ):
        _make(tmp_path, "winzapp-audio-abc.ogg", 7200)

        def _refuse(path):
            raise PermissionError(13, "denied", path)

        monkeypatch.setattr(temp_sweep.os, "remove", _refuse)
        caplog.set_level(logging.DEBUG)
        assert temp_sweep.sweep_stale_temporaries(str(tmp_path), now=1_000_000.0) == 0
        assert "PermissionError" in caplog.text
        assert str(tmp_path) not in caplog.text


class TestCleanupFailuresAreLogged:
    def test_a_workdir_that_cannot_be_removed_warns_with_the_basename(
        self, monkeypatch, tmp_path, caplog
    ):
        monkeypatch.setattr(_fileops.tempfile, "tempdir", str(tmp_path))

        def _refuse(path, onexc=None):
            onexc(os.rmdir, path, PermissionError(13, "denied"))

        monkeypatch.setattr(_fileops.shutil, "rmtree", _refuse)
        caplog.set_level(logging.DEBUG)
        with _fileops.private_temp_dir("winzapp-whisper-") as workdir:
            name = os.path.basename(workdir)
        assert name in caplog.text
        assert str(tmp_path) not in caplog.text

    def test_a_removable_workdir_is_removed_quietly(self, monkeypatch, tmp_path, caplog):
        monkeypatch.setattr(_fileops.tempfile, "tempdir", str(tmp_path))
        caplog.set_level(logging.DEBUG)
        with _fileops.private_temp_dir("winzapp-whisper-") as workdir:
            open(os.path.join(workdir, "x"), "w").close()
        assert not os.path.exists(workdir)
        assert "could not remove" not in caplog.text


class TestHandoverOfAFileNobodyTook:
    def test_a_finished_callback_that_raises_discards_the_handed_over_wav(
        self, tmp_path, monkeypatch
    ):
        wav = tmp_path / "prepared.wav"
        wav.write_text("audio")
        seen = []

        def _boom(result, error):
            raise RuntimeError("callback bug")

        job = job_module.TranscriptionJob.__new__(job_module.TranscriptionJob)
        job._on_finished = _boom
        job._on_phase = None
        job._backend_id = None
        job._model_id = "m"
        job._language = None
        job.device = job.compute_type = None
        job.phase = None
        job.prepared_handover = audio_prep.PreparedAudio(path=str(wav), duration_seconds=1.0)
        monkeypatch.setattr(audio_prep, "discard", lambda p: (seen.append(p), wav.unlink()))

        job._finish(None, job_module.errors.TranscriptionError(job_module.errors.BACKEND_ERROR, "x"), 0.0)

        assert seen and not wav.exists()
        assert job.prepared_handover is None
