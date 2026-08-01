"""In-process event broadcaster: the live feed behind every UI.

The append-only ``events`` table is the system's record of what happened; this
module is the *push* side of the same information, so a HUD or overlay sees
things as they occur instead of polling.

Thread safety matters here: events are appended from the event loop, from tool
worker threads, and from the voice bridge. ``publish`` is therefore safe to
call from any thread - it hands the payload to the loop that owns the
subscriber queues.

Subscribers are slow-consumer-safe: a queue that fills up drops its oldest
entries rather than blocking the kernel. A UI that cannot keep up loses
history, never correctness (the database still has everything).
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from myagent.logging import get_logger

log = get_logger(__name__)

QUEUE_LIMIT = 500


class EventBroadcaster:
    """Fan-out of live kernel events to any number of subscribers."""

    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
        """Record the loop that owns the subscriber queues (called at startup)."""
        self._loop = loop or asyncio.get_running_loop()

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=QUEUE_LIMIT)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._subscribers.discard(queue)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def publish(self, payload: dict[str, Any]) -> None:
        """Push one payload to all subscribers, from any thread."""
        if not self._subscribers:
            return
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            self._deliver(payload)
        else:
            with contextlib.suppress(RuntimeError):  # loop shutting down
                loop.call_soon_threadsafe(self._deliver, payload)

    def _deliver(self, payload: dict[str, Any]) -> None:
        for queue in list(self._subscribers):
            if queue.full():
                with contextlib.suppress(asyncio.QueueEmpty):
                    queue.get_nowait()  # drop oldest: a stalled UI must not stall us
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(payload)


# One broadcaster per process; the kernel binds its loop at startup.
broadcaster = EventBroadcaster()
