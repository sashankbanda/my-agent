"""Security API: confirmations, audit trail, grants, and the kill switch.

The ``/security`` WebSocket is the human end of the confirmation workflow: it
receives permission requests and sends back decisions. The audit endpoint is a
read-only view over the append-only event log (v3 review F11 - one log).
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from myagent.db import connection
from myagent.events import EventType, append_event
from myagent.logging import get_logger
from myagent.security.broker import PermissionBroker
from myagent.security.confirm import Answer, ConfirmationService
from myagent.tools import registry

log = get_logger(__name__)

router = APIRouter()

AUDIT_EVENT_TYPES = (
    EventType.TOOL_CALL_REQUESTED.value,
    EventType.TOOL_CALL_COMPLETED.value,
    EventType.PERMISSION_DECIDED.value,
    EventType.CONFIRMATION_RESOLVED.value,
    EventType.GRANT_ADDED.value,
    EventType.GRANT_REVOKED.value,
    EventType.KILL_SWITCH_ENGAGED.value,
    EventType.KILL_SWITCH_RELEASED.value,
    EventType.BUDGET_EXCEEDED.value,
)


class DecisionBody(BaseModel):
    """A human's answer to a confirmation request."""

    id: str
    allowed: bool
    scope: str = "once"  # once | session | always


@router.get("/audit")
async def audit(request: Request, limit: int = 100) -> list[dict[str, Any]]:
    """Recent security-relevant events, newest first."""
    loop_ = request.app.state.loop
    placeholders = ",".join("?" for _ in AUDIT_EVENT_TYPES)
    with connection(loop_.db_path) as conn:
        rows = conn.execute(
            f"""
            SELECT id, ts, type, trace_id, data_json FROM events
            WHERE type IN ({placeholders})
            ORDER BY id DESC LIMIT ?
            """,
            (*AUDIT_EVENT_TYPES, max(1, min(limit, 1000))),
        ).fetchall()
    return [
        {
            "id": row["id"],
            "ts": row["ts"],
            "type": row["type"],
            "session_id": row["trace_id"],
            "data": json.loads(row["data_json"]),
        }
        for row in rows
    ]


@router.get("/tools")
async def list_tools() -> list[dict[str, Any]]:
    """Every registered tool with its risk tier (transparency for the user)."""
    return [
        {"name": spec.name, "tier": spec.tier.label, "description": spec.description}
        for spec in registry.all_tools()
    ]


@router.get("/grants")
async def list_grants(request: Request) -> list[dict[str, Any]]:
    """Standing permission grants."""
    broker: PermissionBroker = request.app.state.broker
    return broker.list_grants()


@router.delete("/grants/{grant_id}")
async def revoke_grant(grant_id: int, request: Request) -> dict[str, Any]:
    """Revoke one standing grant."""
    broker: PermissionBroker = request.app.state.broker
    if not broker.revoke_grant(grant_id):
        raise HTTPException(status_code=404, detail="unknown grant")
    return {"revoked": grant_id}


@router.post("/kill")
async def engage_kill(request: Request) -> dict[str, Any]:
    """Emergency stop: deny everything in flight and block new tool calls."""
    broker: PermissionBroker = request.app.state.broker
    confirmations: ConfirmationService = request.app.state.confirmations
    broker.kill_switch.engage()
    denied = confirmations.deny_all()
    loop_ = request.app.state.loop
    with connection(loop_.db_path) as conn:
        append_event(conn, EventType.KILL_SWITCH_ENGAGED, {"denied_pending": denied})
    return {"engaged": True, "denied_pending": denied}


@router.post("/kill/release")
async def release_kill(request: Request) -> dict[str, Any]:
    """Re-enable tool execution after an emergency stop."""
    broker: PermissionBroker = request.app.state.broker
    broker.kill_switch.release()
    loop_ = request.app.state.loop
    with connection(loop_.db_path) as conn:
        append_event(conn, EventType.KILL_SWITCH_RELEASED, {})
    return {"engaged": False}


@router.get("/kill")
async def kill_status(request: Request) -> dict[str, Any]:
    """Whether the emergency stop is currently engaged."""
    broker: PermissionBroker = request.app.state.broker
    return {"engaged": broker.kill_switch.engaged}


@router.websocket("/security")
async def security_ws(websocket: WebSocket) -> None:
    """Confirmation channel: requests out, decisions in."""
    confirmations: ConfirmationService = websocket.app.state.confirmations
    await websocket.accept()

    async def notify(payload: dict[str, Any]) -> None:
        await websocket.send_text(json.dumps(payload))

    confirmations.add_notifier(notify)
    try:
        for pending in confirmations.pending:  # catch up a late-connecting client
            await notify(pending)
        while True:
            raw = await websocket.receive_text()
            try:
                body = DecisionBody.model_validate_json(raw)
            except ValueError:
                await notify({"type": "error", "message": "invalid decision payload"})
                continue
            resolved = confirmations.resolve(
                body.id, Answer(allowed=body.allowed, scope=body.scope)
            )
            if not resolved:
                await notify({"type": "confirm_closed", "id": body.id})
    except WebSocketDisconnect:
        return
    finally:
        confirmations.remove_notifier(notify)
