"""Tool execution: the chokepoint.

Every effect the assistant has on the world flows through ``execute``:

    model tool call -> broker.authorize -> (confirm?) -> run -> audit event

There is deliberately no other path from a tool call to an effect. The model
cannot skip the broker because it never calls tool functions directly - it
emits a name and arguments, and this module decides what happens next.

Tool bodies are synchronous and may block (filesystem, subprocess), so they
run in a worker thread; the confirmation wait is async.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

from myagent.config import Settings
from myagent.db import connection
from myagent.events import EventType, append_event
from myagent.logging import get_logger
from myagent.security.broker import PermissionBroker
from myagent.security.confirm import ConfirmationService
from myagent.security.taint import TurnContext
from myagent.security.tiers import Decision
from myagent.tools.registry import ToolContext, ToolError, get_tool

log = get_logger(__name__)

TOOL_TIMEOUT_SECONDS = 120.0


class ToolExecutor:
    """Authorizes and runs tool calls for one kernel instance."""

    def __init__(
        self,
        db_path: Path,
        settings: Settings,
        broker: PermissionBroker,
        confirmations: ConfirmationService,
    ) -> None:
        self._db_path = db_path
        self._settings = settings
        self._broker = broker
        self._confirmations = confirmations

    async def execute(self, name: str, args: dict[str, Any], turn: TurnContext) -> dict[str, Any]:
        """Run one tool call, returning a result dict for the model.

        Never raises for expected failures: denials, unknown tools, and tool
        errors come back as ``{"error": ...}`` so the loop can feed them to
        the model as observations and let it adapt.
        """
        started = time.perf_counter()
        try:
            spec = get_tool(name)
        except ToolError as exc:
            return {"error": str(exc)}

        self._emit(EventType.TOOL_CALL_REQUESTED, {"tool": name, "args": args}, turn)

        decision, reason = self._broker.authorize(name, spec.tier, args, turn)
        if decision is Decision.DENY:
            return {"error": f"denied: {reason}"}
        if decision is Decision.CONFIRM:
            answer = await self._confirmations.ask(
                tool=name,
                tier=spec.tier.label,
                summary=spec.summary(args),
                reason=reason,
                args=args,
                session_id=turn.session_id,
            )
            self._emit(
                EventType.CONFIRMATION_RESOLVED,
                {"tool": name, "allowed": answer.allowed, "scope": answer.scope},
                turn,
            )
            if not answer.allowed:
                return {"error": "the user declined this action"}
            if answer.scope in ("session", "always"):
                self._broker.add_grant(name, answer.scope, turn.session_id)

        # Re-check the kill switch: it may have been engaged while we waited
        # for confirmation, and an emergency stop must win that race.
        if self._broker.kill_switch.engaged:
            return {"error": "denied: emergency stop is engaged"}

        context = ToolContext(turn=turn, db_path=self._db_path, settings=self._settings)
        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(lambda: spec.func(context, **args)),
                timeout=TOOL_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            result = {"error": f"{name} timed out after {TOOL_TIMEOUT_SECONDS:.0f}s"}
        except ToolError as exc:
            result = {"error": str(exc)}
        except TypeError as exc:  # bad arguments from the model
            result = {"error": f"invalid arguments for {name}: {exc}"}
        except Exception as exc:  # unexpected: log fully, tell the model plainly
            log.exception("tool_crashed", tool=name)
            result = {"error": f"{name} failed unexpectedly: {type(exc).__name__}: {exc}"}

        elapsed_ms = int((time.perf_counter() - started) * 1000)
        self._emit(
            EventType.TOOL_CALL_COMPLETED,
            {
                "tool": name,
                "ok": "error" not in result,
                "error": result.get("error"),
                "ms": elapsed_ms,
            },
            turn,
        )
        return result

    def _emit(self, type_: EventType, data: dict[str, Any], turn: TurnContext) -> None:
        with connection(self._db_path) as conn:
            append_event(conn, type_, data, turn.session_id)
