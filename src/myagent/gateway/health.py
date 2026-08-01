"""Provider health: failure counting with cooldown-based circuit breaking.

Deliberately simple (v3 review, finding F7): consecutive failures put a
provider on an exponentially growing cooldown; one success resets it. No
EWMA, no half-open state machine - at personal scale this 10-line policy
yields the same behavior.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

from myagent.db import connection, transaction

FAILURE_THRESHOLD = 3
BASE_COOLDOWN_SECONDS = 30.0
MAX_COOLDOWN_SECONDS = 15 * 60.0


def _cooldown_for(failures: int) -> float:
    """Cooldown duration once the threshold is reached; doubles per failure."""
    if failures < FAILURE_THRESHOLD:
        return 0.0
    return min(BASE_COOLDOWN_SECONDS * 2 ** (failures - FAILURE_THRESHOLD), MAX_COOLDOWN_SECONDS)


class HealthTracker:
    """Per-provider failure counter and cooldown clock, persisted in SQLite."""

    def __init__(self, db_path: Path, now: Callable[[], float] = time.time) -> None:
        self._db_path = db_path
        self._now = now

    def is_available(self, provider: str) -> bool:
        """False while the provider is cooling down after repeated failures."""
        with connection(self._db_path) as conn:
            row = conn.execute(
                "SELECT cooldown_until FROM provider_health WHERE provider = ?",
                (provider,),
            ).fetchone()
        return row is None or self._now() >= row["cooldown_until"]

    def record_failure(self, provider: str) -> None:
        """Count a failure; at the threshold, start (and then grow) a cooldown."""
        with connection(self._db_path) as conn, transaction(conn):
            row = conn.execute(
                "SELECT failures FROM provider_health WHERE provider = ?",
                (provider,),
            ).fetchone()
            failures = (row["failures"] if row else 0) + 1
            cooldown_until = self._now() + _cooldown_for(failures)
            conn.execute(
                """
                INSERT INTO provider_health (provider, failures, cooldown_until)
                VALUES (?, ?, ?)
                ON CONFLICT (provider)
                DO UPDATE SET failures = excluded.failures, cooldown_until = excluded.cooldown_until
                """,
                (provider, failures, cooldown_until),
            )

    def record_success(self, provider: str) -> None:
        """A successful call fully resets the provider's health."""
        with connection(self._db_path) as conn, transaction(conn):
            conn.execute(
                """
                INSERT INTO provider_health (provider, failures, cooldown_until)
                VALUES (?, 0, 0)
                ON CONFLICT (provider) DO UPDATE SET failures = 0, cooldown_until = 0
                """,
                (provider,),
            )
