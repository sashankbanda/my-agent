"""Memory store: standing facts and keyword search over transcripts.

M2 retrieval is FTS5 + recency, per the v3 staging plan (finding F10): vector
search arrives in M8 behind this module's unchanged ``search`` signature.
Facts are small, explicit, user-auditable records ("the user prefers X");
episodes are the immutable transcript ground truth.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from myagent.db import connection
from myagent.events import EventType, append_event
from myagent.gateway.privacy import contains_secret
from myagent.gateway.types import PrivacyClass

RECENCY_HALF_LIFE_DAYS = 30.0  # a hit this old scores half as much as a fresh one
SEARCH_OVERSAMPLE = 4  # fetch k*N by keyword rank, then re-rank with recency


def add_fact(
    db_path: Path,
    content: str,
    type_: str = "fact",
    provenance: str = "user",
) -> int:
    """Store one standing fact and return its id.

    Facts containing secret material are auto-classified ``local_only`` so
    they can never ride a prompt to a cloud provider.
    """
    privacy = PrivacyClass.LOCAL_ONLY if contains_secret(content) else PrivacyClass.CLOUD_OK
    with connection(db_path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO memory_items (type, content, provenance, privacy_class)
            VALUES (?, ?, ?, ?)
            """,
            (type_, content, provenance, privacy.value),
        )
        item_id = cursor.lastrowid
        assert item_id is not None
        append_event(conn, EventType.MEMORY_WRITTEN, {"id": item_id, "type": type_})
    return item_id


def forget(db_path: Path, item_id: int) -> bool:
    """Delete one fact; True if it existed. The right-to-forget is absolute."""
    with connection(db_path) as conn:
        cursor = conn.execute("DELETE FROM memory_items WHERE id = ?", (item_id,))
        removed = cursor.rowcount > 0
        if removed:
            append_event(conn, EventType.MEMORY_FORGOTTEN, {"id": item_id})
    return removed


def list_facts(db_path: Path) -> list[dict[str, Any]]:
    """All standing facts, newest first (memory viewer + context assembly)."""
    with connection(db_path) as conn:
        rows = conn.execute(
            """
            SELECT id, type, content, provenance, confidence, privacy_class, created_at
            FROM memory_items ORDER BY id DESC
            """
        ).fetchall()
    return [dict(row) for row in rows]


def _recency_weight(ts: str, now: datetime) -> float:
    """Exponential decay by message age; malformed timestamps count as old."""
    try:
        age_days = (now - datetime.fromisoformat(ts.replace("Z", "+00:00"))).days
    except ValueError:
        return 0.25
    return math.pow(0.5, max(age_days, 0) / RECENCY_HALF_LIFE_DAYS)


def _fts_query(query: str) -> str:
    """Sanitize free text into an OR-of-terms FTS5 query (never raises)."""
    terms = ["".join(ch for ch in term if ch.isalnum()) for term in query.split()]
    terms = [term for term in terms if term]
    return " OR ".join(f'"{term}"' for term in terms)


def search_messages(
    db_path: Path,
    query: str,
    k: int = 6,
    exclude_session: str | None = None,
) -> list[dict[str, Any]]:
    """Top-k past messages by combined keyword relevance and recency.

    ``exclude_session`` drops hits from the current conversation - those are
    already in the recent-messages window and would only duplicate context.
    """
    fts = _fts_query(query)
    if not fts:
        return []
    sql = """
        SELECT m.id, m.session_id, m.role, m.content, m.ts,
               bm25(messages_fts) AS keyword_rank
        FROM messages_fts
        JOIN messages m ON m.id = messages_fts.rowid
        WHERE messages_fts MATCH ?
    """
    params: list[Any] = [fts]
    if exclude_session is not None:
        sql += " AND m.session_id != ?"
        params.append(exclude_session)
    sql += " ORDER BY keyword_rank LIMIT ?"
    params.append(k * SEARCH_OVERSAMPLE)

    with connection(db_path) as conn:
        rows = [dict(row) for row in conn.execute(sql, params)]

    now = datetime.now(UTC)
    for row in rows:
        # bm25 is smaller-is-better; normalize to bigger-is-better, then decay.
        keyword_score = 1.0 / (1.0 + max(row.pop("keyword_rank"), 0.0))
        row["score"] = keyword_score * _recency_weight(row["ts"], now)
    rows.sort(key=lambda row: row["score"], reverse=True)
    return rows[:k]
