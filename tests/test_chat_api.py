"""Chat API tests: SSE stream, WebSocket stream, and session endpoints."""

from __future__ import annotations

import json
from typing import Any

from fastapi.testclient import TestClient

from myagent.config import Settings
from myagent.core.loop import AgentLoop
from myagent.db import connection, migrate
from myagent.gateway.gateway import Gateway
from myagent.gateway.health import HealthTracker
from myagent.gateway.quota import QuotaGovernor
from myagent.server.app import create_app
from tests.fakes import FakeClient, Script, make_registry


def make_client(settings: Settings, scripts: dict[str, Script]) -> TestClient:
    with connection(settings.db_path()) as conn:
        migrate(conn)
    registry = make_registry()
    gateway = Gateway(
        registry=registry,
        quota=QuotaGovernor(settings.db_path()),
        health=HealthTracker(settings.db_path()),
        client=FakeClient(scripts),
        db_path=settings.db_path(),
    )
    # fast_path off: these tests exercise the model/streaming path, and the
    # local shortcut would answer greetings before the gateway is reached.
    loop = AgentLoop(gateway, settings.db_path(), fast_path=False)
    return TestClient(create_app(settings, loop=loop))


def parse_sse(body: str) -> list[dict[str, Any]]:
    events = []
    for line in body.splitlines():
        if line.startswith("data: "):
            events.append(json.loads(line.removeprefix("data: ")))
    return events


def test_chat_streams_deltas_and_done(settings: Settings) -> None:
    with make_client(settings, {"p1/m": ["Hel", "lo!"]}) as client:
        response = client.post("/chat", json={"message": "hi"})
        assert response.status_code == 200
        events = parse_sse(response.text)
    assert "session_id" in events[0]
    deltas = "".join(e["delta"] for e in events if "delta" in e)
    assert deltas == "Hello!"
    assert events[-1] == {"done": True, "model": "p1/m"}


def test_chat_reuses_session(settings: Settings) -> None:
    with make_client(settings, {"p1/m": ["reply"]}) as client:
        first = parse_sse(client.post("/chat", json={"message": "one"}).text)
        session_id = first[0]["session_id"]
        second = parse_sse(
            client.post("/chat", json={"message": "two", "session_id": session_id}).text
        )
        assert second[0]["session_id"] == session_id
        messages = client.get(f"/sessions/{session_id}").json()
    roles = [m["role"] for m in messages]
    assert roles == ["user", "assistant", "user", "assistant"]


def test_chat_reports_total_failure_honestly(settings: Settings) -> None:
    from myagent.gateway.types import ProviderError

    scripts: dict[str, Script] = {
        "p1/m": ProviderError("p1", "down"),
        "p2/m": ProviderError("p2", "down"),
        "p3/m": ProviderError("p3", "down"),
    }
    with make_client(settings, scripts) as client:
        events = parse_sse(client.post("/chat", json={"message": "hi"}).text)
    assert any("error" in event for event in events)


def test_sessions_endpoints(settings: Settings) -> None:
    with make_client(settings, {"p1/m": ["reply"]}) as client:
        events = parse_sse(client.post("/chat", json={"message": "title me"}).text)
        session_id = events[0]["session_id"]
        sessions = client.get("/sessions").json()
        assert sessions[0]["id"] == session_id
        assert sessions[0]["title"] == "title me"
        assert client.get("/sessions/unknown").status_code == 404


def test_websocket_chat_round_trip(settings: Settings) -> None:
    with (
        make_client(settings, {"p1/m": ["ws ", "answer"]}) as client,
        client.websocket_connect("/ws") as socket,
    ):
        socket.send_text(json.dumps({"message": "hello"}))
        received: list[dict[str, Any]] = []
        while True:
            event = json.loads(socket.receive_text())
            received.append(event)
            if event.get("done") or event.get("error"):
                break
    deltas = "".join(e["delta"] for e in received if "delta" in e)
    assert deltas == "ws answer"
    assert received[-1]["done"] is True
