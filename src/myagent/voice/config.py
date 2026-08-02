"""Voice satellite settings, loaded from ``config/voice.yaml``.

Independent of the kernel's Settings on purpose: the voice process owns its
own configuration surface and shares only the WebSocket contract.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

SAMPLE_RATE = 16_000  # Hz; VAD, wake word, and whisper all expect 16 kHz mono
FRAME_SAMPLES = 512  # 32 ms - the Silero VAD frame size at 16 kHz


class WakeSettings(BaseModel):
    """How the assistant decides you are talking to it.

    Two mechanisms, and which one runs depends on whether ``phrase`` is set.

    ``model`` is a *trained neural network* named by one of openWakeWord's
    pretrained words (alexa, hey_jarvis, hey_mycroft). Cheap - it scores every
    80 ms of audio on the CPU for nothing - but you cannot invent a new one by
    changing this string.

    ``phrase`` is any words you like. It costs more (short speech bursts are
    transcribed on-device to check them) and buys a wake word that is yours.
    """

    model: str = "hey_jarvis"
    threshold: float = 0.5
    # Any spoken phrase, e.g. "hey ev". Overrides `model` when set.
    phrase: str | None = None
    # How close the transcription must be to `phrase` (0-1). Speech-to-text
    # mangles short phrases, so this is forgiving by default.
    phrase_similarity: float = 0.72
    # Seconds of attention after the assistant STOPS SPEAKING. Every exchange
    # refreshes it, so a real back-and-forth never needs the wake word again.
    followup_window: float = 30.0


class VadSettings(BaseModel):
    threshold: float = 0.5
    min_speech_ms: int = 250
    silence_ms: int = 700
    max_utterance_s: int = 30


class SttSettings(BaseModel):
    # groq: fast cloud Whisper (falls back to local automatically on failure)
    # local: fully offline CPU Whisper, noticeably slower
    engine: Literal["groq", "local"] = "groq"
    groq_model: str = "whisper-large-v3-turbo"
    model: str = "base.en"  # local engine size: tiny.en | base.en | small.en
    compute_type: str = "int8"


class TtsSettings(BaseModel):
    # windows: WinRT built-in voices - instant on any CPU, no downloads.
    # kokoro:  near-human quality - needs a CPU that runs it at real time
    #          (benchmark on this machine: 7s to synthesize 3s of audio -> windows).
    engine: Literal["windows", "kokoro"] = "windows"
    windows_voice: str | None = None  # substring of a voice display name, e.g. "Zira"
    voice: str = "af_heart"  # kokoro voice id
    speed: float = 1.0


class VoiceSettings(BaseModel):
    """Root of the voice process configuration."""

    mode: Literal["wake", "ptt", "continuous"] = "wake"
    kernel_url: str = "ws://127.0.0.1:8765/voice"
    input_device: str | int | None = None
    output_device: str | int | None = None
    models_dir: Path | None = None
    wake: WakeSettings = Field(default_factory=WakeSettings)
    vad: VadSettings = Field(default_factory=VadSettings)
    stt: SttSettings = Field(default_factory=SttSettings)
    tts: TtsSettings = Field(default_factory=TtsSettings)

    def resolved_models_dir(self) -> Path:
        """Where voice models live; created on demand."""
        if self.models_dir is not None:
            return self.models_dir
        local_app_data = os.environ.get("LOCALAPPDATA")
        base = Path(local_app_data) if local_app_data else Path.home() / "AppData" / "Local"
        return base / "MyAgent" / "models"


def default_voice_config_path() -> Path:
    return Path(__file__).resolve().parents[3] / "config" / "voice.yaml"


def load_voice_settings(config_path: Path | None = None) -> VoiceSettings:
    """Load voice settings; defaults are complete if the file is absent."""
    path = config_path or default_voice_config_path()
    raw = {}
    if path.exists():
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        if loaded is not None:
            raw = loaded
    return VoiceSettings.model_validate(raw)
