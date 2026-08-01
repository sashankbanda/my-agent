"""Context assembler tests: budgets, privacy propagation, source layout."""

from __future__ import annotations

import sqlite3

from myagent.config import Settings
from myagent.core import history
from myagent.gateway.types import PrivacyClass
from myagent.memory import context, store
from myagent.tokens import estimate_tokens

PROMPT = "you are a test assistant"


def test_facts_are_injected(db: sqlite3.Connection, settings: Settings) -> None:
    store.add_fact(settings.db_path(), "the user's cat is named Miso")
    session = history.create_session(settings.db_path())
    bundle = context.assemble(settings.db_path(), session, "hi", PROMPT)
    joined = "\n".join(m.content for m in bundle.messages if m.role == "system")
    assert "Miso" in joined
    assert bundle.privacy_class is PrivacyClass.CLOUD_OK


def test_local_only_fact_taints_whole_bundle(db: sqlite3.Connection, settings: Settings) -> None:
    store.add_fact(settings.db_path(), "server password: hunter2secret")
    session = history.create_session(settings.db_path())
    bundle = context.assemble(settings.db_path(), session, "hi", PROMPT)
    assert bundle.privacy_class is PrivacyClass.LOCAL_ONLY


def test_facts_budget_is_hard(db: sqlite3.Connection, settings: Settings) -> None:
    for index in range(100):
        store.add_fact(settings.db_path(), f"fact number {index}: " + "detail " * 30)
    session = history.create_session(settings.db_path())
    bundle = context.assemble(settings.db_path(), session, "hi", PROMPT)
    facts_blocks = [m.content for m in bundle.messages if m.content.startswith("Things you know")]
    assert len(facts_blocks) == 1
    assert estimate_tokens(facts_blocks[0]) <= context.FACTS_BUDGET + 20  # header slack


def test_retrieved_moments_come_from_other_sessions(
    db: sqlite3.Connection, settings: Settings
) -> None:
    other = history.create_session(settings.db_path())
    history.append_message(settings.db_path(), other, "user", "my anniversary is June 9th")
    current = history.create_session(settings.db_path())
    bundle = context.assemble(settings.db_path(), current, "when is my anniversary?", PROMPT)
    joined = "\n".join(m.content for m in bundle.messages if m.role == "system")
    assert "June 9th" in joined


def test_recent_window_keeps_newest_within_budget(
    db: sqlite3.Connection, settings: Settings
) -> None:
    session = history.create_session(settings.db_path())
    for index in range(200):
        history.append_message(settings.db_path(), session, "user", f"message {index} " + "x" * 200)
    bundle = context.assemble(settings.db_path(), session, "hi", PROMPT)
    conversational = [m for m in bundle.messages if m.role in ("user", "assistant")]
    assert conversational  # some recent messages survive
    assert conversational[-1].content.startswith("message 199")  # newest is kept
    total = sum(estimate_tokens(m.content) for m in conversational)
    assert total <= context.RECENT_BUDGET


def test_empty_state_yields_prompt_plus_user_turn_only(
    db: sqlite3.Connection, settings: Settings
) -> None:
    session = history.create_session(settings.db_path())
    history.append_message(settings.db_path(), session, "user", "first words")
    bundle = context.assemble(settings.db_path(), session, "first words", PROMPT)
    assert bundle.messages[0].content == PROMPT
    assert bundle.messages[-1].content == "first words"
