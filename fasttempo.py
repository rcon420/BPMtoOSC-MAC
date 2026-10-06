"""Fast reacting BPM estimator (numpy only).

aubio's tempo tracker is accurate but holds on to the old tempo for a very long time
(30+ seconds after a song change). This estimator looks at the last ~6 seconds only,
weights recent audio more, and reacts to a new song within a few seconds.
"""
from collections import deque

import numpy as np


class FastTempo:

    def __init__(self, samplerate: int, hop: int = 256, win: int = 1024,
                 history_s: float = 6.0, update_s: float = 0.25,
                 bpm_min: float = 70.0, bpm_max: float = 180.0, prior_center: float = 135.0):
        self.sr = samplerate
        self.hop = hop
        self.win = win
        self.fs = samplerate / hop  # ODF frames per second

        self.window = np.hanning(win).astype(np.float32)
        self.audio = np.zeros(win, dtype=np.float32)
        self.pending = np.zeros(0, dtype=np.float32)
        self.prev = None

        freqs = np.fft.rfftfreq(win, 1.0 / samplerate)
        self.low = freqs < 250
        self.mid = (freqs >= 250) & (freqs < 6000)

        self.odf = deque(maxlen=int(history_s * self.fs))
        self.loud = deque(maxlen=int(1.5 * self.fs))  # RMS of the last 1.5 s (do not guess tempo from silence)
        self.update_every = max(1, int(update_s * self.fs))
        self.min_frames = int(4.0 * self.fs)  # need 4 s of audio before the first guess
        self.frames_since = 0

        # candidate tempos (0.25 BPM steps) and a soft prior around typical dance tempos
        self.grid = np.arange(bpm_min, bpm_max + 0.01, 0.25)
        self.prior = np.exp(-0.5 * (np.log2(self.grid / prior_center) / 0.9) ** 2)

        self.raw = deque(maxlen=5)
        self.published = None  # last published float bpm
        self.debug = False
        self._dbg_count = 0
        self.silent_updates = 0
        self.silence_flag = False

    def pop_silence(self) -> bool:
        """True once after the music was silent for about 3 seconds (tempo memory was cleared)"""
        flag, self.silence_flag = self.silence_flag, False
        return flag

    def reset(self):
        self.odf.clear()
        self.raw.clear()
        self.published = None
        self.prev = None
        self.frames_since = 0

    # ------------------------------------------------------------------ audio -> ODF
    def _frame(self, frame: np.ndarray) -> float:
        spec = np.abs(np.fft.rfft(frame * self.window))
        mag = np.log1p(20.0 * spec / (self.win / 4.0))
        if self.prev is None:
            self.prev = mag
            return 0.0
        flux = np.maximum(mag - self.prev, 0.0)
        self.prev = mag
        return float(1.5 * flux[self.low].sum() + flux[self.mid].sum())

    # ------------------------------------------------------------------ ODF -> bpm
    def _estimate(self):
        x = np.asarray(self.odf, dtype=np.float64)
        n = len(x)
        if x.max() < 1e-3:
            return None  # silence

        # remove slow trend, keep only the beats
        k = int(0.4 * self.fs)
        c = np.cumsum(np.insert(x, 0, 0.0))
        mean = np.empty(n)
        for i in range(n):  # moving average (cheap enough: n ~ 1500, every 0.5 s)
            lo = max(0, i - k)
            hi = min(n, i + k + 1)
            mean[i] = (c[hi] - c[lo]) / (hi - lo)
        x = np.maximum(x - mean, 0.0)

        # recent audio counts more than old audio
        x *= np.linspace(0.25, 1.0, n)
        if x.sum() < 1e-6:
            return None

        # autocorrelation (via FFT), unbiased
        spec = np.fft.rfft(x, 2 * n)
        acf = np.fft.irfft(spec * np.conj(spec))[:n]
        acf = acf / (n - np.arange(n))
        if acf[0] <= 0:
            return None
        acf = acf / acf[0]

        lags = self.fs * 60.0 / self.grid
        score = np.zeros_like(self.grid)
        # beat, 2 beats, bar and 2 bars: bar level evidence separates the real tempo from wrong fractions
        # such as 2/3 or 4/5 of it (tested on many syncopated rhythms)
        for mult, weight in ((1, 0.7), (2, 0.7), (4, 1.0), (8, 0.5)):
            idx = lags * mult
            valid = idx < n - 1
            vals = np.zeros_like(idx)
            vals[valid] = np.interp(idx[valid], np.arange(n), acf)
            score += weight * vals
        score *= self.prior

        # stickiness: stay with the tempo that is already locked unless something is clearly stronger
        if self.published is not None:
            width = 0.025 * self.published
            score *= 1.0 + 0.12 * np.exp(-0.5 * ((self.grid - self.published) / width) ** 2)

        best = int(np.argmax(score))
        if self.debug:
            self._dbg_count += 1
            if self._dbg_count % 4 == 0:   # about once per second
                order = np.argsort(score)[::-1]
                picked = []
                for idx in order:
                    if all(abs(self.grid[idx] - b) > 0.05 * b for b, _ in picked):
                        picked.append((float(self.grid[idx]), float(score[idx])))
                    if len(picked) == 4:
                        break
                print("tempo candidates:", "  ".join(f"{b:6.1f} ({sc:.2f})" for b, sc in picked), flush=True)
        return float(self.grid[best])

    # ------------------------------------------------------------------ public
    def feed(self, samples: np.ndarray):
        """Feed mono float32 samples. Returns a new bpm (float) when the tempo changed, else None."""
        self.pending = np.concatenate((self.pending, samples))
        result = None

        while len(self.pending) >= self.hop:
            chunk = self.pending[:self.hop]
            self.pending = self.pending[self.hop:]
            self.loud.append(float(np.sqrt(np.mean(chunk * chunk))))
            self.audio = np.concatenate((self.audio[self.hop:], chunk))
            self.odf.append(self._frame(self.audio))
            self.frames_since += 1

            if self.frames_since >= self.update_every and len(self.odf) >= self.min_frames:
                self.frames_since = 0
                if self.loud and sum(self.loud) / len(self.loud) < 0.004:   # about -48 dBFS = silence
                    self.silent_updates += 1
                    if self.silent_updates >= 12:   # 12 x 0.25 s = 3 s
                        self.reset()
                        self.silence_flag = True
                        self.silent_updates = 0
                    continue
                self.silent_updates = 0
                est = self._estimate()
                if est is None:
                    continue
                self.raw.append(est)

                # accept only when the last estimates agree; a big jump (new song or a wrong fraction
                # like 2/3 of the tempo) has to stay stable longer than a small correction
                need = 3
                if self.published is not None and abs(est - self.published) / self.published > 0.06:
                    need = 5
                recent = list(self.raw)[-need:]
                if len(recent) == need and max(recent) - min(recent) <= 1.5:
                    value = sum(recent) / need
                    if self.published is None or abs(value - self.published) >= 0.8:
                        self.published = value
                        result = value
        return result
