"""Wire-protocol tests: exactly one 'done' per turn, even with tool steps.

Regression guard: the loop emits a done chunk per model step, so forwarding
them made the UI mark the answer finished while tools were still running.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from myagent.config import Settings, ToolSettings
from myagent.core.loop import AgentLoop
from myagent.db import connection, migrate
from myagent.gateway.gateway import Gateway
from myagent.gateway.health import HealthTracker
from myagent.gateway.quota import QuotaGovernor
from myagent.security.broker import PermissionBroker
from myagent.security.confirm import ConfirmationService
from myagent.server.app import create_app
from myagent.tools.executor import ToolExecutor
from myagent.tools.registry import load_builtin_tools
from tests.fakes import Script, make_registry
from tests.test_tool_loop import ScriptedClient, call

load_builtin_tools()


@pytest.fixture
def sandbox(tmp_path: Path) -> Path:
    root = tmp_path / "root"
    root.mkdir()
    (root / "a.txt").write_text("alpha", encoding="utf-8")
    return root


def make_client(settings: Settings, sandbox: Path, turns: list) -> TestClient:
    scoped = settings.model_copy(update={"tools": ToolSettings(roots=[str(sandbox)])})
    with connection(scoped.db_path()) as conn:
        migrate(conn)
    gateway = Gateway(
        registry=make_registry(),
        quota=QuotaGovernor(scoped.db_path()),
        health=HealthTracker(scoped.db_path()),
        client=ScriptedClient(turns),
        db_path=scoped.db_path(),
    )
    broker = PermissionBroker(scoped.db_path())
    confirmations = ConfirmationService()
    executor = ToolExecutor(scoped.db_path(), scoped, broker, confirmations)
    loop = AgentLoop(gateway, scoped.db_path(), executor=executor, fast_path=False)
    return TestClient(create_app(scoped, loop=loop, broker=broker, confirmations=confirmations))


def events_of(body: str) -> list[dict]:
    return [json.loads(line[6:]) for line in body.splitlines() if line.startswith("data: ")]


def test_single_done_across_multiple_tool_steps(settings: Settings, sandbox: Path) -> None:
    turns = [
        ("Looking. ", [call("files.list_dir", path=str(sandbox))]),
        ("Reading. ", [call("files.read_text", path=str(sandbox / "a.txt"))]),
        ("It contains alpha.", []),
    ]
    with make_client(settings, sandbox, turns) as client:
        events = events_of(client.post("/chat", json={"message": "what's in a.txt?"}).text)

    dones = [event for event in events if event.get("done")]
    assert len(dones) == 1, f"expected one done event, got {len(dones)}"
    assert events[-1] == dones[0]  # and it must be last
    assert dones[0]["model"] == "p1/m"

    text = "".join(event["delta"] for event in events if "delta" in event)
    assert "It contains alpha." in text


def test_single_done_for_a_plain_answer(settings: Settings, sandbox: Path) -> None:
    with make_client(settings, sandbox, [("Just talking.", [])]) as client:
        events = events_of(client.post("/chat", json={"message": "hi"}).text)
    assert len([event for event in events if event.get("done")]) == 1


def test_error_turn_emits_no_done(settings: Settings, sandbox: Path) -> None:
    from myagent.gateway.types import ProviderError

    scripts: dict[str, Script] = {
        key: ProviderError(key.split("/")[0], "down") for key in ("p1/m", "p2/m", "p3/m")
    }
    scoped = settings.model_copy(update={"tools": ToolSettings(roots=[str(sandbox)])})
    with connection(scoped.db_path()) as conn:
        migrate(conn)
    from tests.fakes import FakeClient

    gateway = Gateway(
        registry=make_registry(),
        quota=QuotaGovernor(scoped.db_path()),
        health=HealthTracker(scoped.db_path()),
        client=FakeClient(scripts),
        db_path=scoped.db_path(),
    )
    loop = AgentLoop(gateway, scoped.db_path(), fast_path=False)
    with TestClient(create_app(scoped, loop=loop)) as client:
        events = events_of(client.post("/chat", json={"message": "hi"}).text)
    assert any("error" in event for event in events)
    assert not any(event.get("done") for event in events)
