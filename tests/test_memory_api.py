"""Memory + vault API tests, including preference-changes-behavior end to end."""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

from fastapi import FastAPI

from myagent.config import Settings
from tests.fakes import Script
from tests.test_chat_api import make_client, parse_sse


def make_vault_settings(settings: Settings, tmp_path: Path) -> Settings:
    """Same per-test settings, with a folder vault enabled."""
    return settings.model_copy(
        update={
            "vault": settings.vault.model_copy(
                update={"enabled": True, "backend": "folder", "local_path": tmp_path / "vault"}
            )
        }
    )


def test_memory_crud_endpoints(settings: Settings) -> None:
    with make_client(settings, {"p1/m": ["ok"]}) as client:
        created = client.post("/memory", json={"content": "speaks Telugu and English"})
        assert created.status_code == 200
        item_id = created.json()["id"]

        listed = client.get("/memory").json()
        assert listed[0]["content"] == "speaks Telugu and English"

        gone = client.post("/memory/forget", json={"id": item_id})
        assert gone.status_code == 200
        assert client.get("/memory").json() == []

        missing = client.post("/memory/forget", json={"id": 424242})
        assert missing.status_code == 404


def test_empty_fact_rejected(settings: Settings) -> None:
    with make_client(settings, {"p1/m": ["ok"]}) as client:
        assert client.post("/memory", json={"content": "   "}).status_code == 422


def test_stated_fact_reaches_the_model(settings: Settings) -> None:
    """M2 exit criterion: a remembered preference changes what the model sees."""
    seen_prompts: list[str] = []

    scripts: dict[str, Script] = {"p1/m": ["noted"]}
    with make_client(settings, scripts) as client:
        app = cast(FastAPI, client.app)
        fake = app.state.loop._gateway._client  # the FakeClient inside
        original = fake.stream

        def spy(spec, messages, usage_out, max_tokens=None, tools=None, tool_calls_out=None):  # type: ignore[no-untyped-def]
            seen_prompts.append("\n".join(m.content for m in messages))
            return original(spec, messages, usage_out, max_tokens, tools, tool_calls_out)

        fake.stream = spy  # type: ignore[method-assign]
        client.post("/memory", json={"content": "the user is vegetarian"})
        parse_sse(client.post("/chat", json={"message": "plan my dinner"}).text)

    assert seen_prompts
    assert "vegetarian" in seen_prompts[-1]


def test_vault_backup_and_status_endpoints(settings: Settings, tmp_path: Path) -> None:
    vault_settings = make_vault_settings(settings, tmp_path)
    with make_client(vault_settings, {"p1/m": ["ok"]}) as client:
        status = client.get("/vault/status").json()
        assert status["enabled"] is True
        assert status["last_snapshot"] is None

        backup = client.post("/vault/backup")
        assert backup.status_code == 200
        entry = backup.json()
        assert entry["blob_name"].startswith("snapshots/")
        # First-ever backup surfaces the recovery string exactly once.
        assert "recovery_string" in entry

        status = client.get("/vault/status").json()
        assert status["last_snapshot"]["blob_name"] == entry["blob_name"]
        assert status["manifest_chain_ok"] is True

        second = client.post("/vault/backup").json()
        assert "recovery_string" not in second


def test_vault_backup_when_disabled_returns_503(settings: Settings) -> None:
    with make_client(settings, {"p1/m": ["ok"]}) as client:
        response = client.post("/vault/backup")
        assert response.status_code == 503
        assert "disabled" in response.json()["detail"]


def test_ws_and_memory_share_state(settings: Settings) -> None:
    """The fact store and the chat pipeline operate on the same database."""
    with make_client(settings, {"p1/m": ["hi"]}) as client:
        client.post("/memory", json={"content": "shared-state check"})
        with client.websocket_connect("/ws") as socket:
            socket.send_text(json.dumps({"message": "hello"}))
            while True:
                event = json.loads(socket.receive_text())
                if event.get("done") or event.get("error"):
                    break
        assert client.get("/memory").json()[0]["content"] == "shared-state check"
