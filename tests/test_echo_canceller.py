"""The echo canceller removes the far-end voice from the microphone.

The first version was tested with stationary white noise through a pure delay,
where it reached 52 dB; on speech it reached 1-4 dB and nobody could hear the
difference. So the signals here are speech-like: coloured noise in syllable
bursts with pauses, through a reverberant room, with the streams misaligned the
way two real audio devices are.
"""

import numpy as np

from core.echo_canceller import AEC_BLOCK, AEC_SAMPLE_RATE, EchoCanceller

SR = AEC_SAMPLE_RATE


def _speech_like(seconds, seed):
    rng = np.random.default_rng(seed)
    n = SR * seconds
    x = np.convolve(rng.standard_normal(n), np.ones(6) / 6, "same")
    envelope = np.zeros(n)
    i = 0
    while i < n:
        length = int(rng.uniform(0.08, 0.3) * SR)
        envelope[i:i + length] = rng.uniform(0.2, 1.0) if rng.random() < 0.7 else 0.0
        i += length
    envelope = np.convolve(envelope, np.ones(480) / 480, "same")
    return (x * envelope * 0.3).astype(np.float32)


def _room(delay_ms, seed=7, rt60=0.25, gain=0.5):
    rng = np.random.default_rng(seed)
    delay = int(delay_ms * SR / 1000)
    n = delay + int(0.4 * SR)
    t = np.arange(n - delay) / SR
    h = np.zeros(n)
    h[delay:] = rng.standard_normal(n - delay) * np.exp(-6.9 * t / rt60) * 0.05
    h[delay] = gain
    return h


def _through(x, h):
    size = len(x) + len(h)
    return np.fft.irfft(np.fft.rfft(x, size) * np.fft.rfft(h, size), size)[:len(x)].astype(np.float32)


def _rms(x):
    return float(np.sqrt(np.mean(np.square(x))))


def _erle_db(before, after):
    return 10 * np.log10((np.mean(before ** 2) + 1e-12) / (np.mean(after ** 2) + 1e-12))


def _run(aec, far, mic, lead=0):
    """Feed the streams as the call does: reference first, then the microphone.

    `lead` is how far ahead of the microphone the reference already is.
    """
    out = []
    pushed = 0
    for i in range(0, len(far) - AEC_BLOCK, AEC_BLOCK):
        upto = min(len(far), i + AEC_BLOCK + lead)
        if upto > pushed:
            aec.push_reference(far[pushed:upto])
            pushed = upto
        out.append(aec.process(mic[i:i + AEC_BLOCK]))
    return np.concatenate(out)


def test_speech_echo_is_reduced_by_at_least_8_db():
    far = _speech_like(14, 1)
    echo = _through(far, _room(60))
    out = _run(EchoCanceller(), far, echo, lead=4800)
    tail = slice(9 * SR, len(out))
    assert _erle_db(echo[tail], out[tail]) > 8


def test_echo_that_arrives_before_the_paired_reference_is_still_cancelled():
    # The microphone stream started 300 ms after the reference stream and the
    # device adds only 40 ms: by sample index the echo precedes its source.
    far = _speech_like(14, 2)
    echo = _through(far, _room(40))
    early = 300 * SR // 1000
    far_stream = np.concatenate((np.zeros(early, dtype=np.float32), far))
    mic = np.concatenate((echo, np.zeros(early, dtype=np.float32)))
    out = _run(EchoCanceller(), far_stream, mic, lead=early + 4800)
    tail = slice(9 * SR, len(out))
    assert _erle_db(mic[:len(out)][tail], out[tail]) > 8


def test_echo_later_than_the_filter_tail_is_still_cancelled():
    far = _speech_like(14, 3)
    echo = _through(far, _room(450))
    out = _run(EchoCanceller(), far, echo)
    tail = slice(9 * SR, len(out))
    assert _erle_db(echo[tail], out[tail]) > 8


