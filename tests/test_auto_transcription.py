"""MainWindow's glue for automatic transcription of received voice messages."""

import time

import pytest

from core.transcription import auto_queue, errors, message_run
from core.transcription import stored as stored_transcription
from core.transcription.backend import TranscriptionResult
from main_window import auto_transcription
from main_window.auto_transcription import AutoTranscriptionMixin

KEY = stored_transcription.TRANSCRIPTION_KEY
JID = "5511999999999@s.whatsapp.net"


def _msg(msg_id="M1", **extra):
    msg = {
        "key": {"id": msg_id, "fromMe": False, "remoteJid": JID},
        "messageType": "audioMessage",
        "messageTimestamp": time.time(),
        "message": {"audioMessage": {"seconds": 8}},
    }
    msg.update(extra)
    return msg


def _result(text="olá"):
    return TranscriptionResult(
        text=text, language="pt", language_probability=0.9, duration_seconds=8.0,
        segments=(), vad_used=True, backend="faster_whisper", model_id="base",
        device="cpu", compute_type="int8",
    )


class _Queue:
    def __init__(self):
        self.submitted = []
        self.paused = []

    def submit(self, key, item):
        self.submitted.append((key, item))
        return auto_queue.ACCEPTED

    def pause(self):
        self.paused.append(True)
        return len(self.paused) == 1


class _I18n:
    language = "pt-BR"

    @staticmethod
    def t(key):
        return key


class _Window(AutoTranscriptionMixin):
    def __init__(self, enabled=True, locked=False):
        self.settings = {"transcription": {"auto_transcribe_received": enabled}}
        self.key = b"k"
        self.i18n = _I18n()
        self._locked = locked
        self.queue = _Queue()
        self._auto_transcription_queue = self.queue
        self.records = []
        self.stored = []
        self.spoken = []
        self.repainted = []
        self.conversations_panel = None
        self._wa_connected = True

    def is_chat_locked(self, jid):
        return self._locked

    def is_chat_hidden_by_vault(self, jid):
        return self._locked

    def _transcription_copies(self, jid, msg_id):
        return self.records

    def store_message_transcription(self, jid, msg_id, value):
        self.stored.append((jid, msg_id, value))
        return stored_transcription.SAVE_STORED

    def output(self, text, interrupt=False):
        self.spoken.append(text)

    @staticmethod
    def _normalize_jid(jid):
        return jid

    def _find_api_ffmpeg(self):
        return ""

    def handle_audio_message(self, msg):
        return False


@pytest.fixture(autouse=True)
def _inline_call_after(monkeypatch):
    monkeypatch.setattr(auto_transcription.wx, "CallAfter", lambda fn, *a, **k: fn(*a, **k))


class TestQueueing:
    def test_a_fresh_incoming_voice_message_is_queued(self):
        window = _Window()
        msg = _msg()
        window._maybe_auto_transcribe(JID, msg)
        assert window.queue.submitted == [((JID, "M1"), (JID, "M1", msg))]

    def test_nothing_happens_while_the_setting_is_off(self):
        window = _Window(enabled=False)
        window._maybe_auto_transcribe(JID, _msg())
        assert window.queue.submitted == []

    def test_a_locked_chat_is_skipped(self):
        window = _Window(locked=True)
        window._maybe_auto_transcribe(JID, _msg())
        assert window.queue.submitted == []

    def test_an_ineligible_message_is_skipped(self):
        window = _Window()
        window._maybe_auto_transcribe(JID, _msg(messageType="imageMessage"))
        own = _msg()
        own["key"]["fromMe"] = True
        window._maybe_auto_transcribe(JID, own)
        assert window.queue.submitted == []

    def test_a_lock_check_that_raises_never_breaks_the_arrival(self):
        window = _Window()

        def boom(jid):
            raise RuntimeError("vault")

        window.is_chat_locked = boom
        window._maybe_auto_transcribe(JID, _msg())
        assert window.queue.submitted == []


class _FakeRun:
    """Stands in for MessageTranscription: finishes at start()."""

    outcome = (None, None)
    built = []

    def __init__(self, msg, settings, key, voice_dir, media_dir, **kwargs):
        self.kwargs = kwargs
        _FakeRun.built.append(self)

    def start(self):
        result, error = _FakeRun.outcome
        self.kwargs["on_finished"](result, error)

    def join(self):
        pass


@pytest.fixture
def fake_run(monkeypatch):
    _FakeRun.built = []
    monkeypatch.setattr(message_run, "MessageTranscription", _FakeRun)
    monkeypatch.setattr(auto_transcription, "external_models",
                        type("E", (), {"read_references": staticmethod(lambda a: ((), True))}))
    monkeypatch.setattr(auto_transcription.transcription_preferences, "stored_models_dir",
                        lambda app: "")
    monkeypatch.setattr(auto_transcription, "data_path", lambda *parts: "/".join(parts))
    return _FakeRun


