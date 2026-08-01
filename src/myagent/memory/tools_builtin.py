"""Memory operations exposed as callable functions.

These are the ``remember``/``forget`` operations the M4 tool registry will
mount for the model; until then they are wired directly to the memory API
endpoints (and thus the UI). Keeping them here - not inline in the server -
means M4 registers them with a decorator and nothing else moves.
"""

from __future__ import annotations

from pathlib import Path

from myagent.memory import store


def remember(db_path: Path, content: str, type_: str = "fact") -> dict[str, int | str]:
    """Persist one standing fact about the user; returns its id."""
    stripped = content.strip()
    if not stripped:
        raise ValueError("cannot remember empty content")
    item_id = store.add_fact(db_path, stripped, type_=type_)
    return {"id": item_id, "status": "remembered"}


def forget(db_path: Path, item_id: int) -> dict[str, int | str]:
    """Remove one fact permanently; raises KeyError if it does not exist."""
    if not store.forget(db_path, item_id):
        raise KeyError(f"no memory item with id {item_id}")
    return {"id": item_id, "status": "forgotten"}
