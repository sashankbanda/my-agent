"""HUD backend tests: the live event stream, status snapshot, and launcher."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest

from myagent.bus import EventBroadcaster
from myagent.config import Settings
from myagent.db import connection
from myagent.events import EventType, append_event
from myagent.start import port_in_use, wait_for_kernel
from tests.test_chat_api import make_client


class TestBroadcaster:
    async def test_publish_reaches_subscribers(self) -> None:
        broadcaster = EventBroadcaster()
        broadcaster.bind_loop()
        queue = broadcaster.subscribe()
        broadcaster.publish({"type": "Hello"})
        assert (await asyncio.wait_for(queue.get(), 1))["type"] == "Hello"

    async def test_unsubscribe_stops_delivery(self) -> None:
        broadcaster = EventBroadcaster()
        broadcaster.bind_loop()
        queue = broadcaster.subscribe()
        broadcaster.unsubscribe(queue)
        broadcaster.publish({"type": "Hello"})
        assert queue.empty()

    async def test_slow_consumer_drops_oldest_not_newest(self) -> None:
        """A stalled UI must never stall the kernel; newest data wins."""
        broadcaster = EventBroadcaster()
        broadcaster.bind_loop()
        queue = broadcaster.subscribe()
        for index in range(700):  # more than QUEUE_LIMIT
            broadcaster.publish({"type": "E", "n": index})
        assert queue.full()
        newest = 0
        while not queue.empty():
            newest = max(newest, queue.get_nowait()["n"])
        assert newest == 699

    async def test_publish_from_another_thread(self) -> None:
        """Tool threads append events, so cross-thread publish must work."""
        broadcaster = EventBroadcaster()
        broadcaster.bind_loop()
        queue = broadcaster.subscribe()
        await asyncio.to_thread(broadcaster.publish, {"type": "FromThread"})
        assert (await asyncio.wait_for(queue.get(), 1))["type"] == "FromThread"

    def test_publish_without_loop_is_safe(self) -> None:
        """Before startup (or after shutdown) publishing must not raise."""
        EventBroadcaster().publish({"type": "Ignored"})

    async def test_append_event_publishes(self, db: sqlite3.Connection) -> None:
        """The DB write and the live feed are one action, not two call sites."""
        from myagent.bus import broadcaster

        broadcaster.bind_loop()
        queue = broadcaster.subscribe()
        try:
            append_event(db, EventType.APP_STARTED, {"version": "test"})
            payload = await asyncio.wait_for(queue.get(), 1)
        finally:
            broadcaster.unsubscribe(queue)
        assert payload["type"] == "AppStarted"
        assert payload["data"] == {"version": "test"}


class TestEndpoints:
    def test_status_reports_everything_a_dashboard_needs(self, settings: Settings) -> None:
        with make_client(settings, {"p1/m": ["hi"]}) as client:
            status = client.get("/status").json()
        assert status["kill_switch"] is False
        assert status["voice"]["connected"] is False
        assert {"sessions", "messages", "facts"} <= set(status["memory"])
        assert "roots" in status["tools"]
        assert isinstance(status["providers"], list)

    def test_status_reflects_the_kill_switch(self, settings: Settings) -> None:
        with make_client(settings, {"p1/m": ["hi"]}) as client:
            client.post("/kill")
            assert client.get("/status").json()["kill_switch"] is True
            client.post("/kill/release")
            assert client.get("/status").json()["kill_switch"] is False

    def test_events_socket_replays_recent_history(self, settings: Settings) -> None:
        """A HUD opened mid-task should not be blank."""
        with (
            make_client(settings, {"p1/m": ["hi"]}) as client,
            client.websocket_connect("/events") as socket,
        ):
            first = json.loads(socket.receive_text())
        assert first["replay"] is True
        assert first["type"] == "AppStarted"  # written during startup

    def test_events_socket_pushes_new_events(self, settings: Settings) -> None:
        """Something that happens after connecting must arrive live.

        Read with a bound rather than draining first: after the finite replay
        there is nothing to receive until an event occurs, so a blind drain
        would block forever.
        """
        with (
            make_client(settings, {"p1/m": ["hi"]}) as client,
            client.websocket_connect("/events") as socket,
        ):
            client.post("/memory", json={"content": "live feed check"})
            for _ in range(60):  # replay frames first, then the live one
                event = json.loads(socket.receive_text())
                if event["type"] == "MemoryWritten" and not event.get("replay"):
                    break
            else:
                raise AssertionError("live MemoryWritten event never arrived")

    def test_voice_connection_shows_up_in_status(self, settings: Settings) -> None:
        with make_client(settings, {"p1/m": ["hi"]}) as client:
            with client.websocket_connect("/voice") as socket:
                socket.receive_text()  # session frame
                assert client.get("/status").json()["voice"]["connected"] is True
            assert client.get("/status").json()["voice"]["connected"] is False

    def test_voice_state_frames_become_events(self, settings: Settings) -> None:
        with (
            make_client(settings, {"p1/m": ["hi"]}) as client,
            client.websocket_connect("/voice") as voice,
        ):
            voice.receive_text()
            voice.send_text(json.dumps({"type": "state", "value": "listening"}))
            # The state is authoritative on the kernel, so /status shows it.
            for _ in range(20):
                if client.get("/status").json()["voice"]["state"] == "listening":
                    break
            assert client.get("/status").json()["voice"]["state"] == "listening"


class TestLauncher:
    def test_port_in_use_detects_a_listener(self, settings: Settings) -> None:
        import socket

        with socket.socket() as server:
            server.bind(("127.0.0.1", 0))
            server.listen(1)
            port = server.getsockname()[1]
            assert port_in_use("127.0.0.1", port) is True
        assert port_in_use("127.0.0.1", port) is False

    def test_wait_for_kernel_gives_up(self) -> None:
        assert wait_for_kernel("http://127.0.0.1:9", timeout=0.5) is False


class TestOverlay:
    def test_state_styles_cover_every_reported_state(self) -> None:
        """Every state the kernel can publish must render as something."""
        from myagent.overlay.__main__ import STATE_STYLE

        for state in ("offline", "idle", "waiting", "listening", "thinking", "speaking", "down"):
            assert state in STATE_STYLE

    def test_event_application_updates_state_and_caption(self, tmp_path: Path) -> None:
        """The event->visual mapping is testable without opening a window."""
        from myagent.overlay.__main__ import Overlay

        overlay = Overlay.__new__(Overlay)  # no Tk needed for the pure logic
        overlay.state = "idle"
        overlay.caption_text = ""
        overlay._apply({"type": "VoiceState", "data": {"state": "thinking"}})
        assert overlay.state == "thinking"
        overlay._apply({"type": "UserSaid", "data": {"text": "open downloads"}})
        assert "open downloads" in overlay.caption_text
        overlay._apply(
            {"type": "ToolCallCompleted", "data": {"tool": "files.list_dir", "ok": True}}
        )
        assert "files.list_dir" in overlay.caption_text
        overlay._apply({"type": "_disconnected"})
        assert overlay.state == "down"


@pytest.mark.parametrize(
    "event_type",
    [
        EventType.USER_SAID,
        EventType.ASSISTANT_SAID,
        EventType.VOICE_STATE,
        EventType.VOICE_CONNECTED,
        EventType.VOICE_DISCONNECTED,
    ],
)
def test_new_event_types_round_trip(db: sqlite3.Connection, event_type: EventType) -> None:
    append_event(db, event_type, {"probe": True})
    types = [row["type"] for row in db.execute("SELECT type FROM events")]
    assert event_type.value in types


def test_status_survives_a_missing_registry(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A broken providers.yaml must not take the dashboard down with it."""
    from myagent.gateway.registry import RegistryError
    from myagent.server import events_ws

    def explode(*_args: object, **_kwargs: object) -> None:
        raise RegistryError("simulated")

    monkeypatch.setattr(events_ws, "load_registry", explode)
    with make_client(settings, {"p1/m": ["hi"]}) as client:
        status = client.get("/status")
    assert status.status_code == 200
    assert status.json()["providers"] == []


def test_events_table_is_the_single_source(db: sqlite3.Connection, settings: Settings) -> None:
    """What the HUD streams is exactly what the audit log records."""
    with connection(settings.db_path()) as conn:
        append_event(conn, EventType.TOOL_CALL_COMPLETED, {"tool": "files.list_dir", "ok": True})
    rows = list(db.execute("SELECT type, data_json FROM events WHERE type='ToolCallCompleted'"))
    assert len(rows) == 1
    assert json.loads(rows[0]["data_json"])["tool"] == "files.list_dir"
