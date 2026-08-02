"""Append-only event log.

Every meaningful occurrence in the kernel is one immutable row in ``events``.
This single log serves as the audit trail, the UI live feed, and the
debugging/replay record. Application code appends; it never updates a row.

Two deliberate exceptions to "one log, forever":

- ``publish_transient`` pushes a passing *state* to the UIs without storing
  it. Which colour the orb is right now belongs on a dashboard, not in an
  audit trail - and persisting it made VoiceState a third of all rows.
- ``prune_events`` deletes *operational* rows past a retention window. The
  security trail - permissions, tool calls, kill switch - is never pruned.

Event types are declared here as they are introduced by each milestone, so
this enum is the catalogue of everything the system can report about itself.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from myagent.bus import broadcaster


class EventType(StrEnum):
    """Catalogue of event types written to the log."""

    # M0 - lifecycle
    APP_STARTED = "AppStarted"
    APP_STOPPING = "AppStopping"

    # M1 - model gateway
    INFERENCE_ROUTED = "InferenceRouted"
    PROVIDER_DEGRADED = "ProviderDegraded"
    QUOTA_EXHAUSTED = "QuotaExhausted"

    # M2 - memory and vault
    MEMORY_WRITTEN = "MemoryWritten"
    MEMORY_FORGOTTEN = "MemoryForgotten"
    VAULT_SNAPSHOT_CREATED = "VaultSnapshotCreated"
    VAULT_RESTORE_COMPLETED = "VaultRestoreCompleted"

    # M3 - voice
    TURN_INTERRUPTED = "TurnInterrupted"
    VOICE_STATE = "VoiceState"  # idle | listening | thinking | speaking
    VOICE_CONNECTED = "VoiceConnected"
    VOICE_DISCONNECTED = "VoiceDisconnected"
    USER_SAID = "UserSaid"
    ASSISTANT_SAID = "AssistantSaid"
    USER_STOPPED = "UserStopped"  # "stop talking": turn cancelled, speech flushed
    VOICE_MUTED = "VoiceMuted"  # microphone gated at the satellite

    # M4 - tools and permissions
    FAST_PATH_HANDLED = "FastPathHandled"  # answered locally, zero tokens
    INFERENCE_TIER = "InferenceTier"  # which tier a turn was routed to, and why
    ESCALATED_TO_CLOUD = "EscalatedToCloud"  # local model's answer was not good enough
    TOOL_NUDGE = "ToolNudge"  # model explained instead of acting; corrected in place
    LANGUAGE_CORRECTED = "LanguageCorrected"  # replied in a script the user did not use
    TOOL_CALL_REQUESTED = "ToolCallRequested"
    TOOL_CALL_COMPLETED = "ToolCallCompleted"
    PERMISSION_DECIDED = "PermissionDecided"
    CONFIRMATION_RESOLVED = "ConfirmationResolved"
    GRANT_ADDED = "GrantAdded"
    GRANT_REVOKED = "GrantRevoked"
    KILL_SWITCH_ENGAGED = "KillSwitchEngaged"
    KILL_SWITCH_RELEASED = "KillSwitchReleased"
    BUDGET_EXCEEDED = "BudgetExceeded"

    # M5 - web and time
    SCHEDULE_ADDED = "ScheduleAdded"
    SCHEDULE_REMOVED = "ScheduleRemoved"
    SCHEDULE_FIRED = "ScheduleFired"
    NOTIFICATION_SENT = "NotificationSent"


_transient_id = 0


def publish_transient(type_: EventType, data: dict[str, Any] | None = None) -> None:
    """Broadcast a passing state to the UIs without storing it.

    Some things are states, not facts: which colour the orb is right now is
    worth pushing to a dashboard and worthless in an audit log a year later.
    Persisting them made ``VoiceState`` a third of every row in the database -
    and of every nightly backup - while the HUD filtered them straight out
    again. Ids count downwards so they cannot collide with real event rows.
    """
    global _transient_id
    _transient_id -= 1
    broadcaster.publish(
        {
            "id": _transient_id,
            "ts": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
            "type": type_.value,
            "session_id": None,
            "data": data or {},
            "transient": True,
        }
    )


def append_event(
    conn: sqlite3.Connection,
    type_: EventType,
    data: dict[str, Any] | None = None,
    trace_id: str | None = None,
) -> int:
    """Append one event row and return its id.

    ``data`` must be JSON-serializable; it is stored compactly. Events are
    facts about what happened - never put secrets or raw prompt bodies here.
    """
    payload = data or {}
    data_json = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    cursor = conn.execute(
        "INSERT INTO events (type, trace_id, data_json) VALUES (?, ?, ?) RETURNING ts",
        (type_.value, trace_id, data_json),
    )
    row = cursor.fetchone()
    row_id = cursor.lastrowid
    assert row_id is not None  # INSERT on a rowid table always yields an id
    # Same information, push side: this is what the HUD and overlay render.
    # The timestamp comes from the row so live and replayed events agree -
    # without it, live rows rendered with a blank time column.
    broadcaster.publish(
        {
            "id": row_id,
            "ts": row["ts"] if row else None,
            "type": type_.value,
            "session_id": trace_id,
            "data": payload,
        }
    )
    return row_id


# Events that exist to explain *how the machine ran* - useful for a few days
# of debugging, worthless a year later, and pure weight in every backup.
# Everything not listed here is kept: the security trail (who approved what,
# what a tool did, when the kill switch fired) is the point of the log.
OPERATIONAL_EVENTS = frozenset(
    {
        EventType.APP_STARTED,
        EventType.APP_STOPPING,
        EventType.INFERENCE_ROUTED,
        EventType.INFERENCE_TIER,
        EventType.PROVIDER_DEGRADED,
        EventType.QUOTA_EXHAUSTED,
        EventType.VOICE_STATE,
        EventType.VOICE_CONNECTED,
        EventType.VOICE_DISCONNECTED,
        EventType.VOICE_MUTED,
        EventType.FAST_PATH_HANDLED,
        EventType.ESCALATED_TO_CLOUD,
        EventType.TOOL_NUDGE,
        EventType.LANGUAGE_CORRECTED,
        EventType.USER_STOPPED,
        EventType.SCHEDULE_FIRED,
    }
)


def prune_events(conn: sqlite3.Connection, keep_days: int) -> int:
    """Delete operational events older than ``keep_days``; returns how many.

    The audit trail is append-only *and* permanent - this only removes rows
    nothing reads after the week they were written. Without it the log grows
    without bound and the nightly encrypted backup grows with it.
    """
    if keep_days <= 0:
        return 0
    placeholders = ",".join("?" * len(OPERATIONAL_EVENTS))
    cursor = conn.execute(
        f"DELETE FROM events WHERE type IN ({placeholders}) AND ts < datetime('now', ?)",
        (*[event.value for event in OPERATIONAL_EVENTS], f"-{keep_days} days"),
    )
    return cursor.rowcount


def read_event(conn: sqlite3.Connection, event_id: int) -> dict[str, Any] | None:
    """Fetch one event by id as a plain dict, or None if absent."""
    row = conn.execute(
        "SELECT id, ts, type, trace_id, data_json FROM events WHERE id = ?",
        (event_id,),
    ).fetchone()
    if row is None:
        return None
    return {
        "id": row["id"],
        "ts": row["ts"],
        "type": row["type"],
        "trace_id": row["trace_id"],
        "data": json.loads(row["data_json"]),
    }
