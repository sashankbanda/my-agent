"""Append-only event log.

Every meaningful occurrence in the kernel is one immutable row in ``events``.
This single log serves as the audit trail, the UI live feed, and the
debugging/replay record. Application code appends; it never updates or
deletes event rows.

Event types are declared here as they are introduced by each milestone, so
this enum is the catalogue of everything the system can report about itself.
"""

from __future__ import annotations

import json
import sqlite3
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

    # M4 - tools and permissions
    FAST_PATH_HANDLED = "FastPathHandled"  # answered locally, zero tokens
    ESCALATED_TO_CLOUD = "EscalatedToCloud"  # local model's answer was not good enough
    TOOL_CALL_REQUESTED = "ToolCallRequested"
    TOOL_CALL_COMPLETED = "ToolCallCompleted"
    PERMISSION_DECIDED = "PermissionDecided"
    CONFIRMATION_RESOLVED = "ConfirmationResolved"
    GRANT_ADDED = "GrantAdded"
    GRANT_REVOKED = "GrantRevoked"
    KILL_SWITCH_ENGAGED = "KillSwitchEngaged"
    KILL_SWITCH_RELEASED = "KillSwitchReleased"
    BUDGET_EXCEEDED = "BudgetExceeded"


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
        "INSERT INTO events (type, trace_id, data_json) VALUES (?, ?, ?)",
        (type_.value, trace_id, data_json),
    )
    row_id = cursor.lastrowid
    assert row_id is not None  # INSERT on a rowid table always yields an id
    # Same information, push side: this is what the HUD and overlay render.
    broadcaster.publish(
        {"id": row_id, "type": type_.value, "session_id": trace_id, "data": payload}
    )
    return row_id


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
