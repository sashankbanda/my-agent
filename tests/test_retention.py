"""Event log hygiene: what is kept forever, and what is not.

The log is the audit trail *and* the debugging record *and* the UI feed. Those
have different lifetimes, and conflating them made a third of the database
transient UI state that nothing ever read back.
"""

from __future__ import annotations

import sqlite3

from myagent.config import Settings
from myagent.db import connection
from myagent.events import (
    OPERATIONAL_EVENTS,
    EventType,
    append_event,
    prune_events,
    publish_transient,
)


def _age(conn: sqlite3.Connection, event_id: int, days: int) -> None:
    conn.execute(
        "UPDATE events SET ts = datetime('now', ?) WHERE id = ?", (f"-{days} days", event_id)
    )


class TestTransientStates:
    def test_voice_state_is_not_stored(self, settings: Settings, db: sqlite3.Connection) -> None:
        """It was 32% of every row - and the HUD filtered it straight out."""
        publish_transient(EventType.VOICE_STATE, {"state": "listening"})
        stored = db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        assert stored == 0

    async def test_transient_ids_cannot_collide_with_real_rows(
        self, settings: Settings, db: sqlite3.Connection
    ) -> None:
        """UIs key on the id; a duplicate would drop a live row."""
        from myagent.bus import broadcaster

        broadcaster.bind_loop()
        queue = broadcaster.subscribe()
        try:
            publish_transient(EventType.VOICE_STATE, {"state": "idle"})
            payload = queue.get_nowait()
        finally:
            broadcaster.unsubscribe(queue)
        assert payload["id"] < 0
        assert payload["transient"] is True

    async def test_transient_events_carry_a_timestamp(self) -> None:
        from myagent.bus import broadcaster

        broadcaster.bind_loop()
        queue = broadcaster.subscribe()
        try:
            publish_transient(EventType.VOICE_STATE, {"state": "idle"})
            payload = queue.get_nowait()
        finally:
            broadcaster.unsubscribe(queue)
        assert payload["ts"], "the feed renders a time column"


class TestLiveEventsAreComplete:
    async def test_a_live_event_carries_its_timestamp(
        self, settings: Settings, db: sqlite3.Connection
    ) -> None:
        """Live rows used to reach the UI with no ts, so the feed showed blanks."""
        from myagent.bus import broadcaster

        broadcaster.bind_loop()
        queue = broadcaster.subscribe()
        try:
            with connection(settings.db_path()) as conn:
                append_event(conn, EventType.USER_SAID, {"text": "hello"})
            payload = queue.get_nowait()
        finally:
            broadcaster.unsubscribe(queue)
        assert payload["ts"], "no timestamp means a blank time column in the HUD"
        assert payload["data"] == {"text": "hello"}


class TestPruning:
    def test_old_operational_rows_are_removed(
        self, settings: Settings, db: sqlite3.Connection
    ) -> None:
        with connection(settings.db_path()) as conn:
            old = append_event(conn, EventType.INFERENCE_ROUTED, {"model": "x/y"})
            _age(conn, old, 30)
            append_event(conn, EventType.INFERENCE_ROUTED, {"model": "x/y"})  # today
            assert prune_events(conn, keep_days=14) == 1

        remaining = db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        assert remaining == 1

    def test_the_audit_trail_is_never_pruned(
        self, settings: Settings, db: sqlite3.Connection
    ) -> None:
        """Who approved what, and what ran, is the point of keeping a log."""
        with connection(settings.db_path()) as conn:
            for kind in (
                EventType.PERMISSION_DECIDED,
                EventType.TOOL_CALL_COMPLETED,
                EventType.CONFIRMATION_RESOLVED,
                EventType.KILL_SWITCH_ENGAGED,
                EventType.GRANT_ADDED,
                EventType.MEMORY_WRITTEN,
            ):
                _age(conn, append_event(conn, kind, {}), 400)
            assert prune_events(conn, keep_days=14) == 0

        assert db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 6

    def test_conversation_history_survives(
        self, settings: Settings, db: sqlite3.Connection
    ) -> None:
        """What was said is a fact about the user, not machine telemetry."""
        with connection(settings.db_path()) as conn:
            _age(conn, append_event(conn, EventType.USER_SAID, {"text": "hi"}), 400)
            _age(conn, append_event(conn, EventType.ASSISTANT_SAID, {"text": "hello"}), 400)
            assert prune_events(conn, keep_days=14) == 0

    def test_retention_can_be_switched_off(
        self, settings: Settings, db: sqlite3.Connection
    ) -> None:
        with connection(settings.db_path()) as conn:
            _age(conn, append_event(conn, EventType.INFERENCE_ROUTED, {}), 400)
            assert prune_events(conn, keep_days=0) == 0

    def test_every_prunable_type_is_deliberate(self) -> None:
        """A new event type defaults to being kept, which is the safe default."""
        must_keep = {
            EventType.PERMISSION_DECIDED,
            EventType.CONFIRMATION_RESOLVED,
            EventType.GRANT_ADDED,
            EventType.GRANT_REVOKED,
            EventType.KILL_SWITCH_ENGAGED,
            EventType.KILL_SWITCH_RELEASED,
            EventType.TOOL_CALL_REQUESTED,
            EventType.TOOL_CALL_COMPLETED,
            EventType.USER_SAID,
            EventType.ASSISTANT_SAID,
            EventType.MEMORY_WRITTEN,
            EventType.MEMORY_FORGOTTEN,
            EventType.VAULT_SNAPSHOT_CREATED,
        }
        assert not (must_keep & OPERATIONAL_EVENTS)
