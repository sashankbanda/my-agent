"""Memory store tests: facts, forgetting, and FTS + recency search."""

from __future__ import annotations

import sqlite3

from myagent.config import Settings
from myagent.core import history
from myagent.memory import store


def seed_message(settings: Settings, session: str, role: str, content: str) -> None:
    history.append_message(settings.db_path(), session, role, content)


def test_add_list_forget_fact(db: sqlite3.Connection, settings: Settings) -> None:
    item_id = store.add_fact(settings.db_path(), "prefers dark roast coffee")
    facts = store.list_facts(settings.db_path())
    assert [fact["id"] for fact in facts] == [item_id]
    assert facts[0]["privacy_class"] == "cloud_ok"
    assert store.forget(settings.db_path(), item_id) is True
    assert store.list_facts(settings.db_path()) == []
    assert store.forget(settings.db_path(), item_id) is False


def test_memory_events_are_logged(db: sqlite3.Connection, settings: Settings) -> None:
    item_id = store.add_fact(settings.db_path(), "likes hiking")
    store.forget(settings.db_path(), item_id)
    types = [row["type"] for row in db.execute("SELECT type FROM events ORDER BY id")]
    assert types == ["MemoryWritten", "MemoryForgotten"]


def test_secret_fact_is_local_only(db: sqlite3.Connection, settings: Settings) -> None:
    store.add_fact(settings.db_path(), "wifi password: hunter2secret")
    facts = store.list_facts(settings.db_path())
    assert facts[0]["privacy_class"] == "local_only"


def test_search_finds_relevant_message(db: sqlite3.Connection, settings: Settings) -> None:
    session = history.create_session(settings.db_path())
    seed_message(settings, session, "user", "my dentist appointment is on Thursday")
    seed_message(settings, session, "user", "I love pizza with mushrooms")
    hits = store.search_messages(settings.db_path(), "when is my dentist visit?")
    assert hits
    assert "dentist" in hits[0]["content"]


def test_search_excludes_current_session(db: sqlite3.Connection, settings: Settings) -> None:
    current = history.create_session(settings.db_path())
    other = history.create_session(settings.db_path())
    seed_message(settings, current, "user", "talking about quasars right now")
    seed_message(settings, other, "user", "we discussed quasars last week")
    hits = store.search_messages(settings.db_path(), "quasars", exclude_session=current)
    assert hits
    assert all(hit["session_id"] == other for hit in hits)


def test_search_prefers_recent_on_equal_relevance(
    db: sqlite3.Connection, settings: Settings
) -> None:
    session = history.create_session(settings.db_path())
    seed_message(settings, session, "user", "the falcon project deadline")
    seed_message(settings, session, "user", "the falcon project deadline")
    db.execute(
        "UPDATE messages SET ts = '2020-01-01T00:00:00.000Z' WHERE id = "
        "(SELECT MIN(id) FROM messages)"
    )
    hits = store.search_messages(settings.db_path(), "falcon project")
    assert len(hits) == 2
    assert hits[0]["ts"] > hits[1]["ts"]  # fresh hit outranks the 2020 one


def test_search_survives_hostile_query(db: sqlite3.Connection, settings: Settings) -> None:
    """FTS syntax characters in user text must never raise."""
    session = history.create_session(settings.db_path())
    seed_message(settings, session, "user", "hello world")
    for query in ('"unbalanced', "NEAR(", "a* AND (b OR", "-- ; DROP TABLE", "()[]{}"):
        store.search_messages(settings.db_path(), query)  # must not raise


def test_fts_stays_in_sync_after_delete(db: sqlite3.Connection, settings: Settings) -> None:
    session = history.create_session(settings.db_path())
    seed_message(settings, session, "user", "ephemeral xylophone message")
    db.execute("DELETE FROM messages")
    hits = store.search_messages(settings.db_path(), "xylophone")
    assert hits == []
