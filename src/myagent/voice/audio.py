"""Audio I/O: microphone capture and interruptible playback.

Capture: 16 kHz mono float32 in 512-sample frames (the VAD frame size).
Playback: a pull-based ring buffer behind an OutputStream callback - which is
what makes barge-in instant: ``flush()`` empties the buffer and playback
stops within one audio block (~20 ms), no thread teardown involved.
"""

from __future__ import annotations

import contextlib
import queue
import threading
import time
from collections.abc import Callable

import numpy as np
import sounddevice as sd

from myagent.voice.config import FRAME_SAMPLES, SAMPLE_RATE


def resample_linear(samples: np.ndarray, source_rate: int, target_rate: int) -> np.ndarray:
    """Mono linear-interpolation resampling (good enough for speech playback)."""
    if source_rate == target_rate:
        return samples
    target_length = int(len(samples) * target_rate / source_rate)
    positions = np.linspace(0, len(samples) - 1, target_length)
    return np.interp(positions, np.arange(len(samples)), samples).astype(np.float32)


class MicStream:
    """Blocking 512-sample @ 16 kHz frame source from any input device.

    Capture happens at the device's *native* rate (WASAPI shared mode does
    not resample; forcing 16 kHz fails with "Invalid sample rate" on it) and
    is resampled down here, then re-chunked into exact VAD-sized frames.
    """

    def __init__(self, device: str | int | None = None) -> None:
        self._frames: queue.Queue[np.ndarray] = queue.Queue(maxsize=256)
        device_info = sd.query_devices(device if device is not None else sd.default.device[0])
        self._native_rate = int(device_info["default_samplerate"])
        native_block = int(FRAME_SAMPLES * self._native_rate / SAMPLE_RATE)
        self._pending = np.zeros(0, dtype=np.float32)
        self._stream = sd.InputStream(
            samplerate=self._native_rate,
            channels=1,
            dtype="float32",
            blocksize=native_block,
            device=device,
            callback=self._on_audio,
        )

    def _on_audio(
        self, indata: np.ndarray, _frames: int, _time: object, status: sd.CallbackFlags
    ) -> None:
        if status:
            # Overflows are visible to the pipeline as get() starvation; never
            # raise inside the audio callback (it must not fail).
            pass
        resampled = resample_linear(indata[:, 0], self._native_rate, SAMPLE_RATE)
        self._pending = np.concatenate([self._pending, resampled])
        while len(self._pending) >= FRAME_SAMPLES:
            frame = self._pending[:FRAME_SAMPLES].copy()
            self._pending = self._pending[FRAME_SAMPLES:]
            # A full queue means the pipeline is stalled; dropping frames lets
            # it recover instead of blocking the audio callback.
            with contextlib.suppress(queue.Full):
                self._frames.put_nowait(frame)

    def start(self) -> None:
        self._stream.start()

    def stop(self) -> None:
        self._stream.stop()
        self._stream.close()

    def get(self, timeout: float = 1.0) -> np.ndarray | None:
        """Next 512-sample frame, or None on timeout."""
        try:
            return self._frames.get(timeout=timeout)
        except queue.Empty:
            return None


class Speaker:
    """Interruptible playback sink.

    Opens at the output device's native rate (WASAPI-safe, like MicStream);
    ``play`` resamples whatever the TTS engine produced onto it.
    """

    def __init__(self, device: str | int | None = None) -> None:
        device_info = sd.query_devices(device if device is not None else sd.default.device[1])
        self._rate = int(device_info["default_samplerate"])
        self._lock = threading.Lock()
        self._buffer = np.zeros(0, dtype=np.float32)
        self._stream = sd.OutputStream(
            samplerate=self._rate,
            channels=1,
            dtype="float32",
            device=device,
            callback=self._on_pull,
        )

    def _on_pull(self, outdata: np.ndarray, frames: int, _time: object, _status: object) -> None:
        with self._lock:
            take = min(frames, len(self._buffer))
            outdata[:take, 0] = self._buffer[:take]
            outdata[take:, 0] = 0.0
            self._buffer = self._buffer[take:]

    def start(self) -> None:
        self._stream.start()

    def stop(self) -> None:
        self._stream.stop()
        self._stream.close()

    def play(self, samples: np.ndarray, source_rate: int) -> None:
        """Queue samples for playback (resampled to the device rate)."""
        prepared = resample_linear(samples, source_rate, self._rate)
        with self._lock:
            self._buffer = np.concatenate([self._buffer, prepared])

    def flush(self) -> None:
        """Barge-in: stop speaking within one audio block."""
        with self._lock:
            self._buffer = np.zeros(0, dtype=np.float32)

    @property
    def is_active(self) -> bool:
        """True while there is queued audio still being spoken."""
        with self._lock:
            return len(self._buffer) > 0


def list_devices() -> str:
    """Human-readable device table for --list-devices."""
    return str(sd.query_devices())


def reinitialize_portaudio() -> None:
    """Re-enumerate audio devices.

    PortAudio takes its device snapshot at initialization; devices plugged in
    or connected later are invisible until this runs. All streams must be
    closed first. (sd._terminate/_initialize is the documented sounddevice
    idiom for hot-plug support - there is no public API for it.)
    """
    sd._terminate()
    sd._initialize()


# Virtual pass-through endpoints: they forward to the system default and mask
# the real device, so probing them tells us nothing - always skip.
_VIRTUAL_DEVICE_MARKERS = ("sound mapper", "primary sound")


def probe_live_input(seconds: float = 1.0) -> int | None:
    """Find the input device that actually hears something.

    A connected-but-idle Bluetooth headset delivers *digital* silence
    (exactly 0.0) while any live microphone picks up ambient noise, so the
    device with the highest peak over a short capture is the one really
    listening. Returns a device index, or None if everything is silent
    (then the system default is the best remaining guess).
    """
    best_index: int | None = None
    best_peak = 1e-4  # below this is digital silence, not a live mic
    for index, device in enumerate(sd.query_devices()):
        if device["max_input_channels"] < 1 or device["hostapi"] != sd.default.hostapi:
            continue
        if any(marker in device["name"].lower() for marker in _VIRTUAL_DEVICE_MARKERS):
            continue
        try:
            rate = int(device["default_samplerate"])
            recording = sd.rec(
                int(seconds * rate), samplerate=rate, channels=1, dtype="float32", device=index
            )
            sd.wait()
            peak = float(np.abs(recording).max())
        except sd.PortAudioError:
            continue
        if peak > best_peak:
            best_peak = peak
            best_index = index
    return best_index


class DeadAudioWatchdog:
    """Detect a capture stream that runs but hears only digital silence.

    A live microphone always picks up ambient noise; exactly-zero input for
    ``trip_seconds`` straight is the signature of a device that was switched
    away, unplugged, or parked in its case. Time-based (not frame-counted) so
    missing frames and timeouts weigh correctly.
    """

    SILENCE_PEAK = 1e-4

    def __init__(self, trip_seconds: float, now: Callable[[], float] = time.monotonic) -> None:
        self._trip_seconds = trip_seconds
        self._now = now
        self._last_live = now()

    def feed(self, peak: float | None) -> bool:
        """Record one observation (None = no frame arrived); True = tripped."""
        now = self._now()
        if peak is not None and peak > self.SILENCE_PEAK:
            self._last_live = now
            return False
        if now - self._last_live >= self._trip_seconds:
            self._last_live = now  # avoid instant re-trip while recovering
            return True
        return False
