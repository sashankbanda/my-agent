"""The permission broker: the single gate every tool call passes through.

Decision policy, in order:

1. Kill switch engaged            -> DENY (everything, immediately)
2. Tier READ                      -> ALLOW
3. Remote channel and tier >= T1  -> CONFIRM (grants do not apply remotely)
4. Turn is tainted and tier >= T1 -> CONFIRM (grants suspended - SEC-07)
5. Standing grant for the tool    -> ALLOW
6. Tier REVERSIBLE (local, clean) -> ALLOW
7. Otherwise (T2)                 -> CONFIRM

Every decision is written to the event log, which is what the audit view
reads. Nothing here consults the model: this is policy, not reasoning.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from myagent.db import connection
from myagent.events import EventType, append_event
from myagent.logging import get_logger
from myagent.security.taint import TurnContext
from myagent.security.tiers import Decision, Tier

log = get_logger(__name__)


class KillSwitch:
    """Process-wide emergency stop (SEC-04).

    Thread-safe and checked by the broker on every call, so engaging it halts
    all further tool execution within one tool boundary - typically well
    under the 500 ms budget.
    """

    def __init__(self) -> None:
        self._engaged = threading.Event()

    @property
    def engaged(self) -> bool:
        return self._engaged.is_set()

    def engage(self) -> None:
        self._engaged.set()
        log.warning("kill_switch_engaged")

    def release(self) -> None:
        self._engaged.clear()
        log.info("kill_switch_released")


class PermissionBroker:
    """Authorizes tool calls against tiers, taint, channel, and grants."""

    def __init__(self, db_path: Path, kill_switch: KillSwitch | None = None) -> None:
        self._db_path = db_path
        self.kill_switch = kill_switch or KillSwitch()

    # -- grants -------------------------------------------------------------

    def has_grant(self, tool: str, session_id: str) -> bool:
        """True if a session or always grant covers this tool."""
        with connection(self._db_path) as conn:
            row = conn.execute(
                """
                SELECT 1 FROM grants
                WHERE tool = ?
                  AND ((scope = 'always') OR (scope = 'session' AND session_id = ?))
                LIMIT 1
                """,
                (tool, session_id),
            ).fetchone()
        return row is not None

    def add_grant(self, tool: str, scope: str, session_id: str | None) -> None:
        """Record a standing decision ('session' or 'always')."""
        if scope not in ("session", "always"):
            raise ValueError(f"unsupported grant scope: {scope}")
        stored_session = session_id if scope == "session" else None
        with connection(self._db_path) as conn:
            conn.execute(
                """
                INSERT INTO grants (tool, scope, session_id) VALUES (?, ?, ?)
                ON CONFLICT (tool, scope, session_id) DO NOTHING
                """,
                (tool, scope, stored_session),
            )
            append_event(
                conn,
                EventType.GRANT_ADDED,
                {"tool": tool, "scope": scope},
                session_id,
            )

    def list_grants(self) -> list[dict[str, Any]]:
        """All standing grants (for the UI's security panel)."""
        with connection(self._db_path) as conn:
            rows = conn.execute(
                "SELECT id, tool, scope, session_id, created_at FROM grants ORDER BY id DESC"
            ).fetchall()
        return [dict(row) for row in rows]

    def revoke_grant(self, grant_id: int) -> bool:
        """Delete one grant; True if it existed."""
        with connection(self._db_path) as conn:
            cursor = conn.execute("DELETE FROM grants WHERE id = ?", (grant_id,))
            removed = cursor.rowcount > 0
            if removed:
                append_event(conn, EventType.GRANT_REVOKED, {"grant_id": grant_id})
        return removed

    # -- authorization ------------------------------------------------------

    def authorize(
        self, tool: str, tier: Tier, args: dict[str, Any], context: TurnContext
    ) -> tuple[Decision, str]:
        """Decide whether ``tool`` may run; returns (decision, reason).

        The reason string is surfaced to the user in the confirmation prompt
        and stored in the audit log, so it must be concrete and honest.
        """
        decision, reason = self._decide(tool, tier, context)
        with connection(self._db_path) as conn:
            append_event(
                conn,
                EventType.PERMISSION_DECIDED,
                {
                    "tool": tool,
                    "tier": tier.label,
                    "decision": decision.name,
                    "reason": reason,
                    "channel": context.channel,
                    "tainted": context.tainted,
                    "args": _redact(args),
                },
                context.session_id,
            )
        return decision, reason

    def _decide(self, tool: str, tier: Tier, context: TurnContext) -> tuple[Decision, str]:
        if self.kill_switch.engaged:
            return Decision.DENY, "emergency stop is engaged"
        if tier is Tier.READ:
            return Decision.ALLOW, "read-only"
        if context.is_remote:
            return Decision.CONFIRM, f"remote session ({context.channel}) - writes need approval"
        if context.tainted:
            return (
                Decision.CONFIRM,
                f"this turn read untrusted content ({context.describe_taint()}), "
                "so saved permissions are suspended",
            )
        if self.has_grant(tool, context.session_id):
            return Decision.ALLOW, "standing grant"
        if tier is Tier.REVERSIBLE:
            return Decision.ALLOW, "reversible action"
        return Decision.CONFIRM, "destructive or irreversible action"


_SECRET_ARG_NAMES = ("password", "token", "secret", "key", "credential")


def _redact(args: dict[str, Any]) -> dict[str, Any]:
    """Copy args with obviously-sensitive values masked before logging."""
    redacted: dict[str, Any] = {}
    for name, value in args.items():
        if any(marker in name.lower() for marker in _SECRET_ARG_NAMES):
            redacted[name] = "***"
        else:
            redacted[name] = value
    return redacted
