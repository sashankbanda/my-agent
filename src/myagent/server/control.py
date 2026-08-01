"""Live control of a running turn: stop talking, and mute the microphone.

Two things a person needs from an assistant that is already speaking, both of
which have to work in well under a second:

**Stop** must mean stop. The kill switch was the only "stop" the UI had, and
it blocks *future* tool calls - it does not end the sentence being spoken or
the answer being generated, so clicking it while the assistant talked did
nothing audible. Stopping properly needs three things at once: cancel the
in-flight turn, discard queued speech, and silence the speaker.

**Mute** must be authoritative at the microphone. Gating on the kernel side
would still let a conversation with someone else in the room be captured,
transcribed, and uploaded to a cloud STT provider before anything rejected it.
The satellite therefore owns the mute state and drops frames before they reach
wake-word detection; the kernel is only a remote control. Either side can flip
it (HUD button, overlay menu, or the global hotkey in the voice process), and
whichever does tells the other, so there is one shared truth.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import APIRouter, Request, WebSocket
from pydantic import BaseModel

from myagent.db import connection
from myagent.events import EventType, append_event
from myagent.logging import get_logger

log = get_logger(__name__)

router = APIRouter()


class VoiceLink:
    """The kernel's handle on the connected voice satellite, if any.

    At most one satellite is attached at a time; commands sent while none is
    connected are dropped, not queued - a stale "stop" delivered minutes later
    to a fresh session would be worse than no stop at all.
    """

    def __init__(self) -> None:
        self._socket: WebSocket | None = None

    def attach(self, socket: WebSocket) -> None:
        """Adopt a newly connected satellite as the command target."""
        self._socket = socket

    def detach(self, socket: WebSocket) -> None:
        """Forget a satellite, unless it was already replaced by a newer one."""
        if self._socket is socket:
            self._socket = None

    @property
    def connected(self) -> bool:
        """True when a satellite is attached to receive commands."""
        return self._socket is not None

    async def send(self, payload: dict[str, Any]) -> bool:
        """Send one command frame; False if nothing was listening."""
        socket = self._socket
        if socket is None:
            return False
        try:
            await socket.send_text(json.dumps(payload))
        except (RuntimeError, OSError):  # socket closed between check and send
            return False
        return True


class TurnRegistry:
    """Every turn currently generating, across voice and text alike.

    Stop has to reach a typed answer mid-stream as well as a spoken one, so
    cancellation is tracked here rather than inside either transport.
    """

    def __init__(self) -> None:
        self._events: set[asyncio.Event] = set()

    def register(self, cancel: asyncio.Event) -> None:
        """Track a turn's cancellation handle for its lifetime."""
        self._events.add(cancel)

    def discard(self, cancel: asyncio.Event) -> None:
        """Stop tracking a finished turn."""
        self._events.discard(cancel)

    def cancel_all(self) -> int:
        """Signal every in-flight turn to stop; returns how many were running."""
        stopped = 0
        for cancel in list(self._events):
            if not cancel.is_set():
                cancel.set()
                stopped += 1
        return stopped

    @property
    def active(self) -> int:
        """How many turns are currently generating."""
        return len(self._events)


class MuteBody(BaseModel):
    """POST /voice/mute body; omit ``muted`` to toggle."""

    muted: bool | None = None


@router.post("/stop")
async def stop(request: Request) -> dict[str, Any]:
    """Stop talking and stop thinking, right now.

    Distinct from ``/kill``: this ends the current turn, while the kill switch
    is a standing block on all actions. Stop is the one you press because the
    answer is wrong or too long; kill is the one you press because something
    is happening that must not happen.
    """
    app = request.app
    registry: TurnRegistry = app.state.turns
    link: VoiceLink = app.state.voice_link
    stopped = registry.cancel_all()
    silenced = await link.send({"type": "stop"})
    with connection(app.state.loop.db_path) as conn:
        append_event(conn, EventType.USER_STOPPED, {"turns": stopped, "silenced": silenced})
    log.info("stop_requested", turns=stopped, silenced=silenced)
    return {"stopped": stopped, "silenced": silenced}


@router.post("/voice/mute")
async def set_mute(request: Request, body: MuteBody | None = None) -> dict[str, Any]:
    """Mute or unmute the microphone (no body, or no ``muted``, toggles it)."""
    app = request.app
    link: VoiceLink = app.state.voice_link
    wanted = body.muted if body is not None else None
    muted = (not app.state.voice_muted) if wanted is None else wanted
    delivered = await link.send({"type": "set_mute", "value": muted})
    apply_mute(app, muted)
    return {"muted": muted, "delivered": delivered}


@router.get("/voice/mute")
async def mute_status(request: Request) -> dict[str, Any]:
    """Whether the microphone is currently muted."""
    return {"muted": bool(request.app.state.voice_muted)}


def apply_mute(app: Any, muted: bool) -> None:
    """Record the mute state and announce it once.

    Called both when a UI requests the change and when the voice process
    reports one it made itself (the global hotkey), so the two paths converge
    on the same state without either becoming the sole owner.
    """
    if bool(getattr(app.state, "voice_muted", False)) == muted:
        return
    app.state.voice_muted = muted
    with connection(app.state.loop.db_path) as conn:
        append_event(conn, EventType.VOICE_MUTED, {"muted": muted})
    log.info("voice_mute", muted=muted)
