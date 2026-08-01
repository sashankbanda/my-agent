"""Voice pipeline unit tests: segmentation logic and audio helpers.

These run without any model on disk - SpeechSegmenter consumes probabilities,
not audio models. Real-model integration tests live in test_voice_models.py
and are gated on the models being downloaded.
"""

from __future__ import annotations

import numpy as np

from myagent.voice.audio import DeadAudioWatchdog, resample_linear
from myagent.voice.config import FRAME_SAMPLES, VadSettings, load_voice_settings
from myagent.voice.vad import SpeechSegmenter

FRAME = np.zeros(FRAME_SAMPLES, dtype=np.float32)
FRAME_MS = 32  # 512 samples @ 16 kHz


def make_segmenter(**overrides: int | float) -> SpeechSegmenter:
    settings = VadSettings(
        threshold=0.5,
        min_speech_ms=96,  # 3 frames
        silence_ms=160,  # 5 frames
        max_utterance_s=2,
        **overrides,  # type: ignore[arg-type]
    )
    return SpeechSegmenter(settings)


def feed(segmenter: SpeechSegmenter, probabilities: list[float]) -> list[float]:
    """Feed a probability script; return durations of emitted utterances."""
    emitted = []
    for probability in probabilities:
        utterance = segmenter.feed(FRAME, probability)
        if utterance is not None:
            emitted.append(utterance.duration_s)
    return emitted


def test_short_noise_burst_is_ignored() -> None:
    segmenter = make_segmenter()
    assert feed(segmenter, [0.9, 0.9] + [0.0] * 20) == []


def test_speech_then_silence_emits_one_utterance() -> None:
    segmenter = make_segmenter()
    emitted = feed(segmenter, [0.9] * 10 + [0.0] * 6)
    assert len(emitted) == 1


def test_pause_shorter_than_silence_window_does_not_split() -> None:
    segmenter = make_segmenter()
    script = [0.9] * 6 + [0.0] * 3 + [0.9] * 6 + [0.0] * 6  # mid-pause of 3 < 5 frames
    emitted = feed(segmenter, script)
    assert len(emitted) == 1


def test_two_utterances_with_long_gap() -> None:
    segmenter = make_segmenter()
    one = [0.9] * 6 + [0.0] * 6
    emitted = feed(segmenter, one + [0.0] * 10 + one)
    assert len(emitted) == 2


def test_overlong_utterance_is_force_closed() -> None:
    segmenter = make_segmenter()
    frames_for_2s = int(2000 / FRAME_MS) + 5
    emitted = feed(segmenter, [0.9] * frames_for_2s)
    assert len(emitted) == 1  # closed by max_utterance_s, not by silence


def test_pre_roll_is_included() -> None:
    segmenter = make_segmenter()
    marker = np.full(FRAME_SAMPLES, 0.5, dtype=np.float32)
    segmenter.feed(marker, 0.0)  # silence frame that lands in the pre-roll
    for _ in range(6):
        result = segmenter.feed(FRAME, 0.9)
    for _ in range(6):
        result = segmenter.feed(FRAME, 0.0)
        if result is not None:
            break
    assert result is not None
    assert result.audio.max() == 0.5  # the pre-roll marker frame survived


def test_in_speech_flag_tracks_state() -> None:
    segmenter = make_segmenter()
    assert segmenter.in_speech is False
    for _ in range(4):
        segmenter.feed(FRAME, 0.9)
    assert segmenter.in_speech is True


def test_resample_linear_changes_length_and_preserves_scale() -> None:
    tone = np.sin(np.linspace(0, 2 * np.pi * 10, 24_000)).astype(np.float32)
    out = resample_linear(tone, 24_000, 16_000)
    assert len(out) == 16_000
    assert abs(float(out.max()) - 1.0) < 0.05


def test_resample_same_rate_is_identity() -> None:
    tone = np.ones(100, dtype=np.float32)
    assert resample_linear(tone, 16_000, 16_000) is tone


def test_checked_in_voice_config_loads() -> None:
    settings = load_voice_settings()
    assert settings.mode in ("wake", "ptt", "continuous")
    assert settings.kernel_url.startswith("ws://")


class TestDeadAudioWatchdog:
    def make(self, trip_s: float = 6.0) -> tuple[DeadAudioWatchdog, list[float]]:
        clock = [1000.0]
        return DeadAudioWatchdog(trip_s, now=lambda: clock[0]), clock

    def test_live_audio_never_trips(self) -> None:
        watchdog, clock = self.make()
        for _ in range(100):
            clock[0] += 1.0
            assert watchdog.feed(0.05) is False

    def test_sustained_silence_trips_once(self) -> None:
        watchdog, clock = self.make(trip_s=6.0)
        tripped = []
        for _ in range(12):
            clock[0] += 1.0
            tripped.append(watchdog.feed(0.0))
        assert tripped.count(True) == 2  # at ~6s and ~12s, not every second

    def test_missing_frames_count_as_silence(self) -> None:
        watchdog, clock = self.make(trip_s=3.0)
        clock[0] += 3.0
        assert watchdog.feed(None) is True

    def test_live_frame_resets_the_clock(self) -> None:
        watchdog, clock = self.make(trip_s=5.0)
        clock[0] += 4.0
        assert watchdog.feed(0.0) is False
        clock[0] += 0.5
        assert watchdog.feed(0.2) is False  # speech arrives
        clock[0] += 4.0
        assert watchdog.feed(0.0) is False  # only 4s dead since the speech
