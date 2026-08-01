"""Chat API: SSE for simple clients, WebSocket for the UI.

Wire protocol (both transports, one JSON payload shape per event):
  {"session_id": "..."}          - first event of a turn, echoes/announces the session
  {"delta": "text"}              - streamed answer fragment
  {"reset": true}                - provider failed over mid-answer: discard text so far
  {"done": true, "model": "..."} - turn finished
  {"error": "message"}           - turn failed honestly (after all failover attempts)
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from myagent.core.history import get_messages, list_sessions, session_exists
from myagent.core.loop import AgentLoop
from myagent.gateway.types import GatewayError
from myagent.logging import get_logger
from myagent.server.control import TurnRegistry

log = get_logger(__name__)

router = APIRouter()


class ChatBody(BaseModel):
    """POST /chat request body."""

    message: str
    session_id: str | None = None


async def _turn_events(
    loop: AgentLoop,
    session_id: str,
    message: str,
    registry: TurnRegistry | None = None,
) -> AsyncIterator[str]:
    """Run one turn and yield wire-protocol payloads as JSON strings.

    ``done`` is emitted exactly once, when the whole turn ends. The loop's
    per-step ``done`` chunks are internal boundaries between tool steps -
    forwarding them would tell the UI the answer is finished while the
    assistant is still working.

    The turn registers a cancellation handle so ``POST /stop`` can end a typed
    answer mid-stream, exactly as barge-in ends a spoken one.
    """
    yield json.dumps({"session_id": session_id})
    model: str | None = None
    cancel = asyncio.Event()
    if registry is not None:
        registry.register(cancel)
    try:
        async for chunk in loop.respond(session_id, message, cancel=cancel):
            if chunk.reset:
                yield json.dumps({"reset": True})
            if chunk.delta:
                yield json.dumps({"delta": chunk.delta})
            if chunk.done:
                model = chunk.model_key
    except GatewayError as exc:
        log.warning("turn_failed", session=session_id, error=str(exc))
        yield json.dumps({"error": str(exc)})
        return
    finally:
        if registry is not None:
            registry.discard(cancel)
    yield json.dumps({"done": True, "model": model, "stopped": cancel.is_set()})


@router.post("/chat")
async def chat(body: ChatBody, request: Request) -> StreamingResponse:
    """Stream one conversation turn as Server-Sent Events."""
    loop: AgentLoop = request.app.state.loop
    registry: TurnRegistry = request.app.state.turns
    session_id = loop.ensure_session(body.session_id)

    async def sse() -> AsyncIterator[str]:
        async for payload in _turn_events(loop, session_id, body.message, registry):
            yield f"data: {payload}\n\n"

    return StreamingResponse(sse(), media_type="text/event-stream")


@router.get("/sessions")
async def sessions(request: Request) -> list[dict[str, object]]:
    """All sessions, newest first."""
    loop: AgentLoop = request.app.state.loop
    return list_sessions(loop.db_path)


@router.get("/sessions/{session_id}")
async def session_messages(session_id: str, request: Request) -> list[dict[str, object]]:
    """Chronological messages of one session."""
    loop: AgentLoop = request.app.state.loop
    if not session_exists(loop.db_path, session_id):
        raise HTTPException(status_code=404, detail="unknown session")
    return get_messages(loop.db_path, session_id)


@router.websocket("/ws")
async def chat_ws(websocket: WebSocket) -> None:
    """Persistent chat connection: one JSON request in, streamed events out."""
    loop: AgentLoop = websocket.app.state.loop
    registry: TurnRegistry = websocket.app.state.turns
    await websocket.accept()
    try:
        while True:
            raw = await websocket.receive_text()
            try:
                body = ChatBody.model_validate_json(raw)
            except ValueError:
                await websocket.send_text(json.dumps({"error": "invalid request"}))
                continue
            session_id = loop.ensure_session(body.session_id)
            async for payload in _turn_events(loop, session_id, body.message, registry):
                await websocket.send_text(payload)
    except WebSocketDisconnect:
        return
