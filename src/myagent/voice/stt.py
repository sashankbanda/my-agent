"""Speech-to-text: fast cloud transcription with a local fallback.

Two engines behind one interface, chosen by ``stt.engine`` in
config/voice.yaml:

- ``groq`` (default): Whisper on Groq's free tier. Measured ~5x faster than
  local CPU Whisper on the reference laptop (which needed ~1.8 s for a 5 s
  utterance - long enough to feel broken in conversation). Falls back to the
  local engine automatically when the network or the API fails, so voice keeps
  working offline.
- ``local``: faster-whisper on CPU (int8). Fully offline, slower.

**Privacy note (deliberate, documented):** the cloud engine uploads utterance
audio to Groq. The transcript of everything you say already goes there as
prompt text, so this widens exposure from text to voice, not to a new party -
but it is a real change, hence the one-line switch to ``local`` and the
architecture's "wake word and VAD are always local" rule stays intact: nothing
is uploaded until VAD has decided a complete utterance exists.
"""

from __future__ import annotations

import io
import wave
from pathlib import Path
from typing import Protocol

import numpy as np

from myagent.logging import get_logger
from myagent.voice.config import SAMPLE_RATE, SttSettings

log = get_logger(__name__)

GROQ_TRANSCRIBE_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
GROQ_TIMEOUT_S = 15.0
MIN_UTTERANCE_SAMPLES = SAMPLE_RATE // 4  # ignore sub-250ms blips


class SttEngine(Protocol):
    """What the pipeline needs from a transcriber."""

    def transcribe(self, audio: np.ndarray) -> str:
        """Text for one utterance (float32 mono @ 16 kHz). Blocking."""
        ...


def to_wav_bytes(audio: np.ndarray, sample_rate: int = SAMPLE_RATE) -> bytes:
    """Pack float32 mono samples into a 16-bit PCM WAV container."""
    pcm = np.clip(audio, -1.0, 1.0)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes((pcm * 32767).astype("<i2").tobytes())
    return buffer.getvalue()


class LocalTranscriber:
    """faster-whisper on CPU. Loaded lazily: it costs ~150 MB resident."""

    def __init__(self, settings: SttSettings, models_dir: Path) -> None:
        self._settings = settings
        self._models_dir = models_dir
        self._model = None

    def _ensure_model(self) -> object:
        if self._model is None:
            from faster_whisper import WhisperModel

            log.info("loading_local_stt", model=self._settings.model)
            self._model = WhisperModel(
                self._settings.model,
                device="cpu",
                compute_type=self._settings.compute_type,
                download_root=str(self._models_dir / "whisper"),
            )
        return self._model

    def transcribe(self, audio: np.ndarray) -> str:
        model = self._ensure_model()
        segments, _info = model.transcribe(  # type: ignore[attr-defined]
            audio,
            language="en",
            beam_size=1,  # greedy: latency matters more than the last 1% WER
            vad_filter=False,  # segmentation already happened upstream
            condition_on_previous_text=False,
        )
        return " ".join(segment.text.strip() for segment in segments).strip()


class GroqTranscriber:
    """Whisper via Groq's free API, with a local engine as backstop."""

    def __init__(self, settings: SttSettings, models_dir: Path) -> None:
        self._settings = settings
        self._fallback = LocalTranscriber(settings, models_dir)
        self._api_key: str | None = None

    def _key(self) -> str | None:
        if self._api_key is None:
            import keyring

            self._api_key = keyring.get_password("myagent", "groq_api_key") or ""
        return self._api_key or None

    def transcribe(self, audio: np.ndarray) -> str:
        key = self._key()
        if key is None:
            log.warning("groq_stt_no_key", action="using local engine")
            return self._fallback.transcribe(audio)
        import httpx

        try:
            response = httpx.post(
                GROQ_TRANSCRIBE_URL,
                headers={"Authorization": f"Bearer {key}"},
                files={"file": ("utterance.wav", to_wav_bytes(audio), "audio/wav")},
                data={
                    "model": self._settings.groq_model,
                    "language": "en",
                    "response_format": "json",
                    "temperature": "0",
                },
                timeout=GROQ_TIMEOUT_S,
            )
            response.raise_for_status()
            return str(response.json().get("text", "")).strip()
        except Exception as exc:  # network, quota, or API change: degrade, never fail
            log.warning("groq_stt_failed", error=str(exc)[:200], action="using local engine")
            return self._fallback.transcribe(audio)


class Transcriber:
    """The engine the pipeline talks to; ignores too-short audio."""

    def __init__(self, settings: SttSettings, models_dir: Path) -> None:
        self._engine: SttEngine = (
            GroqTranscriber(settings, models_dir)
            if settings.engine == "groq"
            else LocalTranscriber(settings, models_dir)
        )
        log.info("stt_engine", engine=settings.engine)

    def transcribe(self, audio: np.ndarray) -> str:
        if len(audio) < MIN_UTTERANCE_SAMPLES:
            return ""
        return self._engine.transcribe(audio)
