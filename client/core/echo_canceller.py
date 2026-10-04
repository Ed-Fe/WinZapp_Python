"""Acoustic echo cancellation for the outgoing call microphone.

When the call plays through a speaker, the microphone hears the other person
and sends them their own voice back. The far-end signal is known exactly (it is
what the output callback played), so an adaptive filter learns the speaker-to-
microphone path and subtracts its estimate from the captured audio.

Three parts, each there because the one before it was measured to fail on real
speech (synthesized with Windows SAPI, a room impulse response, 60 ms echo):

* **Alignment.** Reference and microphone are paired by sample index, but the
  two streams start at different moments and each device adds its own latency,
  so the echo can arrive before the paired reference (nothing a filter can
  model) or hundreds of milliseconds after it (beyond its tail). A GCC-PHAT
  cross-correlation, refreshed twice a second, finds the lag and the reference
  is re-read at the position that puts the echo ~40 ms into the filter. The
  learned echo path is shifted along with it, not thrown away.
* **Filter.** Partitioned-block frequency-domain Kalman filter (overlap-save,
  mono, 48 kHz, 20 ms blocks, 16 partitions = 320 ms tail), diagonal state per
  bin and partition. The previous NLMS normalised by a smoothed power that lags
  every speech onset, and settled at 1-4 dB of echo reduction on speech (52 dB
  on white noise, which is all its test used); this one reaches ~20 dB after a
  few seconds. The gain shrinks by itself when the error grows (double talk).
* **Gate.** The filter always adapts, but its estimate is subtracted only while
  the cross-correlation keeps finding a confident echo peak AND the filter has
  been seen to reduce the microphone energy. With a headset there is no echo
  path: an ungated filter fits noise and puts the other person's voice back,
  inverted, at about -13 dB. Gated, the microphone passes through untouched.

Pure numpy, independent of sounddevice and wx, so it is testable with synthetic
signals.
"""

from __future__ import annotations

import threading

import numpy as np

AEC_SAMPLE_RATE = 48_000
AEC_BLOCK = 960
AEC_PARTITIONS = 16

# The echo must start between LAG_MIN and LAG_MAX samples after the aligned
# reference (10-100 ms, well inside the 320 ms tail). Outside that the reference
# is re-read so it starts at LAG_TARGET (40 ms); inside it nothing moves, so a
# noisy estimate cannot make the alignment jitter.
LAG_MIN = 480
LAG_MAX = 4800
LAG_TARGET = 1920
# How far the alignment may look: older reference (device latency, a stream
# that started early) and newer reference (an echo that would otherwise
# arrive before the paired reference).
SEARCH_BACK = int(AEC_SAMPLE_RATE * 0.8)
SEARCH_AHEAD = int(AEC_SAMPLE_RATE * 0.3)
ESTIMATE_EVERY_BLOCKS = 25
ESTIMATE_WINDOW = AEC_SAMPLE_RATE
# Correlation peak / median, and how many estimates keep the gate open.
PEAK_RATIO = 10
HOLD_ESTIMATES = 8
CONSISTENT_SAMPLES = 96
# Microphone energy over the filter's residual, over far-end-active blocks,
# that proves the filter is really removing something.
REDUCTION_TO_OPEN = 1.6
REFERENCE_KEEP = AEC_SAMPLE_RATE * 3