class TestTheRun:
    def test_a_result_is_stored_as_an_inline_transcription(self, fake_run):
        window = _Window()
        msg = _msg()
        window.records = [msg]
        fake_run.outcome = (_result("bom dia"), None)

        window._run_auto_transcription((JID, "M1", msg))

        [(jid, msg_id, value)] = window.stored
        assert (jid, msg_id) == (JID, "M1")
        assert value["text"] == "bom dia"
        assert stored_transcription.is_inline(value)
        assert window.spoken == []

    def test_it_runs_with_the_arrivals_own_hooks(self, fake_run):
        window = _Window()
        msg = _msg()
        window.records = [msg]
        fake_run.outcome = (_result(), None)
        window._run_auto_transcription((JID, "M1", msg))
        kwargs = fake_run.built[0].kwargs
        assert kwargs["fetch_media"] == window._auto_fetch_media
        assert kwargs["is_online"]() is True
        assert "on_phase" not in kwargs and "on_progress" not in kwargs

    def test_an_empty_result_is_not_stored(self, fake_run):
        window = _Window()
        msg = _msg()
        window.records = [msg]
        fake_run.outcome = (_result(""), None)
        window._run_auto_transcription((JID, "M1", msg))
        assert window.stored == []

    def test_a_message_the_user_already_decided_about_is_left_alone(self, fake_run):
        window = _Window()
        msg = _msg(**{KEY: stored_transcription.tombstone(5.0)})
        window.records = [msg]
        fake_run.outcome = (_result(), None)
        window._run_auto_transcription((JID, "M1", msg))
        assert window.stored == []

    def test_a_message_that_left_memory_is_not_stored(self, fake_run):
        window = _Window()
        fake_run.outcome = (_result(), None)
        window._run_auto_transcription((JID, "M1", _msg()))
        assert window.stored == []

    def test_turning_the_setting_off_meanwhile_stops_it(self, fake_run):
        window = _Window(enabled=False)
        window._run_auto_transcription((JID, "M1", _msg()))
        assert fake_run.built == []

    def test_a_setup_fault_pauses_and_says_so_once(self, fake_run):
        window = _Window()
        fake_run.outcome = (None, errors.TranscriptionError(errors.MODEL_NOT_INSTALLED, "x"))
        window._run_auto_transcription((JID, "M1", _msg("M1")))
        window._run_auto_transcription((JID, "M2", _msg("M2")))
        assert window.spoken == ["transcription_auto_paused"]
        assert len(window.queue.paused) == 2

    def test_a_fault_of_one_message_says_nothing(self, fake_run):
        window = _Window()
        fake_run.outcome = (None, errors.TranscriptionError(errors.FFMPEG_FAILED, "x"))
        window._run_auto_transcription((JID, "M1", _msg()))
        assert window.spoken == [] and window.queue.paused == []


class TestRepainting:
    def test_the_open_conversation_repaints_the_row(self):
        window = _Window()
        msg = _msg()
        window.records = [msg]
        repainted = []

        class _Panel:
            conversation = {"remoteJid": JID}

            def _repaint_or_repopulate(self, ids):
                repainted.append(ids)

        window.conversations_panel = _Panel()
        window._store_auto_transcription(JID, "M1", _result())
        assert repainted == [["M1"]]

    def test_another_conversation_is_not_touched(self):
        window = _Window()
        window.records = [_msg()]
        repainted = []

        class _Panel:
            conversation = {"remoteJid": "other@g.us"}

            def _repaint_or_repopulate(self, ids):
                repainted.append(ids)

        window.conversations_panel = _Panel()
        window._store_auto_transcription(JID, "M1", _result())
        assert repainted == []
        assert len(window.stored) == 1


class TestFetchingTheAudio:
    def test_without_auto_download_it_fetches_itself(self, monkeypatch, tmp_path):
        window = _Window()
        path = tmp_path / "M1.msv"
        calls = []
        monkeypatch.setattr(auto_transcription, "auto_download_enabled", lambda s: False)
        window.handle_audio_message = lambda msg: calls.append(msg) or path.write_bytes(b"x")
        assert window._auto_fetch_media(_msg(), str(path)) is True
        assert len(calls) == 1

    def test_with_auto_download_it_waits_for_the_file_already_coming(self, monkeypatch, tmp_path):
        window = _Window()
        path = tmp_path / "M1.msv"
        monkeypatch.setattr(auto_transcription, "auto_download_enabled", lambda s: True)
        monkeypatch.setattr(auto_transcription, "auto_download_allows", lambda s, m: True)
        monkeypatch.setattr(auto_transcription, "_DOWNLOAD_POLL_SECONDS", 0)
        polls = []

        def sleep(seconds):
            polls.append(seconds)
            if len(polls) == 2:
                path.write_bytes(b"x")

        monkeypatch.setattr(auto_transcription.time, "sleep", sleep)
        window.handle_audio_message = lambda msg: pytest.fail("a second download was started")
        assert window._auto_fetch_media(_msg(), str(path)) is True

    def test_a_download_that_never_arrives_falls_back_to_fetching(self, monkeypatch, tmp_path):
        window = _Window()
        path = tmp_path / "M1.msv"
        calls = []
        monkeypatch.setattr(auto_transcription, "auto_download_enabled", lambda s: True)
        monkeypatch.setattr(auto_transcription, "auto_download_allows", lambda s, m: True)
        monkeypatch.setattr(auto_transcription, "_DOWNLOAD_WAIT_SECONDS", 0)
        window.handle_audio_message = lambda msg: calls.append(msg)
        assert window._auto_fetch_media(_msg(), str(path)) is False
        assert len(calls) == 1
