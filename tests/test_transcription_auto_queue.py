"""Automatic transcription of received voice messages: who qualifies, in what order."""

import pytest

from core.transcription import auto_queue, errors, preferences
from core.transcription.stored import TRANSCRIPTION_KEY

NOW = 1_000_000.0


def _msg(**overrides):
    msg = {
        "key": {"id": "M1", "fromMe": False, "remoteJid": "a@s.whatsapp.net"},
        "messageType": "audioMessage",
        "messageTimestamp": NOW - 5,
        "message": {"audioMessage": {"seconds": 12, "ptt": True}},
    }
    msg.update(overrides)
    return msg


class TestTheSetting:
    def test_it_is_off_by_default_and_spelled_the_same_in_both_modules(self):
        assert preferences.DEFAULTS[preferences.SETTING_AUTO_TRANSCRIBE_RECEIVED] is False
        assert auto_queue.SETTING_AUTO_TRANSCRIBE == preferences.SETTING_AUTO_TRANSCRIBE_RECEIVED

    def test_only_a_real_true_turns_it_on(self):
        assert auto_queue.enabled({"transcription": {"auto_transcribe_received": True}})
        for stored in (False, None, "yes", 1, []):
            assert not auto_queue.enabled({"transcription": {"auto_transcribe_received": stored}})
        assert not auto_queue.enabled({})
        assert not auto_queue.enabled(None)
        assert not auto_queue.enabled({"transcription": "junk"})


class TestWhoQualifies:
    def test_a_fresh_incoming_voice_message_does(self):
        assert auto_queue.eligible(_msg(), now=NOW)

    def test_our_own_message_does_not(self):
        msg = _msg()
        msg["key"]["fromMe"] = True
        assert not auto_queue.eligible(msg, now=NOW)

    @pytest.mark.parametrize("message_type", ["conversation", "imageMessage", "videoMessage",
                                              "documentMessage", "stickerMessage"])
    def test_only_audio_does(self, message_type):
        assert not auto_queue.eligible(_msg(messageType=message_type), now=NOW)

    def test_a_message_without_an_id_does_not(self):
        msg = _msg()
        msg["key"]["id"] = ""
        assert not auto_queue.eligible(msg, now=NOW)

    def test_one_that_already_has_a_decision_does_not(self):
        assert not auto_queue.eligible(_msg(**{TRANSCRIPTION_KEY: {"deleted": True, "at": 1.0}}), now=NOW)
        assert not auto_queue.eligible(_msg(**{TRANSCRIPTION_KEY: {"text": "oi", "at": 1.0}}), now=NOW)

    def test_a_backlog_message_does_not(self):
        old = _msg(messageTimestamp=NOW - auto_queue.MAX_AGE_SECONDS - 1)
        assert not auto_queue.eligible(old, now=NOW)
        edge = _msg(messageTimestamp=NOW - auto_queue.MAX_AGE_SECONDS + 1)
        assert auto_queue.eligible(edge, now=NOW)

    def test_a_message_with_no_timestamp_just_arrived(self):
        msg = _msg()
        del msg["messageTimestamp"]
        assert auto_queue.eligible(msg, now=NOW)

    def test_a_very_long_recording_does_not(self):
        long = _msg(message={"audioMessage": {"seconds": auto_queue.MAX_AUDIO_SECONDS + 1}})
        assert not auto_queue.eligible(long, now=NOW)
        ok = _msg(message={"audioMessage": {"seconds": auto_queue.MAX_AUDIO_SECONDS}})
        assert auto_queue.eligible(ok, now=NOW)

    def test_an_unstated_or_odd_length_is_not_a_reason_to_skip(self):
        for seconds in (None, "", "abc", -3):
            msg = _msg(message={"audioMessage": {"seconds": seconds}})
            assert auto_queue.eligible(msg, now=NOW)

    @pytest.mark.parametrize("junk", [None, "x", 3, []])
    def test_junk_is_not_eligible(self, junk):
        assert not auto_queue.eligible(junk, now=NOW)