class EchoCanceller:
    """Streaming echo canceller: push far-end audio, process near-end audio."""

    def __init__(self, *, block: int = AEC_BLOCK, partitions: int = AEC_PARTITIONS,
                 forgetting: float = 0.999, noise_smoothing: float = 0.9,
                 process_noise: float = 1e-4, step: float = 0.7):
        self._n = block
        self._p = partitions
        self._bins = block + 1
        self._forgetting = forgetting
        self._noise_smoothing = noise_smoothing
        self._process_noise = process_noise
        self._step = step
        self._lock = threading.Lock()
        self._reset_state()

    def _reset_state(self) -> None:
        p, b = self._p, self._bins
        self._weights = np.zeros((p, b), dtype=np.complex128)
        self._spectra = np.zeros((p, b), dtype=np.complex128)
        self._variance = np.ones((p, b))
        self._noise = np.full(b, 1e-2)
        self._ref_prev = np.zeros(self._n)
        self._ref = np.empty(0, dtype=np.float32)
        self._ref_base = 0
        self._mic = np.empty(0, dtype=np.float32)
        self._mic_hist = np.empty(0, dtype=np.float32)
        self._mic_hist_base = 0
        self._mic_pos = 0
        self._shift = 0
        self._blocks = 0
        self._hold = 0
        self._latched = False
        self._gate = 0.0
        self._last_lag = None
        self._eng_mic = 1e-9
        self._eng_out = 1e-9
        self.lag_samples = None

    def reset(self) -> None:
        with self._lock:
            self._reset_state()

    # ── input ────────────────────────────────────────────────────────────

    def push_reference(self, samples: np.ndarray) -> None:
        """Queue far-end audio (48 kHz mono float32) as the device played it."""
        data = np.asarray(samples, dtype=np.float32).reshape(-1)
        if not data.size:
            return
        with self._lock:
            self._ref = np.concatenate((self._ref, data))
            if self._ref.size > REFERENCE_KEEP:
                cut = self._ref.size - REFERENCE_KEEP
                self._ref = self._ref[cut:]
                self._ref_base += cut

    def skip_microphone(self, samples: int) -> None:
        """Microphone audio that was discarded before reaching process().

        Keeps the sample-index pairing with the reference truthful; without it
        every dropped frame would look like a sudden shift of the echo.
        """
        samples = max(0, int(samples))
        if not samples:
            return
        with self._lock:
            # Time went by: the history is a timeline, so it gets the silence
            # that stands for the missing audio, and the reference spectra the
            # filter remembers are re-read to include the block that was not
            # processed.
            self._remember_microphone(np.zeros(samples, dtype=np.float32))
            self._mic_pos += samples
            self._rebuild_reference_history()

    def process(self, samples: np.ndarray) -> np.ndarray:
        """Return echo-reduced microphone audio.

        Input of any length is accepted; output is produced in whole blocks, so
        it can be shorter than the input (the remainder waits for the next
        call). Length is conserved over the stream.
        """
        data = np.asarray(samples, dtype=np.float32).reshape(-1)
        n = self._n
        with self._lock:
            self._mic = np.concatenate((self._mic, data))
            out = []
            while self._mic.size >= n:
                mic = self._mic[:n]
                self._mic = self._mic[n:]
                ref = self._ref_slice(self._mic_pos + self._shift, n)
                out.append(self._process_block(mic, ref))
                self._remember_microphone(mic)
                self._mic_pos += n
                self._blocks += 1
                if self._blocks % ESTIMATE_EVERY_BLOCKS == 0:
                    self._estimate_alignment()
        if not out:
            return np.empty(0, dtype=np.float32)
        return np.concatenate(out)

    def diagnostics(self) -> dict:
        """What the canceller currently believes, for the call log."""
        with self._lock:
            lag_ms = None
            if self.lag_samples is not None:
                # How much later the microphone hears the reference than the
                # streams' own pairing says; not the filter's internal lag,
                # which the alignment keeps at a constant 40 ms.
                lag_ms = round(self.lag_samples * 1000 / AEC_SAMPLE_RATE)
            reduction = 0.0
            if self._eng_mic > 1e-6:
                reduction = round(float(min(60.0, 10 * np.log10(self._eng_mic / self._eng_out))), 1)
            return {
                "cancelling": self._gate > 0.5,
                "echo_lag_ms": lag_ms,
                "reduction_db": reduction,
            }

    # ── alignment ────────────────────────────────────────────────────────

    def _ref_slice(self, start: int, length: int) -> np.ndarray:
        """Reference samples [start, start + length) by absolute index."""
        out = np.zeros(length, dtype=np.float32)
        lo = max(start, self._ref_base)
        hi = min(start + length, self._ref_base + self._ref.size)
        if hi > lo:
            out[lo - start:hi - start] = self._ref[lo - self._ref_base:hi - self._ref_base]
        return out

    def _remember_microphone(self, mic: np.ndarray) -> None:
        self._mic_hist = np.concatenate((self._mic_hist, mic))
        if self._mic_hist.size > ESTIMATE_WINDOW * 2:
            cut = self._mic_hist.size - ESTIMATE_WINDOW * 2
            self._mic_hist = self._mic_hist[cut:]
            self._mic_hist_base += cut

    def _estimate_alignment(self) -> None:
        window = ESTIMATE_WINDOW
        if self._mic_hist.size < window:
            return
        mic_end = self._mic_hist_base + self._mic_hist.size
        mic_start = mic_end - window
        ahead = min(SEARCH_AHEAD, max(0, self._ref_base + self._ref.size - mic_end))
        span = SEARCH_BACK + ahead
        ref = self._ref_slice(mic_start - SEARCH_BACK, SEARCH_BACK + window + ahead)
        ref = ref.astype(np.float64)
        mic = self._mic_hist[-window:].astype(np.float64)
        if (np.mean(ref[SEARCH_BACK:SEARCH_BACK + window] ** 2) < 1e-9
                or np.mean(mic ** 2) < 1e-12):
            return
        size = 1 << int(np.ceil(np.log2(ref.size + window)))
        cross = np.fft.rfft(mic, size) * np.conj(np.fft.rfft(ref, size))
        cross /= np.abs(cross) + 1e-9  # PHAT: only the phase, so the peak is sharp
        freqs = np.fft.rfftfreq(size, 1 / AEC_SAMPLE_RATE)
        cross[(freqs < 200) | (freqs > 4000)] = 0  # where speech is
        corr = np.fft.irfft(cross, size)
        # corr[k] = sum_t ref[t] * mic[t + k]; a peak at k means the microphone
        # lags the reference by SEARCH_BACK + k samples, k in [-span, 0].
        values = np.abs(np.concatenate((corr[size - span:], corr[:1])))
        peak_at = int(np.argmax(values))
        confident = values[peak_at] >= PEAK_RATIO * (np.median(values) + 1e-12)
        if not confident:
            self._hold = max(0, self._hold - 1)
            self._last_lag = None
            if not self._hold:
                self.lag_samples = None
            return
        lag = SEARCH_BACK + (peak_at - span)
        consistent = self._last_lag is not None and abs(lag - self._last_lag) <= CONSISTENT_SAMPLES
        self._last_lag = lag
        if not consistent:
            return
        self._hold = HOLD_ESTIMATES
        self.lag_samples = lag
        if not LAG_MIN <= self._shift + lag <= LAG_MAX:
            # A newer reference than what has been played does not exist yet:
            # never shift past the end of it, or the block reads silence.
            newest = self._ref_base + self._ref.size - self._mic_pos - 2 * self._n
            self._realign(min(LAG_TARGET - lag, max(0, newest)))

    def _realign(self, new_shift: int) -> None:
        """Move the reference alignment and carry the learned echo path along.

        The room has not changed: the echo simply starts later (or earlier)
        relative to the aligned reference, so the impulse response is shifted
        rather than discarded, and the history of reference spectra is rebuilt
        under the new alignment.
        """
        n, p = self._n, self._p
        delta = new_shift - self._shift
        self._shift = new_shift
        impulse = np.fft.irfft(self._weights, 2 * n, axis=1)[:, :n].reshape(-1)
        moved = np.zeros_like(impulse)
        if abs(delta) < impulse.size:
            if delta >= 0:
                moved[delta:] = impulse[:impulse.size - delta]
            else:
                moved[:impulse.size + delta] = impulse[-delta:]
        padded = np.zeros((p, 2 * n))
        padded[:, :n] = moved.reshape(p, n)
        self._weights = np.fft.rfft(padded, axis=1)
        self._rebuild_reference_history()

    def _rebuild_reference_history(self) -> None:
        """Recompute the remembered reference spectra from the reference
        buffer under the current alignment and microphone position."""
        n = self._n
        for k in range(self._p):
            start = self._mic_pos - (k + 1) * n
            window = np.concatenate((self._ref_slice(start - n + self._shift, n),
                                     self._ref_slice(start + self._shift, n)))
            self._spectra[k] = np.fft.rfft(window.astype(np.float64))
        self._ref_prev = self._ref_slice(
            self._mic_pos - n + self._shift, n).astype(np.float64)

    # ── filter ───────────────────────────────────────────────────────────

    def _process_block(self, mic: np.ndarray, ref: np.ndarray) -> np.ndarray:
        n = self._n
        mic64 = mic.astype(np.float64)
        ref64 = ref.astype(np.float64)

        self._spectra = np.roll(self._spectra, 1, axis=0)
        self._spectra[0] = np.fft.rfft(np.concatenate((self._ref_prev, ref64)))
        self._ref_prev = ref64

        # Nothing was played: there is no echo to learn or remove.
        if float(np.mean(ref64 * ref64)) < 1e-9:
            self._gate = max(0.0, self._gate - 0.2)
            return mic.astype(np.float32, copy=False)

        spectra = self._spectra
        estimate = np.fft.irfft(np.sum(self._weights * spectra, axis=0), 2 * n)[n:]
        error = np.fft.rfft(np.concatenate((np.zeros(n), mic64 - estimate)))

        # Kalman update, diagonal in bins and partitions. The innovation
        # variance is what the state uncertainty predicts for the error plus
        # the noise floor (near-end speech and unmodelled echo), so the gain
        # falls by itself when the error grows for reasons the state cannot
        # explain -- the case NLMS needed a separate double-talk guard for.
        innovation = np.sum(np.abs(spectra) ** 2 * self._variance, axis=0) + self._noise + 1e-8
        gain = self._variance * np.conj(spectra) / innovation
        weights = self._weights + self._step * gain * error
        # Overlap-save constraint: each partition's impulse response is n long.
        impulse = np.fft.irfft(weights, 2 * n, axis=1)
        impulse[:, n:] = 0.0
        self._weights = np.fft.rfft(impulse, axis=1)

        posterior = mic64 - np.fft.irfft(np.sum(self._weights * spectra, axis=0), 2 * n)[n:]
        posterior_spectrum = np.fft.rfft(np.concatenate((np.zeros(n), posterior)))
        self._noise = (self._noise_smoothing * self._noise
                       + (1 - self._noise_smoothing) * np.abs(posterior_spectrum) ** 2)
        variance = np.maximum(self._variance * (1 - self._step * np.real(gain * spectra)), 0.0)
        a2 = self._forgetting ** 2
        self._variance = (a2 * variance + (1 - a2) * np.abs(self._weights) ** 2
                          + self._process_noise)

        return self._gated(mic64, posterior)

    def _gated(self, mic: np.ndarray, posterior: np.ndarray) -> np.ndarray:
        """Subtract the echo estimate only while there is proof of an echo."""
        self._eng_mic += 0.03 * (float(np.mean(mic ** 2)) - self._eng_mic)
        self._eng_out += 0.03 * (float(np.mean(posterior ** 2)) - self._eng_out)
        if self._hold <= 0:
            self._latched = False
        elif self._eng_mic >= REDUCTION_TO_OPEN * self._eng_out:
            self._latched = True
        target = 1.0 if (self._hold > 0 and self._latched) else 0.0
        self._gate += max(-0.2, min(0.2, target - self._gate))
        echo = mic - posterior
        return np.clip(mic - self._gate * echo, -1.0, 1.0).astype(np.float32)
