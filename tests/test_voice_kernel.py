"""Kernel-side voice tests: sentence splitting, cancellation, the /voice WS."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import AsyncIterator

from myagent.config import Settings
from myagent.core import history
from myagent.gateway.types import ChatMessage, ModelSpec, ProviderError
from myagent.server.voice_ws import split_sentences
from tests.fakes import FakeClient, Script
from tests.test_chat_api import make_client
from tests.test_loop import build_loop


class TestSplitSentences:
    def test_splits_complete_sentences(self) -> None:
        sentences, rest = split_sentences(
            "The weather is sunny today. Tomorrow it will rain heavily. And then"
        )
        assert sentences == ["The weather is sunny today.", "Tomorrow it will rain heavily."]
        assert rest == " And then"

    def test_short_fragments_ride_along(self) -> None:
        sentences, rest = split_sentences("Dr. Chen says hello to everyone here.")
        assert sentences == ["Dr. Chen says hello to everyone here."]
        assert rest == ""

    def test_incomplete_buffer_returns_nothing(self) -> None:
        sentences, rest = split_sentences("this sentence never actually en")
        assert sentences == []
        assert rest == "this sentence never actually en"

    def test_question_and_exclamation_marks(self) -> None:
        sentences, _ = split_sentences("How are you doing today? I am doing great, thanks!")
        assert len(sentences) == 2


class SlowClient(FakeClient):
    """FakeClient whose stream yields control between deltas (cancellable)."""

    def stream(
        self,
        spec: ModelSpec,
        messages: list[ChatMessage],
        usage_out: dict[str, int],
        max_tokens: int | None = None,
    ) -> AsyncIterator[str]:
        self.calls.append(spec.key)
        script = self.scripts[spec.key]

        async def run() -> AsyncIterator[str]:
            assert isinstance(script, list)
            for delta in script:
                await asyncio.sleep(0)  # let the cancel task run
                yield delta

        return run()


async def test_cancel_persists_partial_answer(db: sqlite3.Connection, settings: Settings) -> None:
    """Barge-in: generation stops and exactly the delivered text is persisted."""
    deltas = [f"chunk{i} " for i in range(50)]
    loop = build_loop(settings, {"p1/m": deltas})
    loop._gateway._client = SlowClient({"p1/m": deltas})  # type: ignore[attr-defined]
    session = loop.ensure_session(None)

    cancel = asyncio.Event()
    received = 0
    async for _chunk in loop.respond(session, "talk a lot", cancel=cancel):
        received += 1
        if received == 5:
            cancel.set()

    messages = history.get_messages(settings.db_path(), session)
    answer = messages[-1]["content"]
    assert messages[-1]["role"] == "assistant"
    assert 0 < len(answer.split()) < 50  # partial, not the full script
    types = [row["type"] for row in db.execute("SELECT type FROM events ORDER BY id")]
    assert "TurnInterrupted" in types


async def test_uncancelled_turn_is_unaffected(db: sqlite3.Connection, settings: Settings) -> None:
    loop = build_loop(settings, {"p1/m": ["complete ", "answer."]})
    session = loop.ensure_session(None)
    cancel = asyncio.Event()
    async for _ in loop.respond(session, "hi", cancel=cancel):
        pass
    assert history.get_messages(settings.db_path(), session)[-1]["content"] == "complete answer."


def test_voice_ws_streams_sentences(settings: Settings) -> None:
    reply = "First sentence of the reply. Second sentence arrives right after. Then a tail"
    deltas = [reply[i : i + 7] for i in range(0, len(reply), 7)]
    with (
        make_client(settings, {"p1/m": deltas}) as client,
        client.websocket_connect("/voice") as socket,
    ):
        hello = json.loads(socket.receive_text())
        assert hello["type"] == "session"

        socket.send_text(json.dumps({"type": "utterance", "text": "speak to me"}))
        said: list[str] = []
        while True:
            frame = json.loads(socket.receive_text())
            if frame["type"] == "say":
                said.append(frame["text"])
            elif frame["type"] == "turn_done":
                assert frame["full_text"] == reply
                break
            elif frame["type"] == "error":
                raise AssertionError(frame["message"])
    assert said[0] == "First sentence of the reply."
    assert said[1] == "Second sentence arrives right after."
    assert said[2] == "Then a tail"  # remainder flushed at turn end


def test_voice_ws_reports_gateway_failure(settings: Settings) -> None:
    scripts: dict[str, Script] = {
        "p1/m": ProviderError("p1", "down"),
        "p2/m": ProviderError("p2", "down"),
        "p3/m": ProviderError("p3", "down"),
    }
    with (
        make_client(settings, scripts) as client,
        client.websocket_connect("/voice") as socket,
    ):
        socket.receive_text()  # session frame
        socket.send_text(json.dumps({"type": "utterance", "text": "hello"}))
        while True:
            frame = json.loads(socket.receive_text())
            if frame["type"] == "error":
                break


def test_voice_ws_rejects_unknown_frames(settings: Settings) -> None:
    with (
        make_client(settings, {"p1/m": ["ok"]}) as client,
        client.websocket_connect("/voice") as socket,
    ):
        socket.receive_text()
        socket.send_text(json.dumps({"type": "bogus"}))
        frame = json.loads(socket.receive_text())
        assert frame["type"] == "error"
