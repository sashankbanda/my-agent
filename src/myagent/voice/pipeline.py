"""The voice pipeline: mic -> wake/VAD -> STT -> kernel -> TTS -> speaker.

Concurrency model: three asyncio tasks over blocking engines run in worker
threads -

- listen():  mic frames -> wake gating -> segmentation -> transcribe -> send
- receive(): kernel frames -> sentence queue / turn bookkeeping
- speak():   sentence queue -> synthesize (thread) -> speaker buffer

Barge-in: while the speaker is active, sustained mic speech flushes playback
and sends ``cancel`` to the kernel; the interrupting speech then becomes the
next utterance. Without echo cancellation the assistant can hear itself on
open speakers, so barge-in requires a longer sustained-speech run than normal
triggering (BARGE_IN_MS); a headset makes it precise. AEC is future work.

Attention model (wake mode) - designed to feel like a conversation, not a
command line:

- the wake word opens attention
- attention *stays* open while the assistant is speaking and for
  ``wake.followup_window`` seconds after playback actually finishes, so
  replying needs no wake word
- every exchange refreshes that window, so a real back-and-forth continues
  indefinitely; it only closes after genuine silence

Echo handling (no AEC yet): the mic hears the assistant through open
speakers, so while playback is active the audio feeds ONLY the barge-in
detector - never the segmenter. Otherwise the assistant transcribes itself and
answers its own words. A short cooldown after playback absorbs the tail.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time

import numpy as np
import websockets

from myagent.logging import get_logger
from myagent.voice.audio import (
    DeadAudioWatchdog,
    MicStream,
    Speaker,
    probe_live_input,
    reinitialize_portaudio,
)
from myagent.voice.config import FRAME_SAMPLES, SAMPLE_RATE, VoiceSettings
from myagent.voice.stt import Transcriber
from myagent.voice.tts import create_synthesizer
from myagent.voice.vad import SileroVad, SpeechSegmenter
from myagent.voice.wake import WAKE_CHUNK_SAMPLES, WakeDetector

log = get_logger(__name__)

BARGE_IN_MS = 500  # sustained speech needed to interrupt playback (echo guard)
ECHO_COOLDOWN_S = 0.4  # ignore the mic briefly after playback (speaker tail)
RECONNECT_DELAY_S = 3.0
DEAD_INPUT_TRIP_S = 6.0  # this much digital silence -> re-probe input devices
IDLE_RESYNC_S = 30.0  # while idle, re-open streams so they follow device switches


class Pipeline:
    """One running voice satellite."""

    def __init__(self, settings: VoiceSettings) -> None:
        self.settings = settings
        models_dir = settings.resolved_models_dir()
        self.vad = SileroVad(models_dir)
        self.segmenter = SpeechSegmenter(settings.vad)
        self.wake = WakeDetector(settings.wake, models_dir) if settings.mode == "wake" else None
        self.stt = Transcriber(settings.stt, models_dir)
        self.tts = create_synthesizer(settings.tts, models_dir)

        self._audio_lock = asyncio.Lock()  # guards stream swap vs playback
        self._device_override: int | None = None  # last probe-picked input
        self._last_resync = time.monotonic()
        self._open_audio(probe=settings.input_device is None)

        self._sentences: asyncio.Queue[str] = asyncio.Queue()
        self._attend_until = 0.0  # attention deadline; refreshed by every exchange
        self._turn_active = False  # a reply is being generated or spoken
        self._quiet_until = 0.0  # echo cooldown after playback stops
        self._wake_buffer = np.zeros(0, dtype=np.float32)
        self._barge_run_frames = 0
        self._barge_needed = max(1, int(BARGE_IN_MS * SAMPLE_RATE / 1000 / FRAME_SAMPLES))
        self._ptt_down = False
        if settings.mode == "ptt":
            self._install_ptt_hook()

    # -- audio device lifecycle ---------------------------------------------

    def _pick_input(self, probe: bool) -> str | int | None:
        """Input device policy: explicit pin > live-signal probe > default."""
        if self.settings.input_device is not None:
            return self.settings.input_device
        if probe:
            import sounddevice as sd

            self._device_override = probe_live_input()
            picked = (
                sd.query_devices(self._device_override)["name"]
                if self._device_override is not None
                else "system default (no live mic found by probe)"
            )
            log.info("input_probe", picked=picked)
        return self._device_override  # None -> system default

    def _open_audio(self, probe: bool) -> None:
        device = self._pick_input(probe)
        self.mic = MicStream(device)
        self.speaker = Speaker(self.settings.output_device)
        self.mic.start()
        self.speaker.start()
        self._last_resync = time.monotonic()

    def _close_audio(self) -> None:
        for stream in (self.mic, self.speaker):
            with contextlib.suppress(Exception):  # closing a dead device must not crash us
                stream.stop()

    async def _rebuild_audio(self, reason: str, probe: bool) -> None:
        """Reopen both streams on freshly enumerated devices.

        Devices plugged in after startup are invisible to PortAudio until it
        reinitializes, so this is what makes the satellite *follow* the
        user's device switches instead of clinging to a stale endpoint.
        """
        log.info("audio_rebuild", reason=reason, probe=probe)
        async with self._audio_lock:
            await asyncio.to_thread(self._close_audio)
            await asyncio.to_thread(reinitialize_portaudio)
            await asyncio.to_thread(self._open_audio, probe)
        self.segmenter.reset()
        self.vad.reset()
        if self.wake is not None:
            self.wake.reset()
        self._wake_buffer = np.zeros(0, dtype=np.float32)

    # -- attention ---------------------------------------------------------

    def _is_attending(self) -> bool:
        """Whether speech should be treated as directed at the assistant."""
        if self.settings.mode == "continuous":
            return True
        if self.settings.mode == "ptt":
            return self._ptt_down
        # Stay attentive through the whole exchange: while the assistant is
        # working or speaking, and for the follow-up window after it stops.
        return self._turn_active or self.speaker.is_active or time.monotonic() < self._attend_until

    def _refresh_attention(self) -> None:
        """Extend the no-wake-word window from *now* (called when speech ends)."""
        self._attend_until = time.monotonic() + self.settings.wake.followup_window

    def _install_ptt_hook(self) -> None:
        import keyboard  # global hotkey hook (Windows-friendly)

        key = "ctrl+space"
        keyboard.on_press_key("space", lambda e: self._set_ptt(keyboard.is_pressed("ctrl")))
        keyboard.on_release_key("space", lambda e: self._set_ptt(False))
        log.info("ptt_ready", key=key)

    def _set_ptt(self, down: bool) -> None:
        self._ptt_down = down

    # -- the three tasks ----------------------------------------------------

    async def listen(self, socket: websockets.ClientConnection) -> None:
        """Mic frames -> wake gating -> segmentation -> STT -> kernel.

        Also owns audio-device health: dead input triggers a probe-rebuild,
        and quiet periods trigger a resync so streams follow the system's
        current devices (Bluetooth/wired switches after startup).
        """
        watchdog = DeadAudioWatchdog(DEAD_INPUT_TRIP_S)
        while True:
            frame = await asyncio.to_thread(self.mic.get, 1.0)

            if watchdog.feed(None if frame is None else float(np.abs(frame).max())):
                await self._rebuild_audio("input went silent", probe=True)
                continue
            idle = not self.speaker.is_active and not self.segmenter.in_speech
            if idle and time.monotonic() - self._last_resync >= IDLE_RESYNC_S:
                await self._rebuild_audio("periodic idle resync", probe=False)
                continue
            if frame is None:
                continue

            if self.wake is not None and not self._is_attending():
                self._feed_wake(frame)
                continue

            probability = await asyncio.to_thread(self.vad, frame)

            # While the assistant speaks, this audio is mostly its own voice.
            # Use it ONLY to detect barge-in; never segment it as user speech.
            if self.speaker.is_active:
                if probability >= self.settings.vad.threshold:
                    self._barge_run_frames += 1
                else:
                    self._barge_run_frames = 0
                if self._barge_run_frames >= self._barge_needed:
                    log.info("barge_in")
                    self.speaker.flush()
                    self._drain_sentences()
                    await socket.send(json.dumps({"type": "cancel"}))
                    self._barge_run_frames = 0
                    self._turn_active = False
                    # Start listening cleanly: the user is mid-sentence, and
                    # everything captured so far is echo.
                    self._quiet_until = time.monotonic() + ECHO_COOLDOWN_S
                    self.segmenter.reset()
                    self.vad.reset()
                continue

            # Speaker tail after playback: still echo, not the user.
            if time.monotonic() < self._quiet_until:
                continue

            utterance = self.segmenter.feed(frame, probability)
            if utterance is None:
                continue
            self.vad.reset()
            self._refresh_attention()  # keep the window open while transcribing

            text = await asyncio.to_thread(self.stt.transcribe, utterance.audio)
            if not text:
                continue
            log.info("utterance", text=text, seconds=round(utterance.duration_s, 2))
            self._turn_active = True
            await socket.send(json.dumps({"type": "utterance", "text": text}))

    def _feed_wake(self, frame: np.ndarray) -> None:
        assert self.wake is not None
        self._wake_buffer = np.concatenate([self._wake_buffer, frame])
        while len(self._wake_buffer) >= WAKE_CHUNK_SAMPLES:
            chunk = self._wake_buffer[:WAKE_CHUNK_SAMPLES]
            self._wake_buffer = self._wake_buffer[WAKE_CHUNK_SAMPLES:]
            if self.wake.process(chunk):
                log.info("wake_word")
                self._refresh_attention()
                self.segmenter.reset()
                self.vad.reset()

    async def receive(self, socket: websockets.ClientConnection) -> None:
        """Kernel frames -> sentence queue / attention bookkeeping."""
        async for raw in socket:
            frame = json.loads(raw)
            kind = frame.get("type")
            if kind == "say":
                await self._sentences.put(frame["text"])
            elif kind == "turn_done":
                # The reply text is complete, but speech is still playing; the
                # follow-up window starts when the *audio* finishes (see
                # playback_watcher), so do not start it here.
                self._turn_active = False
                self._refresh_attention()
            elif kind == "error":
                log.warning("kernel_error", message=frame.get("message"))
                self._turn_active = False
                self._refresh_attention()
            elif kind == "session":
                log.info("session", session_id=frame.get("session_id"))

    async def speak(self) -> None:
        """Sentence queue -> synthesis -> speaker."""
        while True:
            sentence = await self._sentences.get()
            samples, rate = await asyncio.to_thread(self.tts.synthesize, sentence)
            async with self._audio_lock:  # never play into a mid-rebuild stream
                self.speaker.play(samples, rate)

    async def playback_watcher(self) -> None:
        """Start the follow-up window when speech actually stops.

        Without this the window is measured from the end of the *text*, so a
        long spoken reply eats it entirely and the user has to say the wake
        word again - the single biggest reason voice felt un-conversational.
        """
        was_active = False
        while True:
            active = self.speaker.is_active
            if was_active and not active:
                self._quiet_until = time.monotonic() + ECHO_COOLDOWN_S
                self._refresh_attention()
                self.segmenter.reset()
                self.vad.reset()
                log.debug("listening_again", seconds=self.settings.wake.followup_window)
            was_active = active
            await asyncio.sleep(0.1)

    def _drain_sentences(self) -> None:
        while not self._sentences.empty():
            with contextlib.suppress(asyncio.QueueEmpty):
                self._sentences.get_nowait()

    # -- lifecycle -----------------------------------------------------------

    async def run(self) -> None:
        """Run forever, reconnecting to the kernel when it restarts."""
        log.info("voice_ready", mode=self.settings.mode)
        try:
            while True:
                try:
                    async with websockets.connect(self.settings.kernel_url) as socket:
                        log.info("kernel_connected", url=self.settings.kernel_url)
                        tasks = [
                            asyncio.create_task(self.listen(socket)),
                            asyncio.create_task(self.receive(socket)),
                            asyncio.create_task(self.speak()),
                            asyncio.create_task(self.playback_watcher()),
                        ]
                        done, pending = await asyncio.wait(
                            tasks, return_when=asyncio.FIRST_EXCEPTION
                        )
                        for task in pending:
                            task.cancel()
                        for task in done:
                            exc = task.exception()
                            if exc is not None and not isinstance(exc, websockets.ConnectionClosed):
                                raise exc
                except (OSError, websockets.WebSocketException) as exc:
                    log.warning("kernel_unreachable", error=str(exc))
                    await asyncio.sleep(RECONNECT_DELAY_S)
        finally:
            self._close_audio()
