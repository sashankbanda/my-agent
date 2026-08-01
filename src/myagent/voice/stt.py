"""Speech-to-text: faster-whisper on CPU (int8).

Models download once into the voice models directory (setup_voice.py
prefetches; otherwise the first use downloads). English-only ``*.en`` models
are the speed/quality sweet spot for a CPU voice loop; swap the size in
config/voice.yaml.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from myagent.voice.config import SttSettings


class Transcriber:
    """Blocking transcription; callers run it in a worker thread."""

    def __init__(self, settings: SttSettings, models_dir: Path) -> None:
        from faster_whisper import WhisperModel

        self._model = WhisperModel(
            settings.model,
            device="cpu",
            compute_type=settings.compute_type,
            download_root=str(models_dir / "whisper"),
        )

    def transcribe(self, audio: np.ndarray) -> str:
        """Text for one utterance (float32 mono @ 16 kHz)."""
        segments, _info = self._model.transcribe(
            audio,
            language="en",
            beam_size=1,  # greedy: latency matters more than the last 1% WER
            vad_filter=False,  # segmentation already happened upstream
            condition_on_previous_text=False,
        )
        return " ".join(segment.text.strip() for segment in segments).strip()
