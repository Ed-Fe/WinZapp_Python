"""Stationary-noise suppression for the outgoing call microphone.

Fans, air conditioners, laptop hiss and mains hum reach the other person
because WinZapp owns the microphone: the page never opens a device, so
Chrome's own noise suppression never sees the signal. This is a short-time
spectral Wiener filter, mono, 48 kHz, 20 ms sqrt-Hann windows at 50% overlap:

* the noise spectrum follows the smoothed power down at once and creeps up
  slowly (1.3 dB/s), so a fan changing speed is followed and continuous
  speech is not mistaken for noise. The first version took the minimum over a
  1.5 s window instead, which on speech without long pauses is speech: it
  measured 12 dB SNR on clean speech, against 40 dB now;
* the a priori speech-to-noise ratio comes from the decision-directed
  estimate, which keeps the residual from turning into musical tones;
* the gain is floored, so what remains of the noise is quieter, never absent:
  a call that sounds like the line went dead makes people say "hello?".

The gain is deliberately not smoothed across frequency: neighbouring bins of a
voiced vowel are harmonic and valley, and averaging their gains attenuates the
harmonics themselves.

Pure numpy, independent of sounddevice and wx.
"""

from __future__ import annotations

import threading

import numpy as np

NS_SAMPLE_RATE = 48_000
NS_WINDOW = 960
NS_HOP = NS_WINDOW // 2

# Loudest reduction of the noise, in dB.
MAX_ATTENUATION_DB = 18.0
# The tracked noise is the smoothed power times NOISE_BIAS (the smoothed power
# of pure noise dips below its mean, and the tracker follows the dips), and
# rises by NOISE_RISE per 10 ms hop while the power stays above it.
POWER_SMOOTHING = 0.7
NOISE_BIAS = 1.2
NOISE_RISE = 1.003
DECISION_DIRECTED = 0.98


class NoiseSuppressor:
    """Streaming suppressor: process() takes any amount of audio."""

    def __init__(self, *, max_attenuation_db: float = MAX_ATTENUATION_DB):
        self._floor = 10.0 ** (-abs(max_attenuation_db) / 20.0)
        n = NS_WINDOW
        self._window = np.sqrt(np.hanning(n + 1)[:-1])
        self._bins = n // 2 + 1
        self._lock = threading.Lock()
        self._reset_state()

    def _reset_state(self) -> None:
        b = self._bins
        self._pending = np.empty(0, dtype=np.float32)
        self._frame = np.zeros(NS_WINDOW, dtype=np.float64)
        self._tail = np.zeros(NS_HOP, dtype=np.float64)
        self._smoothed = None
        self._noise = None
        self._prev_gain = np.ones(b)
        self._prev_snr = np.ones(b)

    def reset(self) -> None:
        with self._lock:
            self._reset_state()

    def process(self, samples: np.ndarray) -> np.ndarray:
        """Return denoised audio, in whole hops of 10 ms.

        Output can be shorter than the input (the remainder waits for the next
        call) but length is conserved over the stream.
        """
        data = np.asarray(samples, dtype=np.float32).reshape(-1)
        with self._lock:
            self._pending = np.concatenate((self._pending, data))
            out = []
            while self._pending.size >= NS_HOP:
                hop = self._pending[:NS_HOP].astype(np.float64)
                self._pending = self._pending[NS_HOP:]
                out.append(self._process_hop(hop))
        if not out:
            return np.empty(0, dtype=np.float32)
        return np.concatenate(out).astype(np.float32)

    def _process_hop(self, hop: np.ndarray) -> np.ndarray:
        self._frame = np.concatenate((self._frame[NS_HOP:], hop))
        spectrum = np.fft.rfft(self._frame * self._window)
        power = np.abs(spectrum) ** 2
        noise = self._track_noise(power)

        snr_post = power / (noise + 1e-12)
        snr_prior = (DECISION_DIRECTED * self._prev_gain ** 2 * self._prev_snr
                     + (1 - DECISION_DIRECTED) * np.maximum(snr_post - 1.0, 0.0))
        gain = snr_prior / (1.0 + snr_prior)
        gain = np.maximum(gain, self._floor)
        self._prev_gain = gain
        self._prev_snr = snr_post

        frame = np.fft.irfft(spectrum * gain, NS_WINDOW) * self._window
        out = self._tail + frame[:NS_HOP]
        self._tail = frame[NS_HOP:]
        return out

    def _track_noise(self, power: np.ndarray) -> np.ndarray:
        if self._smoothed is None:
            self._smoothed = power.copy()
            self._noise = power * 0.5
        self._smoothed = POWER_SMOOTHING * self._smoothed + (1 - POWER_SMOOTHING) * power
        target = NOISE_BIAS * self._smoothed
        self._noise = np.where(target < self._noise, target, self._noise * NOISE_RISE)
        return self._noise
