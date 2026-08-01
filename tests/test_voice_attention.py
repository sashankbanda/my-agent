"""Conversational-attention tests: the wake word should be needed once.

Regression coverage for a real complaint: "every time I have to say the wake
word", and "if I interrupt while it is responding it does not work properly".
The attention rules are pure logic, so they are tested without audio hardware
by driving a Pipeline whose engines are stubbed out.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from myagent.voice.config import VoiceSettings
from myagent.voice.pipeline import ECHO_COOLDOWN_S, Pipeline


class FakeSpeaker:
    """Speaker stand-in whose playback state the test controls."""

    def __init__(self) -> None:
        self.active = False
        self.flushed = 0

    @property
    def is_active(self) -> bool:
        return self.active

    def flush(self) -> None:
        self.flushed += 1
        self.active = False


@pytest.fixture
def pipeline(monkeypatch: pytest.MonkeyPatch) -> Pipeline:
    """A Pipeline with audio/model construction bypassed."""
    monkeypatch.setattr(Pipeline, "_open_audio", lambda self, probe: None)
    for attribute in ("SileroVad", "SpeechSegmenter", "WakeDetector", "Transcriber"):
        monkeypatch.setattr(f"myagent.voice.pipeline.{attribute}", lambda *a, **k: object())
    monkeypatch.setattr("myagent.voice.pipeline.create_synthesizer", lambda *a, **k: object())
    instance = Pipeline(VoiceSettings(mode="wake"))
    instance.speaker = FakeSpeaker()  # type: ignore[assignment]
    return instance


def test_starts_unattending(pipeline: Pipeline) -> None:
    """Before the wake word, speech is not treated as a command."""
    assert pipeline._is_attending() is False


def test_wake_word_opens_attention(pipeline: Pipeline) -> None:
    pipeline._refresh_attention()
    assert pipeline._is_attending() is True


def test_attention_survives_a_long_spoken_reply(pipeline: Pipeline) -> None:
    """The window must start when SPEECH ends, not when text ends.

    This is the bug that forced a wake word every turn: a 30 s reply used to
    consume the entire follow-up window while the assistant was still talking.
    """
    speaker: Any = pipeline.speaker
    pipeline._attend_until = time.monotonic() - 1  # window already elapsed
    speaker.active = True
    pipeline._turn_active = True

    assert pipeline._is_attending() is True  # speaking keeps attention open

    # Playback finishes: the watcher's logic refreshes from *now*.
    speaker.active = False
    pipeline._turn_active = False
    pipeline._refresh_attention()
    assert pipeline._is_attending() is True
    assert pipeline._attend_until > time.monotonic() + 20  # a usable window


def test_attention_closes_after_real_silence(pipeline: Pipeline) -> None:
    pipeline._refresh_attention()
    pipeline._attend_until = time.monotonic() - 0.01  # simulate the window expiring
    assert pipeline._is_attending() is False


def test_thinking_keeps_attention_open(pipeline: Pipeline) -> None:
    """While the kernel is generating (no audio yet), stay attentive."""
    pipeline._attend_until = 0.0
    pipeline._turn_active = True
    assert pipeline._is_attending() is True


def test_followup_window_default_is_conversational() -> None:
    assert VoiceSettings().wake.followup_window >= 20


def test_continuous_mode_always_attends(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Pipeline, "_open_audio", lambda self, probe: None)
    for attribute in ("SileroVad", "SpeechSegmenter", "Transcriber"):
        monkeypatch.setattr(f"myagent.voice.pipeline.{attribute}", lambda *a, **k: object())
    monkeypatch.setattr("myagent.voice.pipeline.create_synthesizer", lambda *a, **k: object())
    instance = Pipeline(VoiceSettings(mode="continuous"))
    instance.speaker = FakeSpeaker()  # type: ignore[assignment]
    assert instance._is_attending() is True


def test_ptt_mode_needs_the_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Pipeline, "_open_audio", lambda self, probe: None)
    monkeypatch.setattr(Pipeline, "_install_ptt_hook", lambda self: None)
    for attribute in ("SileroVad", "SpeechSegmenter", "Transcriber"):
        monkeypatch.setattr(f"myagent.voice.pipeline.{attribute}", lambda *a, **k: object())
    monkeypatch.setattr("myagent.voice.pipeline.create_synthesizer", lambda *a, **k: object())
    instance = Pipeline(VoiceSettings(mode="ptt"))
    instance.speaker = FakeSpeaker()  # type: ignore[assignment]
    assert instance._is_attending() is False
    instance._set_ptt(True)
    assert instance._is_attending() is True


def test_echo_cooldown_is_short_but_present() -> None:
    """Long enough to swallow the speaker tail, short enough to feel instant."""
    assert 0.2 <= ECHO_COOLDOWN_S <= 1.0
