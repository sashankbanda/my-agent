"""Wake-word detection: openWakeWord over ONNX, always local.

The detector consumes 80 ms chunks (1280 samples @ 16 kHz) and reports when
the configured wake word crosses its threshold. A short refractory period
prevents one spoken wake word from triggering repeatedly.
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

import numpy as np

from myagent.voice.config import SAMPLE_RATE, WakeSettings

WAKE_CHUNK_SAMPLES = 1280  # 80 ms - openWakeWord's expected feed size
REFRACTORY_S = 2.0


# Non-wake helper models that openWakeWord's downloader places alongside the
# wake models; never load these as wake words.
_HELPER_MODEL_MARKERS = ("melspectrogram", "embedding", "silero_vad")


def is_wake_model_file(path: Path) -> bool:
    """True for an actual wake-word model file (not a feature/VAD helper)."""
    return path.suffix == ".onnx" and not any(
        marker in path.name for marker in _HELPER_MODEL_MARKERS
    )


def wake_model_files(wake_dir: Path) -> list[Path]:
    """All downloaded wake-word models in a directory."""
    if not wake_dir.exists():
        return []
    return [path for path in sorted(wake_dir.glob("*.onnx")) if is_wake_model_file(path)]


def resolve_wake_model(models_dir: Path, name: str) -> str:
    """Path of a downloaded wake model, or the bare name (package builtin)."""
    for candidate in wake_model_files(models_dir / "openwakeword"):
        if candidate.name.startswith(name):
            return str(candidate)
    return name


class WakeDetector:
    """Streaming wake-word scorer."""

    def __init__(self, settings: WakeSettings, models_dir: Path) -> None:
        from openwakeword.model import Model

        self._threshold = settings.threshold
        wake_dir = models_dir / "openwakeword"
        melspec = wake_dir / "melspectrogram.onnx"
        embedding = wake_dir / "embedding_model.onnx"
        if not melspec.exists() or not embedding.exists():
            raise FileNotFoundError(
                f"openWakeWord feature models missing in {wake_dir} - run: "
                "uv run python scripts/setup_voice.py"
            )
        self._model = Model(
            wakeword_models=[resolve_wake_model(models_dir, settings.model)],
            inference_framework="onnx",
            melspec_model_path=str(melspec),
            embedding_model_path=str(embedding),
        )
        self._refractory_until = 0.0
        self._clock_s = 0.0
        self.last_score = 0.0  # most recent chunk's score (mic-check tuning aid)

    def reset(self) -> None:
        """Clear streaming feature buffers (call after handling a wake-up)."""
        self._model.reset()

    def process(self, chunk: np.ndarray) -> bool:
        """Feed one 80 ms chunk (float32 [-1,1]); True on wake-word detection."""
        if chunk.shape != (WAKE_CHUNK_SAMPLES,):
            raise ValueError(f"expected {WAKE_CHUNK_SAMPLES} samples, got {chunk.shape}")
        self._clock_s += WAKE_CHUNK_SAMPLES / SAMPLE_RATE
        # openWakeWord expects int16-range samples.
        pcm16 = (chunk * 32767.0).astype(np.int16)
        # predict()'s stub type is imprecise; at runtime it returns {name: score}.
        # The key is the model FILE stem (e.g. "hey_jarvis_v0.1"), not the
        # configured name - and exactly one model is loaded, so take the max.
        scores = cast("dict[str, float]", self._model.predict(pcm16))
        score = max((float(value) for value in scores.values()), default=0.0)
        self.last_score = score
        if score >= self._threshold and self._clock_s >= self._refractory_until:
            self._refractory_until = self._clock_s + REFRACTORY_S
            return True
        return False
