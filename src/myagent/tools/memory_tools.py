"""Memory as tools: let the assistant remember, recall, and forget.

Until now memory was only reachable from the UI, so the assistant could read
what it already knew but never write. These make it an explicit, permissioned
capability (MemGPT-style self-editing memory), which also lets the local fast
path handle "remember that ..." with no model call at all.
"""

from __future__ import annotations

from typing import Any

from myagent.memory import store
from myagent.security.tiers import Tier
from myagent.tools.registry import ToolContext, ToolError, tool

MAX_HITS = 6


@tool(
    name="memory.remember",
    tier=Tier.REVERSIBLE,
    description=(
        "Store a durable fact or preference about the user so it is available "
        "in future conversations. Use for stable things ('prefers dark mode', "
        "'works at X'), not for passing details."
    ),
    params={"content": {"type": "string", "description": "The fact to remember"}},
    required=["content"],
    summarize=lambda args: f"remember: {args.get('content')}",
)
def remember(context: ToolContext, content: str) -> dict[str, Any]:
    """Persist one standing fact about the user."""
    text = content.strip()
    if not text:
        raise ToolError("nothing to remember")
    item_id = store.add_fact(context.db_path, text)
    return {"id": item_id, "remembered": text}


@tool(
    name="memory.search",
    tier=Tier.READ,
    description=(
        "Search past conversations for something the user mentioned before. "
        "Use when they refer to an earlier discussion."
    ),
    params={"query": {"type": "string", "description": "What to look for"}},
    required=["query"],
)
def search(context: ToolContext, query: str) -> dict[str, Any]:
    """Keyword search over the conversation history."""
    hits = store.search_messages(context.db_path, query, k=MAX_HITS)
    # Recalled conversation text is still content the assistant did not
    # author, so it counts as untrusted for permission purposes.
    if hits:
        context.turn.taint("recalled conversation")
    return {
        "count": len(hits),
        "results": [
            {"when": hit["ts"][:10], "role": hit["role"], "text": hit["content"]} for hit in hits
        ],
    }


@tool(
    name="memory.forget",
    tier=Tier.CONFIRM_ALWAYS,
    description="Permanently delete one remembered fact by its id.",
    params={"id": {"type": "integer", "description": "Memory item id to delete"}},
    required=["id"],
    summarize=lambda args: f"permanently forget memory #{args.get('id')}",
)
def forget(context: ToolContext, id: int) -> dict[str, Any]:  # `id` is the model-facing name
    """Delete one fact; the right to forget is absolute."""
    if not store.forget(context.db_path, id):
        raise ToolError(f"no memory item with id {id}")
    return {"id": id, "forgotten": True}


@tool(
    name="memory.list_facts",
    tier=Tier.READ,
    description="List everything currently remembered about the user.",
)
def list_facts(context: ToolContext) -> dict[str, Any]:
    """All standing facts with their ids (so they can be forgotten)."""
    facts = store.list_facts(context.db_path)
    return {
        "count": len(facts),
        "facts": [{"id": fact["id"], "content": fact["content"]} for fact in facts],
    }