def test_headset_without_an_echo_path_is_left_untouched():
    # The far end plays but the microphone never hears it: an ungated filter
    # fits noise and puts the other person's voice back, inverted.
    far = _speech_like(14, 4)
    near = _speech_like(14, 5)
    out = _run(EchoCanceller(), far, near)
    tail = slice(4 * SR, len(out))
    assert np.max(np.abs(out[tail] - near[:len(out)][tail])) < 1e-4


def test_near_end_speech_survives_double_talk():
    far = _speech_like(18, 6)
    echo = _through(far, _room(60))
    near = np.zeros_like(far)
    near[9 * SR:] = _speech_like(9, 11)[:len(near) - 9 * SR] * 4
    only_echo = _run(EchoCanceller(), far, echo, lead=4800)
    both = _run(EchoCanceller(), far, echo + near, lead=4800)
    window = slice(11 * SR, len(both))
    # What the canceller did to the near-end voice: the difference between the
    # two runs, which are identical up to the moment the near end starts.
    kept = both[window] - only_echo[window]
    fidelity = _erle_db(near[:len(both)][window], kept - near[:len(both)][window])
    level = 10 * np.log10(np.mean(kept ** 2) / np.mean(near[:len(both)][window] ** 2))
    assert fidelity > 6
    assert abs(level) < 3


def test_skipped_microphone_audio_does_not_break_the_alignment():
    far = _speech_like(14, 8)
    echo = _through(far, _room(60))
    aec = EchoCanceller()
    out = []
    pushed = 0
    for j, i in enumerate(range(0, len(far) - AEC_BLOCK, AEC_BLOCK)):
        aec.push_reference(far[pushed:i + AEC_BLOCK + 4800])
        pushed = min(len(far), i + AEC_BLOCK + 4800)
        if j % 40 == 39:
            aec.skip_microphone(AEC_BLOCK)  # a stale frame the sender dropped
            continue
        out.append(aec.process(echo[i:i + AEC_BLOCK]))
    # The dropped frames are also missing from the microphone stream.
    kept = np.concatenate([echo[i:i + AEC_BLOCK]
                           for j, i in enumerate(range(0, len(far) - AEC_BLOCK, AEC_BLOCK))
                           if j % 40 != 39])
    out = np.concatenate(out)
    tail = slice(9 * SR, len(out))
    assert _erle_db(kept[:len(out)][tail], out[tail]) > 6


def test_no_reference_passes_microphone_through():
    mic = (np.random.default_rng(3).standard_normal(AEC_BLOCK * 3) * 0.1).astype(np.float32)
    out = EchoCanceller().process(mic)
    assert np.allclose(out, mic)


def test_stream_length_is_conserved_across_odd_chunks():
    aec = EchoCanceller()
    mic = np.zeros(AEC_BLOCK * 4 + 100, dtype=np.float32)
    total = sum(len(aec.process(mic[i:i + 333])) for i in range(0, len(mic), 333))
    assert total == AEC_BLOCK * 4


def test_reset_forgets_the_learned_path_and_alignment():
    far = _speech_like(8, 9)
    echo = _through(far, _room(60))
    aec = EchoCanceller()
    _run(aec, far, echo[:len(far)], lead=4800)
    aec.reset()
    assert aec.diagnostics() == {"cancelling": False, "echo_lag_ms": None, "reduction_db": 0.0}


def test_diagnostics_report_the_echo_lag_not_the_internal_alignment():
    # A 300 ms echo used to report 40 ms: the filter's own lag, which the
    # alignment pins to a constant.
    far = _speech_like(10, 10)
    echo = _through(far, _room(300))
    aec = EchoCanceller()
    _run(aec, far[:7 * SR], echo[:7 * SR])
    assert 250 <= aec.diagnostics()["echo_lag_ms"] <= 350


def test_reduction_report_is_clamped():
    aec = EchoCanceller()
    aec._eng_mic, aec._eng_out = 1.0, 1e-15
    assert aec.diagnostics()["reduction_db"] == 60.0