class TestWhichFailuresPauseTheQueue:
    @pytest.mark.parametrize("code", sorted(auto_queue.SETUP_FAILURES))
    def test_a_setup_fault_pauses(self, code):
        assert auto_queue.pauses_the_queue(errors.TranscriptionError(code, "x"))

    @pytest.mark.parametrize("code", [errors.INSUFFICIENT_RAM, errors.MEDIA_NOT_DOWNLOADED,
                                      errors.CANCELLED, errors.FFMPEG_FAILED,
                                      errors.BACKEND_ERROR, errors.CUDA_UNAVAILABLE])
    def test_a_fault_of_one_message_does_not(self, code):
        assert not auto_queue.pauses_the_queue(errors.TranscriptionError(code, "x"))

    def test_not_an_error_does_not(self):
        assert not auto_queue.pauses_the_queue(None)


class _Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


def _queue(run_one, **kwargs):
    return auto_queue.AutoTranscriptionQueue(run_one, start_worker=False, **kwargs)


class TestTheQueue:
    def test_items_run_in_order_one_at_a_time(self):
        ran = []
        queue = _queue(ran.append)
        for index in range(3):
            assert queue.submit(("c", index), index) == auto_queue.ACCEPTED
        assert queue.pending == 3
        queue.drain()
        assert ran == [0, 1, 2]
        assert queue.pending == 0

    def test_the_same_message_is_not_queued_twice(self):
        queue = _queue(lambda item: None)
        assert queue.submit(("c", "M1"), 1) == auto_queue.ACCEPTED
        assert queue.submit(("c", "M1"), 1) == auto_queue.DUPLICATE
        queue.drain()
        assert queue.submit(("c", "M1"), 1) == auto_queue.ACCEPTED

    def test_a_burst_beyond_the_limit_is_dropped(self):
        queue = _queue(lambda item: None, max_pending=2)
        assert queue.submit(1, 1) == auto_queue.ACCEPTED
        assert queue.submit(2, 2) == auto_queue.ACCEPTED
        assert queue.submit(3, 3) == auto_queue.FULL
        queue.drain()
        assert queue.submit(3, 3) == auto_queue.ACCEPTED

    def test_a_run_that_raises_does_not_stop_the_rest(self):
        ran = []

        def run_one(item):
            if item == 1:
                raise RuntimeError("boom")
            ran.append(item)

        queue = _queue(run_one)
        for item in (1, 2):
            queue.submit(item, item)
        queue.drain()
        assert ran == [2]
        assert queue.pending == 0

    def test_a_pause_refuses_new_work_and_skips_what_waits(self):
        clock = _Clock()
        ran = []
        queue = _queue(ran.append, clock=clock)
        queue.submit(1, 1)
        assert queue.pause(60) is True
        assert queue.paused
        assert queue.submit(2, 2) == auto_queue.PAUSED
        queue.drain()
        assert ran == []
        assert queue.pending == 0

    def test_a_pause_is_announced_once_and_ends(self):
        clock = _Clock()
        queue = _queue(lambda item: None, clock=clock)
        assert queue.pause(60) is True
        assert queue.pause(60) is False
        clock.now += 61
        assert not queue.paused
        assert queue.submit(1, 1) == auto_queue.ACCEPTED
        assert queue.pause(60) is True

    def test_a_stopped_queue_takes_nothing(self):
        queue = _queue(lambda item: None)
        queue.stop()
        assert queue.submit(1, 1) == auto_queue.STOPPED

    def test_the_real_worker_thread_runs_items(self):
        import threading
        done = threading.Event()
        queue = auto_queue.AutoTranscriptionQueue(lambda item: done.set())
        try:
            queue.submit("k", "item")
            assert done.wait(5)
        finally:
            queue.stop()
