"""Confirmation channel: ask the human, wait for the answer.

An in-flight tool call parks on an asyncio Future while the request is pushed
to every connected UI (and, later, voice). The first answer wins; a timeout
counts as denial - silence must never approve an action.

Requests carry a concrete, human-readable ``summary`` built by the tool
itself (real paths, the real command), because "allow action?" trains people
to click yes without reading (SEC-02).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from myagent.logging import get_logger

log = get_logger(__name__)

CONFIRM_TIMEOUT_SECONDS = 300.0  # unanswered prompts deny after 5 minutes


@dataclass
class ConfirmationRequest:
    """One pending permission question."""

    id: str
    tool: str
    tier: str
    summary: str
    reason: str
    args: dict[str, Any]
    session_id: str

    def to_payload(self) -> dict[str, Any]:
        """Wire form sent to clients."""
        return {
            "type": "confirm_request",
            "id": self.id,
            "tool": self.tool,
            "tier": self.tier,
            "summary": self.summary,
            "reason": self.reason,
            "args": self.args,
        }


@dataclass
class Answer:
    """A human's response: allow (with scope) or deny."""

    allowed: bool
    scope: str = "once"  # once | session | always


Notifier = Callable[[dict[str, Any]], Awaitable[None]]


@dataclass
class ConfirmationService:
    """Routes confirmation requests to clients and collects answers."""

    _pending: dict[str, asyncio.Future[Answer]] = field(default_factory=dict)
    _requests: dict[str, ConfirmationRequest] = field(default_factory=dict)
    _notifiers: list[Notifier] = field(default_factory=list)

    def add_notifier(self, notifier: Notifier) -> None:
        """Register a client sink (one per connected UI socket)."""
        self._notifiers.append(notifier)

    def remove_notifier(self, notifier: Notifier) -> None:
        if notifier in self._notifiers:
            self._notifiers.remove(notifier)

    @property
    def pending(self) -> list[dict[str, Any]]:
        """Outstanding requests, so a client that connects late can catch up."""
        return [request.to_payload() for request in self._requests.values()]

    async def ask(
        self,
        tool: str,
        tier: str,
        summary: str,
        reason: str,
        args: dict[str, Any],
        session_id: str,
        timeout: float = CONFIRM_TIMEOUT_SECONDS,
    ) -> Answer:
        """Ask the human and wait. No listener or no answer means denial."""
        request = ConfirmationRequest(
            id=str(uuid.uuid4()),
            tool=tool,
            tier=tier,
            summary=summary,
            reason=reason,
            args=args,
            session_id=session_id,
        )
        if not self._notifiers:
            log.warning("confirmation_impossible", tool=tool)
            return Answer(allowed=False)

        future: asyncio.Future[Answer] = asyncio.get_running_loop().create_future()
        self._pending[request.id] = future
        self._requests[request.id] = request
        payload = request.to_payload()
        for notifier in list(self._notifiers):
            try:
                await notifier(payload)
            except Exception:  # a dead socket must not block the prompt
                log.debug("notifier_failed", tool=tool)
        try:
            return await asyncio.wait_for(future, timeout)
        except TimeoutError:
            log.warning("confirmation_timeout", tool=tool)
            return Answer(allowed=False)
        finally:
            self._pending.pop(request.id, None)
            self._requests.pop(request.id, None)
            for notifier in list(self._notifiers):
                with_close = {"type": "confirm_closed", "id": request.id}
                asyncio.ensure_future(_safe_notify(notifier, with_close))  # noqa: RUF006

    def resolve(self, request_id: str, answer: Answer) -> bool:
        """Deliver a human's answer; False if the request is already gone."""
        future = self._pending.get(request_id)
        if future is None or future.done():
            return False
        future.set_result(answer)
        return True

    def deny_all(self, reason: str = "emergency stop") -> int:
        """Deny every pending request (used by the kill switch)."""
        count = 0
        for future in list(self._pending.values()):
            if not future.done():
                future.set_result(Answer(allowed=False))
                count += 1
        if count:
            log.warning("confirmations_denied", count=count, reason=reason)
        return count


async def _safe_notify(notifier: Notifier, payload: dict[str, Any]) -> None:
    try:
        await notifier(payload)
    except Exception:
        log.debug("notifier_close_failed")
