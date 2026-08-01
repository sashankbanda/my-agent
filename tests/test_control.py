"""Stop and mute: the two controls a person needs mid-conversation.

Both were real complaints. "It is not stopping properly" - the only stop in
the UI was the kill switch, which blocks future actions but never silenced the
sentence being spoken. And "when I speak to my neighbours it takes that as
input" - there was no way to close the microphone at all.
"""

from __future__ import annotations

import asyncio
import sqlite3
from typing import Any

import pytest

from myagent.config import Settings
from myagent.server.control import TurnRegistry, VoiceLink
from myagent.voice.config import VoiceSettings
from myagent.voice.pipeline import SLEEP_PHRASES, Pipeline
from tests.test_chat_api import make_client


class RecordingSocket:
    """Stands in for the voice satellite's WebSocket."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_text(self, payload: str) -> None:
        self.sent.append(payload)


class DeadSocket:
    """A socket that has closed underneath us."""

    async def send_text(self, payload: str) -> None:
        raise RuntimeError("connection closed")


class TestTurnRegistry:
    def test_cancel_all_signals_every_live_turn(self) -> None:
        registry = TurnRegistry()
        first, second = asyncio.Event(), asyncio.Event()
        registry.register(first)
        registry.register(second)

        assert registry.cancel_all() == 2
        assert first.is_set() and second.is_set()

    def test_already_cancelled_turns_are_not_counted_twice(self) -> None:
        registry = TurnRegistry()
        cancel = asyncio.Event()
        registry.register(cancel)
        registry.cancel_all()

        assert registry.cancel_all() == 0, "a second stop must not double-count"

    def test_finished_turns_are_forgotten(self) -> None:
        registry = TurnRegistry()
        cancel = asyncio.Event()
        registry.register(cancel)
        registry.discard(cancel)

        assert registry.active == 0
        assert registry.cancel_all() == 0


class TestVoiceLink:
    async def test_send_reaches_the_attached_satellite(self) -> None:
        link = VoiceLink()
        socket = RecordingSocket()
        link.attach(socket)  # type: ignore[arg-type]

        assert await link.send({"type": "stop"}) is True
        assert socket.sent == ['{"type": "stop"}']

    async def test_send_without_a_satellite_reports_failure(self) -> None:
        """No voice process: stop still succeeds, it just silences nothing."""
        assert await VoiceLink().send({"type": "stop"}) is False

    async def test_a_dead_socket_does_not_raise(self) -> None:
        link = VoiceLink()
        link.attach(DeadSocket())  # type: ignore[arg-type]
        assert await link.send({"type": "stop"}) is False

    def test_detach_ignores_a_superseded_socket(self) -> None:
        """A reconnect must not be unhooked by the old connection's cleanup."""
        link = VoiceLink()
        old, new = RecordingSocket(), RecordingSocket()
        link.attach(old)  # type: ignore[arg-type]
        link.attach(new)  # type: ignore[arg-type]
        link.detach(old)  # type: ignore[arg-type]

        assert link.connected is True


class TestStopEndpoint:
    def test_stop_is_safe_with_nothing_running(self, settings: Settings) -> None:
        with make_client(settings, {}) as client:
            response = client.post("/stop")
        assert response.status_code == 200
        assert response.json() == {"stopped": 0, "silenced": False}

    def test_stop_cancels_an_in_flight_turn(
        self, settings: Settings, db: sqlite3.Connection
    ) -> None:
        with make_client(settings, {}) as client:
            cancel = asyncio.Event()
            client.app.state.turns.register(cancel)  # type: ignore[attr-defined]

            assert client.post("/stop").json()["stopped"] == 1
            assert cancel.is_set()

        types = [row["type"] for row in db.execute("SELECT type FROM events ORDER BY id")]
        assert "UserStopped" in types  # the audit log records who stopped what

    def test_stop_is_not_the_kill_switch(self, settings: Settings) -> None:
        """Stopping an answer must not leave the assistant unable to act."""
        with make_client(settings, {}) as client:
            client.post("/stop")
            assert client.get("/kill").json()["engaged"] is False


