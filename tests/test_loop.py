"""Agent loop tests: turn lifecycle and reset handling."""

from __future__ import annotations

import sqlite3

from tests.fakes import FakeClient, Script, make_registry

from myagent.config import Settings
from myagent.core import history
from myagent.core.loop import AgentLoop
from myagent.gateway.gateway import Gateway
from myagent.gateway.health import HealthTracker
from myagent.gateway.quota import QuotaGovernor


def build_loop(settings: Settings, scripts: dict[str, Script]) -> AgentLoop:
    registry = make_registry()
    gateway = Gateway(
        registry=registry,
        quota=QuotaGovernor(settings.db_path()),
        health=HealthTracker(settings.db_path()),
        client=FakeClient(scripts),
        db_path=settings.db_path(),
    )
    return AgentLoop(gateway, settings.db_path())


async def drain(loop: AgentLoop, session: str, text: str) -> None:
    async for _ in loop.respond(session, text):
        pass


async def test_turn_persists_both_messages(db: sqlite3.Connection, settings: Settings) -> None:
    loop = build_loop(settings, {"p1/m": ["Hi ", "there!"]})
    session = loop.ensure_session(None)
    await drain(loop, session, "hello")
    messages = history.get_messages(settings.db_path(), session)
    assert [(m["role"], m["content"]) for m in messages] == [
        ("user", "hello"),
        ("assistant", "Hi there!"),
    ]
    assert messages[1]["model"] == "p1/m"
    assert messages[1]["provider"] == "p1"


async def test_mid_stream_failover_persists_only_final_answer(
    db: sqlite3.Connection, settings: Settings
) -> None:
    """The reset protocol: text from the dead provider must not be persisted."""
    loop = build_loop(
        settings,
        {
            "p1/m": ("fail_after", 2, ["garbage ", "partial ", "text"]),
            "p2/m": ["Clean final answer."],
        },
    )
    session = loop.ensure_session(None)
    await drain(loop, session, "hello")
    messages = history.get_messages(settings.db_path(), session)
    assert messages[1]["content"] == "Clean final answer."
    assert messages[1]["model"] == "p2/m"


async def test_transcript_includes_prior_turns(db: sqlite3.Connection, settings: Settings) -> None:
    """Multi-turn: the second request must carry the first exchange."""
    client_scripts: dict[str, Script] = {"p1/m": ["reply"]}
    registry = make_registry()
    client = FakeClient(client_scripts)
    gateway = Gateway(
        registry=registry,
        quota=QuotaGovernor(settings.db_path()),
        health=HealthTracker(settings.db_path()),
        client=client,
        db_path=settings.db_path(),
    )

    seen_transcripts: list[list[str]] = []
    original_stream = client.stream

    def spying_stream(spec, messages, usage_out, max_tokens=None):  # type: ignore[no-untyped-def]
        seen_transcripts.append([m.content for m in messages])
        return original_stream(spec, messages, usage_out, max_tokens)

    client.stream = spying_stream  # type: ignore[method-assign]
    loop = AgentLoop(gateway, settings.db_path())
    session = loop.ensure_session(None)
    await drain(loop, session, "first")
    await drain(loop, session, "second")

    assert "first" in seen_transcripts[1]
    assert "reply" in seen_transcripts[1]
    assert seen_transcripts[1][-1] == "second"


def test_ensure_session_reuses_valid_and_replaces_unknown(
    db: sqlite3.Connection, settings: Settings
) -> None:
    loop = build_loop(settings, {"p1/m": ["x"]})
    created = loop.ensure_session(None)
    assert loop.ensure_session(created) == created
    assert loop.ensure_session("unknown-id") != "unknown-id"
