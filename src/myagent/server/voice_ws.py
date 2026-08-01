"""Voice bridge: the kernel side of the voice-satellite WebSocket.

Wire protocol (JSON text frames):
  voice -> kernel:
    {"type": "utterance", "text": "..."}   final transcription; run one turn
    {"type": "cancel"}                     barge-in: stop the current turn now
  kernel -> voice:
    {"type": "session", "session_id": "..."}       once, on connect
    {"type": "say", "text": "...", "seq": n}       one speakable sentence
    {"type": "turn_done", "full_text": "..."}      turn finished (or was cancelled)
    {"type": "error", "message": "..."}            turn failed honestly

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
from myagent.gateway.types import GatewayError
from myagent.logging import get_logger

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
    websocket: WebSocket, loop_: AgentLoop, session_id: str, text: str, turn: _Turn
) -> None:
    """Stream one turn to the voice client as speakable sentences."""
    buffer = ""
    full_text = ""
    seq = 0
    try:
        async for chunk in loop_.respond(session_id, text, cancel=turn.cancel):
            if chunk.reset:
                # Provider failed over mid-answer. Sentences already spoken
                # cannot be unspoken; drop only the unspoken remainder and
                # continue with the new stream.
                buffer = ""
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
    except GatewayError as exc:
        log.warning("voice_turn_failed", session=session_id, error=str(exc))
        await websocket.send_text(json.dumps({"type": "error", "message": str(exc)}))


@router.websocket("/voice")
async def voice_ws(websocket: WebSocket) -> None:
    """Persistent connection from the voice satellite process."""
    loop_: AgentLoop = websocket.app.state.loop
    await websocket.accept()
    session_id = loop_.ensure_session(None)
    await websocket.send_text(json.dumps({"type": "session", "session_id": session_id}))

    current: _Turn | None = None
    try:
        while True:
            frame = json.loads(await websocket.receive_text())
            kind = frame.get("type")
            if kind == "cancel":
                if current is not None:
                    current.cancel.set()
            elif kind == "utterance" and isinstance(frame.get("text"), str):
                if current is not None and current.task is not None:
                    current.cancel.set()  # a new utterance supersedes the old turn
                    with contextlib.suppress(asyncio.CancelledError):
                        await current.task
                current = _Turn()
                current.task = asyncio.create_task(
                    _run_turn(websocket, loop_, session_id, frame["text"], current)
                )
            else:
                await websocket.send_text(
                    json.dumps({"type": "error", "message": f"unknown frame: {kind}"})
                )
    except WebSocketDisconnect:
        if current is not None:
            current.cancel.set()
        return
