"""Context assembler: build each turn's model transcript within token budgets.

Sources, in priority order, each with a hard token cap so no source can
starve the others (v3 risk R17):

1. system prompt (persona)
2. standing facts (all of them, budget permitting - they define the user)
3. retrieved past messages (FTS + recency, other sessions)
4. recent messages of the current session (most recent kept first)

The assembler also reports the strictest privacy class of anything it
included: one ``local_only`` fact makes the whole prompt ``local_only``,
which the gateway then enforces (SEC-13 layering).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from myagent.core import history
from myagent.gateway.types import ChatMessage, PrivacyClass
from myagent.memory import store
from myagent.tokens import estimate_tokens

FACTS_BUDGET = 600
RETRIEVED_BUDGET = 800
RECENT_BUDGET = 2400
RETRIEVED_K = 6


@dataclass
class ContextBundle:
    """Assembled transcript plus the privacy class it must travel under."""

    messages: list[ChatMessage]
    privacy_class: PrivacyClass


def _take_within_budget(items: list[str], budget: int) -> list[str]:
    """Keep items in order until the token budget is spent."""
    kept: list[str] = []
    used = 0
    for item in items:
        cost = estimate_tokens(item)
        if used + cost > budget:
            break
        kept.append(item)
        used += cost
    return kept


def assemble(
    db_path: Path,
    session_id: str,
    user_text: str,
    system_prompt: str,
) -> ContextBundle:
    """Build the model-facing transcript for one turn."""
    privacy = PrivacyClass.CLOUD_OK
    parts: list[ChatMessage] = [ChatMessage(role="system", content=system_prompt)]

    # 1. Standing facts - all of them if the budget allows, newest last.
    facts = store.list_facts(db_path)
    if facts:
        lines = [f"- {fact['content']}" for fact in reversed(facts)]
        kept = _take_within_budget(lines, FACTS_BUDGET)
        if any(fact["privacy_class"] == PrivacyClass.LOCAL_ONLY.value for fact in facts):
            privacy = PrivacyClass.LOCAL_ONLY
        if kept:
            parts.append(
                ChatMessage(
                    role="system",
                    content="Things you know about the user:\n" + "\n".join(kept),
                )
            )

    # 2. Retrieved episodes from other sessions, relevance-ranked.
    hits = store.search_messages(db_path, user_text, k=RETRIEVED_K, exclude_session=session_id)
    if hits:
        lines = [f"- ({hit['ts'][:10]}, {hit['role']}) {hit['content']}" for hit in hits]
        kept = _take_within_budget(lines, RETRIEVED_BUDGET)
        if kept:
            parts.append(
                ChatMessage(
                    role="system",
                    content="Possibly relevant moments from past conversations:\n"
                    + "\n".join(kept),
                )
            )

    # 3. Recent messages of this session - keep the newest, drop the oldest.
    recent = [
        message
        for message in history.get_messages(db_path, session_id)
        if message["role"] in ("user", "assistant")
    ]
    used = 0
    kept_recent: list[ChatMessage] = []
    for message in reversed(recent):
        cost = estimate_tokens(message["content"])
        if used + cost > RECENT_BUDGET:
            break
        kept_recent.append(ChatMessage(role=message["role"], content=message["content"]))
        used += cost
    parts.extend(reversed(kept_recent))

    return ContextBundle(messages=parts, privacy_class=privacy)
