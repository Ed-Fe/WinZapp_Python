"""Noise suppression and echo bookkeeping in the call's microphone path."""

import base64
import threading

import numpy as np

from core.call_audio import (
    CALL_FRAME_SAMPLES, CALL_SAMPLE_RATE, CallAudioConfig, CallAudioSession, _pcm16_bytes,
)
from tests.test_call_audio_session import _Socket, _wait_for


def test_noise_suppression_is_off_unless_configured():
    assert CallAudioSession(_Socket(), CallAudioConfig(session="s"))._noise_suppressor is None
    enabled = CallAudioSession(_Socket(), CallAudioConfig(session="s", noise_suppression=True))
    assert enabled._noise_suppressor is not None


def _send(noise_suppression):
    sio = _Socket()
    session = CallAudioSession(
        sio, CallAudioConfig(session="s", noise_suppression=noise_suppression))
    rng = np.random.default_rng(3)
    noise = (rng.standard_normal(CALL_SAMPLE_RATE * 4) * 0.02).astype(np.float32)
    thread = threading.Thread(target=session._send_microphone_loop, daemon=True)
    thread.start()
    for i in range(0, len(noise), CALL_FRAME_SAMPLES):
        session._mic_queue.put(_pcm16_bytes(noise[i:i + CALL_FRAME_SAMPLES]))
        _wait_for(lambda: session._mic_queue.qsize() == 0)
    session._stop_event.set()
    thread.join(timeout=2)
    sent = b"".join(base64.b64decode(event[1]["pcm"]) for event in sio.events)
    return np.frombuffer(sent, dtype="<i2").astype(np.float32) / 32768.0, noise


def test_send_loop_lowers_steady_noise_when_enabled():
    plain, noise = _send(False)
    suppressed, _ = _send(True)
    tail = slice(-CALL_SAMPLE_RATE, None)
    assert np.sqrt(np.mean(plain[tail] ** 2)) > 0.9 * np.sqrt(np.mean(noise[tail] ** 2))
    assert np.sqrt(np.mean(suppressed[tail] ** 2)) < 0.6 * np.sqrt(np.mean(plain[tail] ** 2))


def test_frames_dropped_for_latency_are_reported_to_the_echo_canceller():
    session = CallAudioSession(_Socket(), CallAudioConfig(session="s", echo_cancellation=True))
    skipped = []
    session._echo_canceller.skip_microphone = skipped.append
    # A backlog deeper than the target makes the sender drop the stale frames.
    for _ in range(6):
        session._mic_queue.put(_pcm16_bytes(np.zeros(CALL_FRAME_SAMPLES, dtype=np.float32)))
    thread = threading.Thread(target=session._send_microphone_loop, daemon=True)
    thread.start()
    _wait_for(lambda: session._mic_queue.qsize() == 0)
    session._stop_event.set()
    thread.join(timeout=2)
    assert skipped and skipped[0] % CALL_FRAME_SAMPLES == 0


def test_reference_resampling_keeps_the_clock_across_odd_device_periods():
    # 44.1 kHz in 448-frame periods: rounding each chunk on its own gains 0.34
    # samples per chunk (~780 ppm) and the reference slides off the microphone.
    from core.call_audio import _ReferenceResampler

    resampler = _ReferenceResampler()
    chunk = np.zeros(448, dtype=np.float32)
    total = sum(len(resampler.process(chunk, 44_100)) for _ in range(2000))
    assert abs(total - 2000 * 448 * CALL_SAMPLE_RATE / 44_100) <= 1


def test_reference_resampler_passes_the_call_rate_through_and_restarts_on_a_rate_change():
    from core.call_audio import _ReferenceResampler

    resampler = _ReferenceResampler()
    data = np.arange(960, dtype=np.float32)
    assert np.array_equal(resampler.process(data, CALL_SAMPLE_RATE), data)
    first = len(resampler.process(np.zeros(441, dtype=np.float32), 44_100))
    resampler.process(np.zeros(480, dtype=np.float32), 32_000)
    assert len(resampler.process(np.zeros(441, dtype=np.float32), 44_100)) == first


def test_reference_played_before_a_drop_reaches_the_canceller_first():
    session = CallAudioSession(_Socket(), CallAudioConfig(session="s", echo_cancellation=True))
    order = []
    session._echo_canceller.push_reference = lambda samples: order.append("reference")
    session._echo_canceller.skip_microphone = lambda samples: order.append("skip")
    session._echo_reference_tap.append((np.zeros(480, dtype=np.float32), CALL_SAMPLE_RATE))
    for _ in range(6):
        session._mic_queue.put(_pcm16_bytes(np.zeros(CALL_FRAME_SAMPLES, dtype=np.float32)))
    thread = threading.Thread(target=session._send_microphone_loop, daemon=True)
    thread.start()
    _wait_for(lambda: session._mic_queue.qsize() == 0)
    session._stop_event.set()
    thread.join(timeout=2)
    assert order[:2] == ["reference", "skip"]


def test_the_saved_call_settings_reach_the_audio_session(monkeypatch):
    # Voice and video calls both build their audio here.
    import core.call_audio as call_audio
    from main_window.calls import CallsMixin

    built = []

    class _Session:
        def __init__(self, sio, config):
            built.append(config)

    monkeypatch.setattr(call_audio, "CallAudioSession", _Session)

    class _WS:
        sio = object()
        instance_name = "acct"

    class _Stub:
        ws = _WS()
        token = "acct:secret"
        settings = {"call_audio_devices": {"echo_cancellation": True, "noise_suppression": True}}

        def call_audio_device(self, kind):
            return ""

    CallsMixin._build_call_audio_session(_Stub())
    assert built[0].echo_cancellation is True
    assert built[0].noise_suppression is True
    _Stub.settings = {"call_audio_devices": {}}
    CallsMixin._build_call_audio_session(_Stub())
    assert built[1].echo_cancellation is False
    assert built[1].noise_suppression is False
