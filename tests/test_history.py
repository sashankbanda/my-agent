"""Conversation persistence tests."""

from __future__ import annotations

import sqlite3

from myagent.config import Settings
from myagent.core import history


def test_create_and_list_sessions(db: sqlite3.Connection, settings: Settings) -> None:
    first = history.create_session(settings.db_path())
    second = history.create_session(settings.db_path())
    listed = [session["id"] for session in history.list_sessions(settings.db_path())]
    assert set(listed) == {first, second}
    assert history.session_exists(settings.db_path(), first)
    assert not history.session_exists(settings.db_path(), "nope")


def test_messages_round_trip_in_order(db: sqlite3.Connection, settings: Settings) -> None:
    session = history.create_session(settings.db_path())
    history.append_message(settings.db_path(), session, "user", "hello")
    history.append_message(
        settings.db_path(), session, "assistant", "hi!", provider="p1", model="p1/m", tokens=3
    )
    messages = history.get_messages(settings.db_path(), session)
    assert [(m["role"], m["content"]) for m in messages] == [
        ("user", "hello"),
        ("assistant", "hi!"),
    ]
    assert messages[1]["model"] == "p1/m"
    assert messages[1]["tokens"] == 3


def test_first_user_message_titles_session(db: sqlite3.Connection, settings: Settings) -> None:
    session = history.create_session(settings.db_path())
    history.append_message(settings.db_path(), session, "user", "plan my trip to Japan")
    history.append_message(settings.db_path(), session, "user", "actually, Korea")
    sessions = {s["id"]: s for s in history.list_sessions(settings.db_path())}
    assert sessions[session]["title"] == "plan my trip to Japan"


def test_history_survives_restart(db: sqlite3.Connection, settings: Settings) -> None:
    """Everything is read back through fresh connections - a restart in miniature."""
    session = history.create_session(settings.db_path())
    history.append_message(settings.db_path(), session, "user", "remember me")
    messages = history.get_messages(settings.db_path(), session)
    assert len(messages) == 1


def test_limit_returns_most_recent(db: sqlite3.Connection, settings: Settings) -> None:
    session = history.create_session(settings.db_path())
    for index in range(10):
        history.append_message(settings.db_path(), session, "user", f"message {index}")
    recent = history.get_messages(settings.db_path(), session, limit=3)
    assert [m["content"] for m in recent] == ["message 7", "message 8", "message 9"]
