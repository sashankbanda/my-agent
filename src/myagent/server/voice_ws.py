"""Voice bridge: the kernel side of the voice-satellite WebSocket.

Wire protocol (JSON text frames):
  voice -> kernel:
    {"type": "utterance", "text": "..."}   final transcription; run one turn
    {"type": "cancel"}                     barge-in: stop the current turn now
    {"type": "state", "value": "..."}      what the edge is doing, for the UIs
    {"type": "mute", "value": bool}        the satellite muted itself (hotkey)
  kernel -> voice:
    {"type": "session", "session_id": "..."}       once, on connect
    {"type": "say", "text": "...", "seq": n}       one speakable sentence
    {"type": "turn_done", "full_text": "..."}      turn finished (or was cancelled)
    {"type": "error", "message": "..."}            turn failed honestly
    {"type": "stop"}                               silence playback immediately
    {"type": "set_mute", "value": bool}            mute/unmute the microphone

Replies are chunked at sentence boundaries so speech synthesis starts on the
first sentence while the model is still writing the rest (NFR-PERF-01).
Perceived latency is first-audio, not last-token.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from myagent.core.loop import AgentLoop
from myagent.db import connection
from myagent.events import EventType, append_event
from myagent.gateway.types import GatewayError
from myagent.logging import get_logger
from myagent.server.control import TurnRegistry, VoiceLink, apply_mute
from myagent.server.events_ws import publish_state

log = get_logger(__name__)

router = APIRouter()

MIN_SENTENCE_CHARS = 20  # don't ship fragments like "Dr." as sentences
_SENTENCE_END = re.compile(r"[.!?…](?=[\s\"')\]]|$)")


def split_sentences(buffer: str) -> tuple[list[str], str]:
    """Split off complete sentences; return (sentences, remaining buffer).

    A split point is a sentence-ending mark followed by whitespace (or end of
    buffer), but only once the candidate sentence is long enough to be worth
    speaking - short abbreviations ride along with the next chunk.
    """
    sentences: list[str] = []
    start = 0
    for match in _SENTENCE_END.finditer(buffer):
        end = match.end()
        candidate = buffer[start:end].strip()
        if len(candidate) >= MIN_SENTENCE_CHARS:
            sentences.append(candidate)
            start = end
    return sentences, buffer[start:]


class _Turn:
    """One in-flight voice turn and its cancellation handle."""

    def __init__(self) -> None:
        self.cancel = asyncio.Event()
        self.task: asyncio.Task[None] | None = None


async def _run_turn(
    websocket: WebSocket,
    loop_: AgentLoop,
    session_id: str,
    text: str,
    turn: _Turn,
    registry: TurnRegistry | None = None,
) -> None:
    """Stream one turn to the voice client as speakable sentences."""
    buffer = ""
    full_text = ""
    seq = 0
    try:
        async for chunk in loop_.respond(session_id, text, cancel=turn.cancel):
            if chunk.reset:
                # The answer so far was withdrawn - a provider failed over, or
                # the loop rejected its own reply. Drop the unspoken remainder
                # AND cut playback: continuing to speak a retracted answer, then
                # speaking its replacement, is worse than a half-second stutter.
                buffer = ""
                full_text = ""
                await websocket.send_text(json.dumps({"type": "stop"}))
                continue
            buffer += chunk.delta
            full_text += chunk.delta
            sentences, buffer = split_sentences(buffer)
            for sentence in sentences:
                if turn.cancel.is_set():
                    break
                await websocket.send_text(json.dumps({"type": "say", "text": sentence, "seq": seq}))
                seq += 1
        remainder = buffer.strip()
        if remainder and not turn.cancel.is_set():
            await websocket.send_text(json.dumps({"type": "say", "text": remainder, "seq": seq}))
        await websocket.send_text(json.dumps({"type": "turn_done", "full_text": full_text}))
        with connection(loop_.db_path) as conn:
            append_event(conn, EventType.ASSISTANT_SAID, {"text": full_text}, session_id)
    except GatewayError as exc:
        log.warning("voice_turn_failed", session=session_id, error=str(exc))
        await websocket.send_text(json.dumps({"type": "error", "message": str(exc)}))
    finally:
        if registry is not None:
            registry.discard(turn.cancel)


@router.websocket("/voice")
async def voice_ws(websocket: WebSocket) -> None:
    """Persistent connection from the voice satellite process.

    Also mirrors voice activity into the event stream (state, transcripts) so
    the HUD and overlay can show what is happening without reading logs.
    """
    app = websocket.app
    loop_: AgentLoop = app.state.loop
    link: VoiceLink = app.state.voice_link
    registry: TurnRegistry = app.state.turns
    await websocket.accept()
    session_id = loop_.ensure_session(None)
    await websocket.send_text(json.dumps({"type": "session", "session_id": session_id}))

    link.attach(websocket)
    app.state.voice_connected = True
    with connection(loop_.db_path) as conn:
        append_event(conn, EventType.VOICE_CONNECTED, {"session": session_id})
    publish_state(app, "idle", loop_.db_path)

    current: _Turn | None = None
    try:
        while True:
            frame = json.loads(await websocket.receive_text())
            kind = frame.get("type")
            if kind == "cancel":
                if current is not None:
                    current.cancel.set()
                publish_state(app, "listening", loop_.db_path)
            elif kind == "state" and isinstance(frame.get("value"), str):
                publish_state(app, frame["value"], loop_.db_path)
            elif kind == "mute" and isinstance(frame.get("value"), bool):
                # The satellite's global hotkey fired; mirror it so the UIs agree.
                apply_mute(app, frame["value"])
            elif kind == "utterance" and isinstance(frame.get("text"), str):
                if current is not None and current.task is not None:
                    current.cancel.set()  # a new utterance supersedes the old turn
                    with contextlib.suppress(asyncio.CancelledError):
                        await current.task
                with connection(loop_.db_path) as conn:
                    append_event(conn, EventType.USER_SAID, {"text": frame["text"]}, session_id)
                publish_state(app, "thinking", loop_.db_path)
                current = _Turn()
                registry.register(current.cancel)
                current.task = asyncio.create_task(
                    _run_turn(websocket, loop_, session_id, frame["text"], current, registry)
                )
            else:
                await websocket.send_text(
                    json.dumps({"type": "error", "message": f"unknown frame: {kind}"})
                )
    except WebSocketDisconnect:
        if current is not None:
            current.cancel.set()
        return
    finally:
        link.detach(websocket)
        app.state.voice_connected = False
        publish_state(app, "offline", loop_.db_path)
        with connection(loop_.db_path) as conn:
            append_event(conn, EventType.VOICE_DISCONNECTED, {})
