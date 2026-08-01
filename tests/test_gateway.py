"""Gateway tests: routing, preemptive quota skips, failover, and events.

The provider-kill test is Milestone 1's exit gate: the primary provider dies
mid-stream and the turn must still complete on the next candidate, with a
ProviderDegraded event on the record.
"""

from __future__ import annotations

import sqlite3

import pytest
from tests.fakes import FakeClient, Script, make_registry

from myagent.config import Settings
from myagent.gateway.gateway import Gateway
from myagent.gateway.health import FAILURE_THRESHOLD, HealthTracker
from myagent.gateway.quota import QuotaGovernor
from myagent.gateway.types import (
    AllProvidersExhaustedError,
    ChatMessage,
    InferenceChunk,
    InferenceRequest,
    NoEligibleModelError,
    ProviderError,
)


def build_gateway(
    settings: Settings, scripts: dict[str, Script], rpm: int = 100
) -> tuple[Gateway, FakeClient]:
    registry = make_registry(rpm=rpm)
    client = FakeClient(scripts)
    gateway = Gateway(
        registry=registry,
        quota=QuotaGovernor(settings.db_path()),
        health=HealthTracker(settings.db_path()),
        client=client,
        db_path=settings.db_path(),
    )
    return gateway, client


def request() -> InferenceRequest:
    return InferenceRequest(messages=[ChatMessage(role="user", content="hi")])


async def collect(gateway: Gateway, req: InferenceRequest) -> tuple[str, list[InferenceChunk]]:
    """Consume a completion the way real consumers do (honoring resets)."""
    text = ""
    chunks: list[InferenceChunk] = []
    async for chunk in gateway.complete(req):
        chunks.append(chunk)
        if chunk.reset:
            text = ""
        text += chunk.delta
    return text, chunks


def event_types(db: sqlite3.Connection) -> list[str]:
    return [row["type"] for row in db.execute("SELECT type FROM events ORDER BY id")]


async def test_happy_path_streams_first_candidate(
    db: sqlite3.Connection, settings: Settings
) -> None:
    gateway, client = build_gateway(settings, {"p1/m": ["Hello ", "world"]})
    text, chunks = await collect(gateway, request())
    assert text == "Hello world"
    assert chunks[-1].done is True
    assert chunks[-1].model_key == "p1/m"
    assert chunks[-1].tokens == len("Hello world")
    assert client.calls == ["p1/m"]
    assert "InferenceRouted" in event_types(db)


async def test_failover_before_first_token(db: sqlite3.Connection, settings: Settings) -> None:
    gateway, client = build_gateway(
        settings,
        {"p1/m": ProviderError("p1", "boom"), "p2/m": ["fallback answer"]},
    )
    text, chunks = await collect(gateway, request())
    assert text == "fallback answer"
    assert chunks[-1].model_key == "p2/m"
    assert client.calls == ["p1/m", "p2/m"]
    assert not any(chunk.reset for chunk in chunks)  # nothing was emitted, so no reset
    assert "ProviderDegraded" in event_types(db)


async def test_provider_kill_mid_stream_completes_on_next_candidate(
    db: sqlite3.Connection, settings: Settings
) -> None:
    """M1 exit gate: mid-stream death fails over with a reset."""
    gateway, client = build_gateway(
        settings,
        {
            "p1/m": ("fail_after", 2, ["I was ", "saying ", "something"]),
            "p2/m": ["The full answer."],
        },
    )
    text, chunks = await collect(gateway, request())
    assert text == "The full answer."
    assert any(chunk.reset for chunk in chunks)
    assert chunks[-1].model_key == "p2/m"
    assert client.calls == ["p1/m", "p2/m"]
    types = event_types(db)
    assert "ProviderDegraded" in types
    assert types.count("InferenceRouted") == 2


async def test_quota_exhausted_candidate_is_never_attempted(
    db: sqlite3.Connection, settings: Settings
) -> None:
    """Preemptive routing: an empty bucket means the request is not sent."""
    gateway, client = build_gateway(
        settings, {"p1/m": ["never used"], "p2/m": ["served by p2"]}, rpm=2
    )
    registry = make_registry(rpm=2)
    governor = QuotaGovernor(settings.db_path())
    p1 = registry.model("p1/m")
    for _ in range(2):
        governor.record_request(p1)

    text, _ = await collect(gateway, request())
    assert text == "served by p2"
    assert client.calls == ["p2/m"]  # p1 was skipped without any attempt


async def test_all_quota_exhausted_raises_without_sending(
    db: sqlite3.Connection, settings: Settings
) -> None:
    gateway, client = build_gateway(settings, {}, rpm=1)
    registry = make_registry(rpm=1)
    governor = QuotaGovernor(settings.db_path())
    for key in ("p1/m", "p2/m", "p3/m"):
        governor.record_request(registry.model(key))

    with pytest.raises(AllProvidersExhaustedError, match="not sent anywhere"):
        await collect(gateway, request())
    assert client.calls == []
    assert "QuotaExhausted" in event_types(db)


async def test_cooling_down_provider_is_skipped(db: sqlite3.Connection, settings: Settings) -> None:
    gateway, client = build_gateway(settings, {"p2/m": ["healthy answer"]})
    tracker = HealthTracker(settings.db_path())
    for _ in range(FAILURE_THRESHOLD):
        tracker.record_failure("p1")

    text, _ = await collect(gateway, request())
    assert text == "healthy answer"
    assert client.calls == ["p2/m"]


async def test_local_only_prompt_with_no_local_model_refuses(
    db: sqlite3.Connection, settings: Settings
) -> None:
    """A secret-bearing prompt must never reach a cloud provider."""
    gateway, client = build_gateway(settings, {"p1/m": ["leak"]})
    req = InferenceRequest(
        messages=[ChatMessage(role="user", content="my password: hunter2secret")]
    )
    with pytest.raises(NoEligibleModelError):
        await collect(gateway, req)
    assert client.calls == []


async def test_every_provider_failing_raises(db: sqlite3.Connection, settings: Settings) -> None:
    gateway, _ = build_gateway(
        settings,
        {
            "p1/m": ProviderError("p1", "down"),
            "p2/m": ProviderError("p2", "down"),
            "p3/m": ProviderError("p3", "down"),
        },
    )
    with pytest.raises(AllProvidersExhaustedError, match="every eligible provider failed"):
        await collect(gateway, request())
