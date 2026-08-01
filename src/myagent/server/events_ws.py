"""Live feed and status snapshot for the HUD and overlay.

``/events`` streams every kernel event as it happens (the push side of the
append-only log). ``/status`` is the cheap poll for things that are *states*
rather than events: provider health, quota headroom, kill switch, whether the
voice satellite is attached, last backup.

Together these replace reading terminal output: anything the logs would have
told you is visible here.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect

import myagent
from myagent.bus import broadcaster
from myagent.config import Settings
from myagent.db import connection
from myagent.events import EventType, append_event
from myagent.gateway.health import HealthTracker
from myagent.gateway.quota import QuotaGovernor
from myagent.gateway.registry import RegistryError, load_registry
from myagent.logging import get_logger
from myagent.security.broker import PermissionBroker
from myagent.vault.snapshot import last_snapshot
from myagent.voice.config import load_voice_settings

log = get_logger(__name__)

router = APIRouter()

STARTED_AT = time.time()
RECENT_EVENT_LIMIT = 40


@router.websocket("/events")
async def events_ws(websocket: WebSocket) -> None:
    """Stream live kernel events to a UI until it disconnects."""
    await websocket.accept()
    loop_ = websocket.app.state.loop

    # Replay a little history so a UI opened mid-task is not blank.
    with connection(loop_.db_path) as conn:
        rows = conn.execute(
            "SELECT id, ts, type, trace_id, data_json FROM events ORDER BY id DESC LIMIT ?",
            (RECENT_EVENT_LIMIT,),
        ).fetchall()
    for row in reversed(rows):
        await websocket.send_text(
            json.dumps(
                {
                    "id": row["id"],
                    "ts": row["ts"],
                    "type": row["type"],
                    "session_id": row["trace_id"],
                    "data": json.loads(row["data_json"]),
                    "replay": True,
                }
            )
        )

    queue = broadcaster.subscribe()
    try:
        while True:
            payload = await queue.get()
            await websocket.send_text(json.dumps(payload))
    except (WebSocketDisconnect, RuntimeError):
        return
    finally:
        broadcaster.unsubscribe(queue)


def _provider_status(settings: Settings) -> list[dict[str, Any]]:
    """Per-model quota headroom and provider health for the HUD."""
    try:
        registry = load_registry()
    except RegistryError as exc:
        log.warning("registry_unavailable", error=str(exc))
        return []
    db_path = settings.db_path()
    quota = QuotaGovernor(db_path)
    health = HealthTracker(db_path)
    entries: list[dict[str, Any]] = []
    for model in registry.all_models:
        usage = quota.usage(model)
        entries.append(
            {
                "key": model.key,
                "provider": model.provider,
                "available": health.is_available(model.provider) and quota.can_use(model),
                "healthy": health.is_available(model.provider),
                "usage": {
                    window: {"used": used, "limit": limit}
                    for window, (used, limit) in usage.items()
                },
                "trains_on_data": model.trains_on_data,
            }
        )
    return entries


@router.get("/status")
async def status(request: Request) -> dict[str, Any]:
    """Everything a dashboard needs that is not an event."""
    settings: Settings = request.app.state.settings
    loop_ = request.app.state.loop
    broker: PermissionBroker = request.app.state.broker
    db_path = loop_.db_path

    with connection(db_path) as conn:
        sessions = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        messages = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        facts = conn.execute("SELECT COUNT(*) FROM memory_items").fetchone()[0]
        # Turns answered locally today: model calls (and tokens) not spent.
        saved = conn.execute(
            "SELECT COUNT(*) FROM events WHERE type = ? AND ts >= date('now')",
            (EventType.FAST_PATH_HANDLED.value,),
        ).fetchone()[0]
        llm_turns = conn.execute(
            "SELECT COUNT(*) FROM events WHERE type = ? AND ts >= date('now')",
            (EventType.INFERENCE_ROUTED.value,),
        ).fetchone()[0]

    voice_settings = load_voice_settings()
    return {
        "version": myagent.__version__,
        "uptime_seconds": int(time.time() - STARTED_AT),
        "kill_switch": broker.kill_switch.engaged,
        "voice": {
            "connected": bool(getattr(request.app.state, "voice_connected", False)),
            "state": getattr(request.app.state, "voice_state", "idle"),
            "mode": voice_settings.mode,
            "wake_word": voice_settings.wake.model,
            "stt_engine": voice_settings.stt.engine,
            "tts_engine": voice_settings.tts.engine,
        },
        "providers": _provider_status(settings),
        "savings": {"local_today": saved, "model_calls_today": llm_turns},
        "memory": {"sessions": sessions, "messages": messages, "facts": facts},
        "vault": {
            "enabled": settings.vault.enabled,
            "backend": settings.vault.backend,
            "last_snapshot": last_snapshot(db_path),
        },
        "tools": {"roots": settings.tools.roots or ["(default user folders)"]},
        "ui_clients": broadcaster.subscriber_count,
    }


def publish_state(app: Any, state: str, db_path: Path) -> None:
    """Record and broadcast the assistant's current state.

    Held on the kernel (not in the voice process) so every attached UI agrees
    on one authoritative state.
    """
    if getattr(app.state, "voice_state", None) == state:
        return  # no event storm from repeated identical states
    app.state.voice_state = state
    with connection(db_path) as conn:
        append_event(conn, EventType.VOICE_STATE, {"state": state})
