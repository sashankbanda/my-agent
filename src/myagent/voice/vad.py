"""Voice activity detection: Silero VAD over onnxruntime, plus segmentation.

Two parts with a deliberate seam:

- ``SileroVad`` wraps the official silero_vad.onnx model (downloaded by
  scripts/setup_voice.py) and turns one 512-sample frame into a speech
  probability.
- ``SpeechSegmenter`` is pure logic: it consumes (frame, probability) pairs
  and yields complete utterances. It takes probabilities, not a model, so
  its behavior is fully unit-testable without any model on disk.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import cast

import numpy as np

from myagent.voice.config import FRAME_SAMPLES, SAMPLE_RATE, VadSettings

SILERO_FILENAME = "silero_vad.onnx"


CONTEXT_SAMPLES = 64  # Silero v5 expects 64 samples of leading context per frame


class SileroVad:
    """Frame-level speech probability from the Silero VAD v5 ONNX model.

    The v5 graph takes each 512-sample frame *prefixed with the last 64
    samples of the previous frame* - feeding bare frames silently yields
    near-zero probabilities (verified empirically: 0.11 vs 1.00 on speech).
    """

    def __init__(self, models_dir: Path) -> None:
        import onnxruntime

        model_path = models_dir / SILERO_FILENAME
        if not model_path.exists():
            raise FileNotFoundError(
                f"{model_path} not found - run: uv run python scripts/setup_voice.py"
            )
        options = onnxruntime.SessionOptions()
        options.inter_op_num_threads = 1
        options.intra_op_num_threads = 1
        self._session = onnxruntime.InferenceSession(
            str(model_path), options, providers=["CPUExecutionProvider"]
        )
        self.reset()

    def reset(self) -> None:
        """Clear recurrent state and context (call between utterances)."""
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._context = np.zeros(CONTEXT_SAMPLES, dtype=np.float32)

    def __call__(self, frame: np.ndarray) -> float:
        """Speech probability for one 512-sample float32 mono frame."""
        if frame.shape != (FRAME_SAMPLES,):
            raise ValueError(f"expected {FRAME_SAMPLES} samples, got {frame.shape}")
        stitched = np.concatenate([self._context, frame.astype(np.float32)])
        outputs = self._session.run(
            None,
            {
                "input": stitched.reshape(1, -1),
                "state": self._state,
                "sr": np.array(SAMPLE_RATE, dtype=np.int64),
            },
        )
        probability = cast("np.ndarray", outputs[0])
        self._state = cast("np.ndarray", outputs[1])
        self._context = frame[-CONTEXT_SAMPLES:].astype(np.float32)
        return float(probability[0][0])


@dataclass
class Utterance:
    """One detected stretch of speech."""

    audio: np.ndarray  # float32 mono @ 16 kHz
    duration_s: float


class SpeechSegmenter:
    """Turn a stream of (frame, speech-probability) into utterances.

    State machine: SILENCE -> (enough consecutive speech) -> SPEECH ->
    (enough consecutive silence) -> emit utterance -> SILENCE. A small
    pre-roll of frames before the trigger is included so initial consonants
    are not clipped.
    """

    PRE_ROLL_FRAMES = 8  # ~0.25 s of audio kept from before speech triggered

    def __init__(self, settings: VadSettings) -> None:
        frame_ms = FRAME_SAMPLES * 1000 / SAMPLE_RATE
        self._threshold = settings.threshold
        self._min_speech_frames = max(1, int(settings.min_speech_ms / frame_ms))
        self._silence_frames_needed = max(1, int(settings.silence_ms / frame_ms))
        self._max_frames = int(settings.max_utterance_s * 1000 / frame_ms)
        self.reset()

    def reset(self) -> None:
        self._in_speech = False
        self._speech_run = 0
        self._silence_run = 0
        self._pre_roll: list[np.ndarray] = []
        self._frames: list[np.ndarray] = []

    @property
    def in_speech(self) -> bool:
        """True while inside a (candidate) utterance - used for barge-in."""
        return self._in_speech

    def feed(self, frame: np.ndarray, probability: float) -> Utterance | None:
        """Consume one frame; return an Utterance when one completes."""
        is_speech = probability >= self._threshold

        if not self._in_speech:
            self._pre_roll.append(frame)
            if len(self._pre_roll) > self.PRE_ROLL_FRAMES:
                self._pre_roll.pop(0)
            if is_speech:
                self._speech_run += 1
                if self._speech_run >= self._min_speech_frames:
                    self._in_speech = True
                    self._frames = list(self._pre_roll)
                    self._silence_run = 0
            else:
                self._speech_run = 0
            return None

        self._frames.append(frame)
        if is_speech:
            self._silence_run = 0
        else:
            self._silence_run += 1

        ended = self._silence_run >= self._silence_frames_needed
        overlong = len(self._frames) >= self._max_frames
        if not (ended or overlong):
            return None

        audio = np.concatenate(self._frames)
        utterance = Utterance(audio=audio, duration_s=len(audio) / SAMPLE_RATE)
        self.reset()
        return utterance