class TestMuteEndpoint:
    def test_mute_defaults_to_off_and_toggles(self, settings: Settings) -> None:
        with make_client(settings, {}) as client:
            assert client.get("/voice/mute").json()["muted"] is False
            assert client.post("/voice/mute").json()["muted"] is True
            assert client.get("/voice/mute").json()["muted"] is True
            assert client.post("/voice/mute").json()["muted"] is False

    def test_mute_can_be_set_explicitly(self, settings: Settings) -> None:
        """Idempotent set, so a UI can send state rather than a toggle."""
        with make_client(settings, {}) as client:
            assert client.post("/voice/mute", json={"muted": True}).json()["muted"] is True
            assert client.post("/voice/mute", json={"muted": True}).json()["muted"] is True

    def test_mute_shows_up_in_status(self, settings: Settings) -> None:
        with make_client(settings, {}) as client:
            client.post("/voice/mute", json={"muted": True})
            assert client.get("/status").json()["voice"]["muted"] is True

    def test_mute_command_is_sent_to_the_satellite(self, settings: Settings) -> None:
        with make_client(settings, {}) as client:
            socket = RecordingSocket()
            client.app.state.voice_link.attach(socket)  # type: ignore[attr-defined]

            assert client.post("/voice/mute", json={"muted": True}).json()["delivered"] is True
        assert socket.sent == ['{"type": "set_mute", "value": true}']


@pytest.fixture
def pipeline(monkeypatch: pytest.MonkeyPatch) -> Pipeline:
    """A Pipeline with audio, models, and the keyboard hook bypassed."""
    monkeypatch.setattr(Pipeline, "_open_audio", lambda self, probe: None)
    monkeypatch.setattr(Pipeline, "_install_mute_hotkey", lambda self: None)
    for attribute in ("SileroVad", "SpeechSegmenter", "WakeDetector", "Transcriber"):
        monkeypatch.setattr(f"myagent.voice.pipeline.{attribute}", lambda *a, **k: object())
    monkeypatch.setattr("myagent.voice.pipeline.create_synthesizer", lambda *a, **k: object())
    instance = Pipeline(VoiceSettings(mode="wake"))
    instance.speaker = _FakeSpeaker()  # type: ignore[assignment]
    return instance


class _FakeSpeaker:
    def __init__(self) -> None:
        self.active = False
        self.flushed = 0

    @property
    def is_active(self) -> bool:
        return self.active

    def flush(self) -> None:
        self.flushed += 1
        self.active = False


class _FakeSegmenter:
    def __init__(self) -> None:
        self.resets = 0
        self.in_speech = False

    def reset(self) -> None:
        self.resets += 1


class TestPipelineMute:
    def test_muting_closes_the_attention_window(self, pipeline: Pipeline) -> None:
        """Muted mid-conversation, the follow-up window must not survive."""
        pipeline.segmenter = _FakeSegmenter()  # type: ignore[assignment]
        pipeline.vad = _FakeSegmenter()  # type: ignore[assignment]
        pipeline._refresh_attention()
        assert pipeline._is_attending() is True

        pipeline.set_muted(True)

        assert pipeline.muted is True
        assert pipeline._is_attending() is False

    def test_unmuting_does_not_reopen_attention(self, pipeline: Pipeline) -> None:
        """After unmuting, the wake word is needed again - no stale window."""
        pipeline.segmenter = _FakeSegmenter()  # type: ignore[assignment]
        pipeline.vad = _FakeSegmenter()  # type: ignore[assignment]
        pipeline._refresh_attention()
        pipeline.set_muted(True)
        pipeline.set_muted(False)

        assert pipeline._is_attending() is False

    def test_toggle_flips_and_flags_the_change(self, pipeline: Pipeline) -> None:
        pipeline.segmenter = _FakeSegmenter()  # type: ignore[assignment]
        pipeline.vad = _FakeSegmenter()  # type: ignore[assignment]
        pipeline.toggle_mute()

        assert pipeline.muted is True
        assert pipeline._mute_changed.is_set(), "the kernel must be told"

    def test_stop_speaking_flushes_audio_and_queue(self, pipeline: Pipeline) -> None:
        speaker: Any = pipeline.speaker
        pipeline.segmenter = _FakeSegmenter()  # type: ignore[assignment]
        pipeline.vad = _FakeSegmenter()  # type: ignore[assignment]
        speaker.active = True
        pipeline._sentences.put_nowait("one")
        pipeline._sentences.put_nowait("two")
        pipeline._turn_active = True

        pipeline.stop_speaking()

        assert speaker.flushed == 1  # audio already buffered is dropped
        assert pipeline._sentences.empty()  # and nothing more gets synthesized
        assert pipeline._turn_active is False

    @pytest.mark.parametrize("phrase", SLEEP_PHRASES)
    def test_sleep_phrases_are_recognized(self, pipeline: Pipeline, phrase: str) -> None:
        assert pipeline._sleep_requested(phrase.upper() + ".") is True

    def test_ordinary_speech_is_not_a_sleep_phrase(self, pipeline: Pipeline) -> None:
        assert pipeline._sleep_requested("stop the music") is False
        assert pipeline._sleep_requested("what time is it") is False
