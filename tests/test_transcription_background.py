"""Transcription downloads that follow "download in the background".

The decision (`core.transcription.background`) and the window's half
(`main_window.transcription_background`), against a stub MainWindow: no window
is created, and the job is a fake that finishes when the test says so.
"""

import types

import pytest

from core.transcription import background, management, management_text
from main_window.transcription_background import TranscriptionBackgroundMixin


DOWNLOADS = (
    management.ACTION_DOWNLOAD_MODEL,
    management.ACTION_REPAIR_MODEL,
    management.ACTION_INSTALL_CUDA_RUNTIME,
    management.ACTION_REPAIR_CUDA_RUNTIME,
    management.ACTION_INSTALL_WHISPER_CPP,
    management.ACTION_REPAIR_WHISPER_CPP,
)


class TestWhichActionsRunInTheBackground:
    @pytest.mark.parametrize("action", DOWNLOADS)
    def test_every_download_follows_the_setting(self, action):
        assert background.runs_in_background(action, True)
        assert not background.runs_in_background(action, False)

    @pytest.mark.parametrize(
        "action", [a for a in management.ACTIONS if a not in DOWNLOADS]
    )
    def test_what_is_not_a_download_keeps_its_dialog(self, action):
        assert not background.runs_in_background(action, True)

    def test_the_six_are_exactly_what_fetches_bytes(self):
        assert set(background.BACKGROUND_ACTIONS) == set(DOWNLOADS)


class _FakeJob:
    action = management.ACTION_DOWNLOAD_MODEL
    model_id = "small"
    build_id = None

    def __init__(self, on_finished):
        self.on_finished = on_finished
        self.started = False

    def start(self):
        self.started = True

    def announcement(self, result, error):
        return management.announcement(
            self.action, result, error, model_id=self.model_id
        )


class _I18n:
    def t(self, key):
        return key


class _Window(TranscriptionBackgroundMixin):
    def __init__(self):
        self.i18n = _I18n()
        self.said = []
        self.error_sound = types.SimpleNamespace(
            play=lambda: self.said.append("<error sound>")
        )
        self.jobs = []

    def output(self, text, interrupt=False):
        self.said.append(text)

    def make_job(self, on_progress, on_finished):
        assert on_progress is None
        job = _FakeJob(on_finished)
        self.jobs.append(job)
        return job


@pytest.fixture
def window(monkeypatch):
    from main_window import transcription_background as module

    # The hop to the wx thread, run inline.
    monkeypatch.setattr(module.wx, "CallAfter", lambda func, *a: func(*a))
    return _Window()


class TestTheWindowRunsTheJob:
    def test_it_starts_the_job_and_says_so(self, window):
        assert window.start_transcription_background_job(window.make_job, "Baixando")
        assert window.jobs[0].started
        assert window.transcription_background_running()
        assert window.said == ["Baixando transcription_background_started"]

    def test_a_second_one_is_refused_while_the_first_runs(self, window):
        window.start_transcription_background_job(window.make_job, "um")
        assert not window.start_transcription_background_job(window.make_job, "dois")
        assert len(window.jobs) == 1

    def test_without_a_dialog_the_window_says_how_it_ended(self, window):
        window.start_transcription_background_job(window.make_job, "x")
        window.said.clear()
        window.jobs[0].on_finished(object(), None)
        assert not window.transcription_background_running()
        assert window.said == [
            management_text.announcement_text(
                window.i18n, window.jobs[0].announcement(object(), None)
            )
        ]

    def test_a_failure_plays_the_error_sound_first(self, window):
        from core.transcription import errors

        window.start_transcription_background_job(window.make_job, "x")
        window.said.clear()
        window.jobs[0].on_finished(None, errors.TranscriptionError(errors.NO_DISK_SPACE, "d"))
        assert window.said[0] == "<error sound>"
        assert len(window.said) == 2

    def test_a_listener_that_said_it_leaves_the_window_silent(self, window):
        window.start_transcription_background_job(window.make_job, "x")
        window.said.clear()
        calls = []
        window.watch_transcription_background(
            lambda job, result, error: calls.append(job) or True
        )
        window.jobs[0].on_finished("ok", None)
        assert calls == [window.jobs[0]]
        assert window.said == []

    def test_a_listener_that_did_not_say_it_does_not_silence_the_window(self, window):
        window.start_transcription_background_job(window.make_job, "x")
        window.said.clear()
        window.watch_transcription_background(lambda job, result, error: False)
        window.jobs[0].on_finished("ok", None)
        assert len(window.said) == 1

    def test_a_listener_that_raises_costs_nothing(self, window):
        window.start_transcription_background_job(window.make_job, "x")
        window.said.clear()

        def broken(job, result, error):
            raise RuntimeError("dialog gone")

        window.watch_transcription_background(broken)
        window.jobs[0].on_finished("ok", None)
        assert len(window.said) == 1
        assert not window.transcription_background_running()

    def test_listeners_are_forgotten_once_called(self, window):
        window.start_transcription_background_job(window.make_job, "x")
        calls = []
        window.watch_transcription_background(
            lambda *args: calls.append(1) or True
        )
        window.jobs[0].on_finished("ok", None)
        window.start_transcription_background_job(window.make_job, "y")
        window.jobs[1].on_finished("ok", None)
        assert calls == [1]

    def test_a_job_that_cannot_be_built_is_not_left_running(self, window):
        def broken(on_progress, on_finished):
            raise OSError("no thread")

        assert not window.start_transcription_background_job(broken, "x")
        assert not window.transcription_background_running()
        assert window.said == []
