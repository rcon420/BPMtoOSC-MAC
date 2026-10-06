import os
import sys
import time
import wave
import subprocess
from pathlib import Path
from threading import Thread

from time import time as wall_time   # wall clock: unlike time.monotonic it keeps counting while the Mac sleeps

import numpy as np
import pyaudio
from collections import deque
from aubio import tempo

from fasttempo import FastTempo


# local
import osc_client


SYSTEM_AUDIO = -1  # special device id: capture the Mac's system sound (ScreenCaptureKit helper)


def system_audio_helper():
    """Path to the compiled sckaudio helper, or None if not available (macOS only)"""
    if sys.platform != "darwin":
        return None
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    for path in (base / "sckaudio", Path(__file__).resolve().parent / "sckaudio"):
        if path.exists():
            return path
    return None


class BeatPrinter:
    def __init__(self):
        self.state: int = 0
        self.spinner = "▚▞"

    def print_bpm(self, bpm: float) -> None:
        print(f"{self.spinner[self.state]}\t{bpm:.1f} BPM")

        self.state = (self.state + 1) % len(self.spinner)


class BeatDetector:

    def __init__(self, client: osc_client.OSCclient, audio_device_index: int = None, parent=None, buf_size: int = 512):
        # arguments
        self.client = client  # OSC client
        self.audio_device_index = audio_device_index  # real PyAudio device index (None = system default)
        self.parent = parent  # MainFrame
        self.buf_size: int = buf_size  # buffer size

        # variables
        self.bpm = 0  # LIVE bpm: what the tracker hears right now (0 = nothing detected yet)
        self.detected = False
        self.sent_bpm = None  # SEND bpm: the value Resolume got (differs from LIVE while LOCK holds a big change)
        self.pending = None   # (bpm, since) = big change waiting for LOCK_SECONDS
        self.lock_label = ''
        self.beat_counter = 0  # for live beat divider
        self.level_queue = deque(maxlen=6)  # RMS Level queue
        self.stream = None

        if self.parent is None:
            self.spinner = BeatPrinter()

        # 'fast' = quick reacting estimator (default), 'aubio' = original aubio tempo (very slow to follow song changes)
        self.engine = 'fast'
        if self.parent is not None:
            self.engine = self.parent.config['AUDIO'].get('engine', 'fast').strip().lower()
        self.fast = None
        self.last_audio = wall_time()  # when the last block of audio arrived (watchdog)

        # debug mode:  BPM_DEBUG=1 python main_gui.py   (prints tempo candidates, saves ~/BPMtoOSC_capture.wav)
        self.debug = os.environ.get('BPM_DEBUG') == '1'
        self.capture = deque(maxlen=int(45 * 48000 / self.buf_size) + 1) if self.debug else None

        self.proc = None  # sckaudio helper process (system audio mode)
        self.reader = None
        self.p = None
        self.channels = 1

        if self.audio_device_index == SYSTEM_AUDIO:
            self._init_system_audio()
        else:
            self._init_pyaudio()

    def _init_system_audio(self):
        """Capture the Mac's internal sound through the sckaudio helper (mono float32, 48000 Hz)"""
        helper = system_audio_helper()
        if helper is None:
            raise RuntimeError("System audio helper 'sckaudio' not found. Build it first (see instructions).")

        self.SAMPLERATE = 48000
        self._make_trackers()

        self.proc = subprocess.Popen([str(helper)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        # if the helper quits right away, permission is missing or capture failed
        try:
            self.proc.wait(timeout=1.5)
            err = self.proc.stderr.read().decode(errors="replace").strip()
            self.proc = None
            raise RuntimeError(
                (err or "System audio capture stopped immediately.") +
                "\n\nAllow BPMtoOSC in System Settings > Privacy & Security > Screen & System Audio Recording, "
                "then restart the app.")
        except subprocess.TimeoutExpired:
            pass  # still running = good

        self.reader = Thread(target=self._read_loop, daemon=True)
        self.reader.start()

    def _make_trackers(self):
        self.tempo = tempo("default", self.buf_size * 2, self.buf_size, self.SAMPLERATE)  # aubio (beats)
        self.fast = FastTempo(self.SAMPLERATE)  # quick bpm estimate
        self.fast.debug = self.debug

    def _read_loop(self):
        """Reads audio from the helper in blocks of buf_size samples"""
        chunk = self.buf_size * 4  # float32
        proc = self.proc
        while proc is not None and proc.poll() is None:
            if self.parent is not None and not self.parent.running:
                break
            data = proc.stdout.read(chunk)
            if len(data) < chunk:
                break
            self._process(np.frombuffer(data, dtype=np.float32).copy())

    def _init_pyaudio(self):
        self.p = pyaudio.PyAudio()

        # Use the native sample rate of the device (macOS devices are often 48000 Hz)
        if self.audio_device_index is None:
            dev = self.p.get_default_input_device_info()
            self.audio_device_index = int(dev['index'])
        dev = self.p.get_device_info_by_index(self.audio_device_index)
        self.SAMPLERATE = int(dev.get('defaultSampleRate', 44100))
        max_ch = int(dev.get('maxInputChannels', 1))

        self._make_trackers()

        callback = self._GUI_callback if self.parent is not None else self._STANDALONE_callback

        # Try mono first, fall back to stereo (virtual devices like BlackHole) and downmix
        last_error = None
        for channels in (1, min(2, max_ch)):
            if channels < 1:
                continue
            try:
                self.channels = channels
                self.stream = self.p.open(
                    format=pyaudio.paFloat32,
                    channels=channels,
                    rate=self.SAMPLERATE,
                    input=True,
                    input_device_index=self.audio_device_index,
                    frames_per_buffer=self.buf_size,
                    stream_callback=callback
                )
                break
            except Exception as e:
                last_error = e
                self.stream = None
        if self.stream is None:
            self.p.terminate()
            self.p = None
            raise RuntimeError(f"Could not open audio device: {last_error}")

    def _to_mono(self, in_data) -> np.ndarray:
        signal = np.frombuffer(in_data, dtype=np.float32)
        if self.channels > 1:
            signal = signal.reshape(-1, self.channels).mean(axis=1)
        return np.ascontiguousarray(signal, dtype=np.float32)

    def _GUI_callback(self, in_data, frame_count, time_info, status):
        """Callback for pyaudio stream (runs in the audio thread)"""
        if not self.parent.running:
            return (None, pyaudio.paComplete)
        self._process(self._to_mono(in_data))
        return (None, pyaudio.paContinue)

    def _process(self, signal):
        """Beat detection + level meter + OSC for one block of mono float32 samples (audio / reader thread)"""
        self.last_audio = wall_time()
        try:
            if self.capture is not None:
                self.capture.append(signal.copy())

            # Level meter
            self.level_queue.append(int(np.sqrt(signal.dot(signal) / signal.size) * 200))

            # aubio: beat positions (LEDs) and, in 'aubio' engine mode, the bpm
            beat = self.tempo(signal)

            new_bpm = None
            if self.engine == 'aubio':
                if beat[0]:
                    new_bpm = round(self.tempo.get_bpm())
            else:
                fast_bpm = self.fast.feed(signal)
                if fast_bpm is not None:
                    new_bpm = round(fast_bpm)
                if self.fast.pop_silence():
                    # music stopped for a while: the next song is accepted at once (no lock)
                    self.sent_bpm = None
                    self.pending = None
                    self._set_lock_label('')

            if new_bpm is not None and 20 < new_bpm < 200 and new_bpm != self.bpm:
                self._publish(new_bpm)

            self._check_pending()

            # beat LEDs follow the (divided) beat while sync is on
            if beat[0] and self.parent.sync:
                self.beat_counter += 1
                if self.beat_counter % self.parent.beat_divider == 0:
                    self.parent.next_led()
                if self.beat_counter >= 4:
                    self.beat_counter = 0
        except Exception as e:
            print("Audio processing error:", e)

    # ------------------------------------------------------------------ LIVE / SEND / LOCK
    def _lock_settings(self):
        cfg = self.parent.config['OSC']
        on = cfg.get('LOCK', 'False') == 'True'
        try:
            percent = float(cfg.get('LOCK_PERCENT', '6'))
            seconds = float(cfg.get('LOCK_SECONDS', '10'))
        except ValueError:
            percent, seconds = 6.0, 10.0
        return on, percent, seconds

    def _publish(self, bpm: int):
        """The tracker heard a new tempo: show it in LIVE and decide what to SEND"""
        self.bpm = bpm
        self.detected = True
        self.parent.update_bpm_display(bpm, send_to="live", Blink=self.parent.sync)

        if not self.parent.sync:
            self.pending = None   # sync off: only the LIVE display follows the music
            return

        lock_on, percent, _ = self._lock_settings()
        small_change = self.sent_bpm is not None and abs(bpm - self.sent_bpm) * 100.0 / self.sent_bpm <= percent
        if (not lock_on) or self.sent_bpm is None or small_change:
            self.pending = None
            self._set_lock_label('')
            self._send(bpm)
        elif self.pending is None or abs(self.pending[0] - bpm) > 1.5:
            # big change: wait and see (a different value restarts the timer)
            self.pending = (bpm, time.monotonic())

    def _check_pending(self):
        """A big change is accepted when it stayed the live tempo for LOCK_SECONDS"""
        if self.pending is None:
            return
        if not self.parent.sync:
            self.pending = None
            self._set_lock_label('')
            return
        value, since = self.pending
        _, _, seconds = self._lock_settings()
        remaining = seconds - (time.monotonic() - since)
        if remaining <= 0:
            self.pending = None
            self._set_lock_label('')
            if abs(self.bpm - value) <= 1.5:
                self._send(value)
        else:
            self._set_lock_label(f"LOCK {int(remaining) + 1}")

    def _set_lock_label(self, text: str):
        if text != self.lock_label:
            self.lock_label = text
            self.parent.set_lock_status(text)

    def accept_live(self):
        """Jump to the live tempo right now (LOCK off, SYNC back on)"""
        self.pending = None
        self._set_lock_label('')
        if self.detected and self.bpm:
            self._send(self.bpm)

    def _send(self, bpm: int):
        """Update the SEND display and send the tempo to the target program"""
        old = self.sent_bpm
        self.sent_bpm = bpm
        self.parent.send_bpm = bpm
        divider = self.parent.beat_divider
        out_bpm = bpm // divider
        cfg = self.parent.config['OSC']
        self.client.send_osc(cfg['BPM_ADRESS'], out_bpm, map_to_resolume=cfg.get('RESOLUME', 'False'),
                             grandma3_master=cfg.get('GRANDMA3_MASTER', ''))
        self.parent.update_bpm_display(out_bpm, send_to="send", Blink=True)
        # real tempo change: let aubio find the new beat grid from scratch
        if old is None or abs(self.tempo.get_bpm() - bpm) > 4:
            self.tempo = tempo("default", self.buf_size * 2, self.buf_size, self.SAMPLERATE)

    def _STANDALONE_callback(self, in_data, frame_count, time_info, status):
        signal = self._to_mono(in_data)
        beat = self.tempo(signal)
        if beat[0]:
            self.spinner.print_bpm(self.tempo.get_bpm())
        return None, pyaudio.paContinue  # Tell pyAudio to continue

    def resync_bar(self):
        """Send resync command to Resolume"""
        self.client.send_osc(self.parent.config['OSC']['RESYNC_BAR_ADRESS'], 1)

    def problem(self):
        """None while the audio flows. Otherwise a short text saying what is wrong (used by the watchdog)."""
        now = wall_time()
        if self.audio_device_index == SYSTEM_AUDIO:
            if self.proc is None or self.proc.poll() is not None:
                return "the system audio helper stopped"
            if self.reader is not None and not self.reader.is_alive():
                return "the audio reader stopped"
            if now - self.last_audio > 20:
                return "no system audio for 20 seconds"
            return None
        if self.stream is None:
            return "the audio stream is closed"
        try:
            if not self.stream.is_active():
                return "the audio stream stopped"
        except Exception:
            return "the audio stream failed"
        if now - self.last_audio > 4:
            return "no audio data for 4 seconds"
        return None

    def save_capture(self):
        """Debug: writes the last 45 s the app heard to ~/BPMtoOSC_capture.wav"""
        if not getattr(self, 'capture', None):
            return
        try:
            data = np.concatenate(list(self.capture))
            pcm = (np.clip(data, -1, 1) * 32767).astype(np.int16)
            path = os.path.expanduser('~/BPMtoOSC_capture.wav')
            with wave.open(path, 'wb') as w:
                w.setnchannels(1); w.setsampwidth(2); w.setframerate(self.SAMPLERATE)
                w.writeframes(pcm.tobytes())
            print('Saved', path, flush=True)
        except Exception as e:
            print('Could not save capture:', e)
        self.capture = None

    def close(self):
        """Close pyaudio stream and terminate pyaudio"""
        self.save_capture()
        if getattr(self, 'proc', None) is not None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=2)
            except Exception:
                pass
            self.proc = None
        try:
            if getattr(self, 'stream', None) is not None:
                if self.stream.is_active():
                    self.stream.stop_stream()
                self.stream.close()
        except Exception:
            pass
        self.stream = None
        try:
            if getattr(self, 'p', None) is not None:
                self.p.terminate()
        except Exception:
            pass
        self.p = None

    def __del__(self):
        self.close()


if __name__ == "__main__":
    import time

    client = osc_client.OSCclient("127.0.0.1", 7000)
    bd = BeatDetector(client, buf_size=128)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        bd.close()
