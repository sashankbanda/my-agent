"""Conversation persistence: sessions and messages.

Plain repository functions over SQLite. Sessions are identified by UUID
strings; a session's title defaults to its first user message (truncated) so
the UI has something meaningful to list.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from myagent.db import connection, transaction

TITLE_MAX_LENGTH = 60


def create_session(db_path: Path) -> str:
    """Create a session and return its id."""
    session_id = str(uuid.uuid4())
    with connection(db_path) as conn:
        conn.execute("INSERT INTO sessions (id) VALUES (?)", (session_id,))
    return session_id


def session_exists(db_path: Path, session_id: str) -> bool:
    """True if the session id is known."""
    with connection(db_path) as conn:
        row = conn.execute("SELECT 1 FROM sessions WHERE id = ?", (session_id,)).fetchone()
    return row is not None


def list_sessions(db_path: Path) -> list[dict[str, Any]]:
    """All sessions, newest first."""
    with connection(db_path) as conn:
        rows = conn.execute(
            "SELECT id, created_at, title FROM sessions ORDER BY created_at DESC"
        ).fetchall()
    return [dict(row) for row in rows]


def append_message(
    db_path: Path,
    session_id: str,
    role: str,
    content: str,
    provider: str | None = None,
    model: str | None = None,
    tokens: int | None = None,
) -> None:
    """Persist one message; the first user message also titles the session."""
    with connection(db_path) as conn, transaction(conn):
        conn.execute(
            """
            INSERT INTO messages (session_id, role, content, provider, model, tokens)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (session_id, role, content, provider, model, tokens),
        )
        if role == "user":
            conn.execute(
                "UPDATE sessions SET title = ? WHERE id = ? AND title = ''",
                (content[:TITLE_MAX_LENGTH], session_id),
            )


def get_messages(db_path: Path, session_id: str, limit: int | None = None) -> list[dict[str, Any]]:
    """Messages of a session in chronological order (the last ``limit`` if set)."""
    query = (
        "SELECT role, content, ts, provider, model, tokens FROM messages "
        "WHERE session_id = ? ORDER BY id"
    )
    with connection(db_path) as conn:
        rows = conn.execute(query, (session_id,)).fetchall()
    messages = [dict(row) for row in rows]
    if limit is not None:
        messages = messages[-limit:]
    return messages
