"""Performance regression: retrieval stays under budget at 50k messages.

M2 exit criterion (NFR-PERF-05 at NFR-SCAL-01 scale): hybrid search under
400 ms with 50k synthetic messages on disk.
"""

from __future__ import annotations

import sqlite3
import time
import uuid

from myagent.config import Settings
from myagent.memory import store

MESSAGE_COUNT = 50_000
BUDGET_SECONDS = 0.400

WORDS = [
    "project",
    "deadline",
    "invoice",
    "meeting",
    "travel",
    "dentist",
    "birthday",
    "recipe",
    "workout",
    "budget",
    "laptop",
    "backup",
    "garden",
    "movie",
    "concert",
    "flight",
    "hotel",
    "deploy",
    "release",
    "bug",
]


def seed_bulk(db: sqlite3.Connection, count: int) -> None:
    session = str(uuid.uuid4())
    db.execute("INSERT INTO sessions (id) VALUES (?)", (session,))
    rows = (
        (
            session,
            "user",
            f"note {index}: the {WORDS[index % len(WORDS)]} for "
            f"{WORDS[(index * 7) % len(WORDS)]} number {index}",
        )
        for index in range(count)
    )
    db.execute("BEGIN")
    db.executemany("INSERT INTO messages (session_id, role, content) VALUES (?, ?, ?)", rows)
    db.execute("COMMIT")


def test_search_under_budget_at_50k_messages(db: sqlite3.Connection, settings: Settings) -> None:
    seed_bulk(db, MESSAGE_COUNT)

    # Warm-up excluded from timing: first query pays page-cache costs.
    store.search_messages(settings.db_path(), "warmup query")

    started = time.perf_counter()
    hits = store.search_messages(settings.db_path(), "dentist deadline invoice")
    elapsed = time.perf_counter() - started

    assert hits, "search returned nothing on a seeded corpus"
    assert elapsed < BUDGET_SECONDS, f"search took {elapsed * 1000:.0f} ms"
