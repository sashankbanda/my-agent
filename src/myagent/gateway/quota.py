"""Quota governor: persisted, preemptive free-tier accounting.

Routing is preemptive: a model whose bucket is empty is never attempted, so
the system fails over *before* receiving a 429, not after. Buckets persist in
SQLite so daily (rpd) counts survive restarts.

Background (non-interactive) work may not draw a bucket below the reserved
interactive headroom, so batch jobs can never mute the assistant for the rest
of the day (FR-LLM-09).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from myagent.db import connection, transaction
from myagent.gateway.types import ModelSpec

INTERACTIVE_HEADROOM = 0.30

_WINDOWS = ("rpm", "rpd", "tpm")


def _limit(spec: ModelSpec, window: str) -> int:
    return {"rpm": spec.rpm, "rpd": spec.rpd, "tpm": spec.tpm}[window]


def _next_reset(window: str, now: float) -> float:
    """Epoch seconds when a fresh bucket for this window resets."""
    if window in ("rpm", "tpm"):
        return now + 60.0
    # rpd: next UTC midnight - matches how providers describe daily limits.
    today = datetime.fromtimestamp(now, tz=UTC)
    midnight = (today + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.timestamp()


class QuotaGovernor:
    """Token-bucket accounting per model and window, persisted in SQLite."""

    def __init__(self, db_path: Path, now: Callable[[], float] = time.time) -> None:
        self._db_path = db_path
        self._now = now

    def can_use(self, spec: ModelSpec, interactive: bool = True) -> bool:
        """True if every window has room for one more request by this caller.

        Interactive callers may use the full limit; background callers stop at
        ``limit * (1 - INTERACTIVE_HEADROOM)``.
        """
        now = self._now()
        with connection(self._db_path) as conn:
            for window in _WINDOWS:
                row = conn.execute(
                    "SELECT count, reset_at FROM quota_buckets WHERE model_key = ? AND window = ?",
                    (spec.key, window),
                ).fetchone()
                if row is None or now >= row["reset_at"]:
                    continue  # empty or expired bucket: room by definition
                limit = _limit(spec, window)
                ceiling = limit if interactive else int(limit * (1 - INTERACTIVE_HEADROOM))
                if row["count"] >= ceiling:
                    return False
        return True

    def record_request(self, spec: ModelSpec) -> None:
        """Count one request against the rpm and rpd windows."""
        self._add(spec, {"rpm": 1, "rpd": 1})

    def record_tokens(self, spec: ModelSpec, tokens: int) -> None:
        """Count consumed tokens against the tpm window."""
        if tokens > 0:
            self._add(spec, {"tpm": tokens})

    def _add(self, spec: ModelSpec, amounts: dict[str, int]) -> None:
        now = self._now()
        with connection(self._db_path) as conn, transaction(conn):
            for window, amount in amounts.items():
                row = conn.execute(
                    "SELECT count, reset_at FROM quota_buckets WHERE model_key = ? AND window = ?",
                    (spec.key, window),
                ).fetchone()
                if row is None or now >= row["reset_at"]:
                    conn.execute(
                        """
                        INSERT INTO quota_buckets (model_key, window, count, reset_at)
                        VALUES (?, ?, ?, ?)
                        ON CONFLICT (model_key, window)
                        DO UPDATE SET count = excluded.count, reset_at = excluded.reset_at
                        """,
                        (spec.key, window, amount, _next_reset(window, now)),
                    )
                else:
                    conn.execute(
                        """
                        UPDATE quota_buckets SET count = count + ?
                        WHERE model_key = ? AND window = ?
                        """,
                        (amount, spec.key, window),
                    )

    def usage(self, spec: ModelSpec) -> dict[str, tuple[int, int]]:
        """Current ``window -> (count, limit)`` snapshot (for doctor / UI)."""
        now = self._now()
        snapshot: dict[str, tuple[int, int]] = {}
        with connection(self._db_path) as conn:
            for window in _WINDOWS:
                row = conn.execute(
                    "SELECT count, reset_at FROM quota_buckets WHERE model_key = ? AND window = ?",
                    (spec.key, window),
                ).fetchone()
                count = 0 if row is None or now >= row["reset_at"] else row["count"]
                snapshot[window] = (count, _limit(spec, window))
        return snapshot
