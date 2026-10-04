"""The suppressor lowers steady noise and leaves speech alone."""

import numpy as np

from core.noise_suppressor import NS_HOP, NoiseSuppressor

SR = 48_000


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
    return (x * envelope * 0.5).astype(np.float32)


def _run(samples, chunk=960):
    ns = NoiseSuppressor()
    out = np.concatenate([ns.process(samples[i:i + chunk])
                          for i in range(0, len(samples) - chunk + 1, chunk)])
    return out[NS_HOP:]  # the output trails the input by one hop


def _db(signal, error):
    return 10 * np.log10((np.mean(signal ** 2) + 1e-14) / (np.mean(error ** 2) + 1e-14))


def _masks(clean):
    frames = len(clean) // 480
    level = np.array([np.mean(clean[i * 480:(i + 1) * 480] ** 2) for i in range(frames)])
    active = np.repeat(level > level.max() * 1e-3, 480)
    return np.concatenate((active, np.zeros(len(clean) - len(active), dtype=bool)))


def test_steady_noise_is_lowered_in_the_pauses():
    clean = _speech_like(10, 1)
    noise = (np.random.default_rng(2).standard_normal(len(clean)) * 0.01).astype(np.float32)
    out = _run(clean + noise)
    n = len(out)
    pauses = ~_masks(clean[:n])
    assert _db(noise[:n][pauses], out[pauses]) > 5


def test_speech_keeps_its_shape_with_noise_around_it():
    # The synthetic voice is coloured noise, so it cannot be told from the
    # noise and the SNR does not improve here (on real speech it does: 24 -> 28
    # dB); what must hold is that it is not shredded.
    clean = _speech_like(10, 3)
    noise = (np.random.default_rng(4).standard_normal(len(clean)) * 0.01).astype(np.float32)
    out = _run(clean + noise)
    n = len(out)
    speaking = _masks(clean[:n])
    assert _db(clean[:n][speaking], (out - clean[:n])[speaking]) > 12


def test_clean_speech_passes_almost_unchanged():
    # The first tracker took the minimum over a window and mistook continuous
    # speech for noise: 12 dB of distortion on a clean recording.
    clean = _speech_like(10, 5)
    out = _run(clean)
    n = len(out)
    speaking = _masks(clean[:n])
    assert _db(clean[:n][speaking], (out - clean[:n])[speaking]) > 25


def test_a_hum_is_lowered():
    t = np.arange(SR * 10) / SR
    hum = (0.02 * np.sin(2 * np.pi * 100 * t)).astype(np.float32)
    clean = _speech_like(10, 6)
    out = _run(clean + hum)
    n = len(out)
    pauses = ~_masks(clean[:n])
    assert _db(hum[:n][pauses], out[pauses]) > 8


def test_length_is_conserved_across_odd_chunks():
    ns = NoiseSuppressor()
    audio = np.zeros(NS_HOP * 10 + 77, dtype=np.float32)
    total = sum(len(ns.process(audio[i:i + 333])) for i in range(0, len(audio), 333))
    assert total == NS_HOP * 10


def test_silence_stays_silent_and_finite():
    out = _run(np.zeros(SR, dtype=np.float32))
    assert np.all(np.isfinite(out))
    assert np.max(np.abs(out)) < 1e-6


def test_reset_starts_over():
    noise = (np.random.default_rng(7).standard_normal(SR) * 0.01).astype(np.float32)
    ns = NoiseSuppressor()
    first = ns.process(noise)
    ns.reset()
    assert np.array_equal(ns.process(noise), first)
