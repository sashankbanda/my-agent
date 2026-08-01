"""Cross-provider failover with tool history.

Regression coverage for a real outage: mid-task failover returned "every
eligible provider failed" because Gemini rejected Groq's tool-call format
("missing thought_signature") and an OpenRouter model could not render it.
"""

from __future__ import annotations

import sqlite3

from myagent.config import Settings
from myagent.gateway.portability import flatten_tool_history, has_tool_history
from myagent.gateway.types import ChatMessage, InferenceRequest, ProviderError, ToolCall
from tests.test_gateway import build_gateway, collect


def tool_transcript() -> list[ChatMessage]:
    return [
        ChatMessage(role="system", content="be helpful"),
        ChatMessage(role="user", content="what's in my folder?"),
        ChatMessage(
            role="assistant",
            content="Looking.",
            tool_calls=[ToolCall(id="c1", name="files.list_dir", arguments='{"path":"D"}')],
        ),
        ChatMessage(role="tool", content='{"entries":["a.txt"]}', tool_call_id="c1"),
    ]


class TestFlattening:
    def test_detects_tool_history(self) -> None:
        assert has_tool_history(tool_transcript()) is True
        assert has_tool_history([ChatMessage(role="user", content="hi")]) is False

    def test_no_native_tool_fields_survive(self) -> None:
        flat = flatten_tool_history(tool_transcript())
        assert all(m.tool_calls is None for m in flat)
        assert all(m.role != "tool" for m in flat)

    def test_narration_preserves_the_facts(self) -> None:
        flat = flatten_tool_history(tool_transcript())
        joined = "\n".join(m.content for m in flat)
        assert "files.list_dir" in joined  # what it did
        assert "a.txt" in joined  # what it learned

    def test_adjacent_same_roles_are_merged(self) -> None:
        """Two tool results in a row must not become two user messages."""
        transcript = [
            ChatMessage(role="user", content="do it"),
            ChatMessage(
                role="assistant",
                content="",
                tool_calls=[
                    ToolCall(id="a", name="one", arguments="{}"),
                    ToolCall(id="b", name="two", arguments="{}"),
                ],
            ),
            ChatMessage(role="tool", content="first", tool_call_id="a"),
            ChatMessage(role="tool", content="second", tool_call_id="b"),
        ]
        flat = flatten_tool_history(transcript)
        roles = [m.role for m in flat]
        assert roles == ["user", "assistant", "user"]  # merged, alternating
        assert "first" in flat[-1].content and "second" in flat[-1].content

    def test_long_results_are_truncated(self) -> None:
        transcript = [
            ChatMessage(
                role="assistant",
                content="",
                tool_calls=[ToolCall(id="c", name="n", arguments="{}")],
            ),
            ChatMessage(role="tool", content="x" * 50_000, tool_call_id="c"),
        ]
        flat = flatten_tool_history(transcript)
        assert len(flat[-1].content) < 3_000  # free tiers have tight TPM budgets

    def test_plain_conversation_is_untouched(self) -> None:
        plain = [
            ChatMessage(role="system", content="be helpful"),
            ChatMessage(role="user", content="hello"),
        ]
        assert flatten_tool_history(plain) == plain


class TestGatewayFailover:
    async def test_same_provider_keeps_native_tool_history(
        self, db: sqlite3.Connection, settings: Settings
    ) -> None:
        gateway, client = build_gateway(settings, {"p1/m": ["fine"]})
        request = InferenceRequest(messages=tool_transcript(), tool_history_provider="p1")
        await collect(gateway, request)
        sent = client.last_messages  # type: ignore[attr-defined]
        assert any(m.role == "tool" for m in sent), "native protocol should be preserved"

    async def test_failover_to_other_provider_flattens(
        self, db: sqlite3.Connection, settings: Settings
    ) -> None:
        """The outage scenario: p1 rate-limited, p2 must still continue the task."""
        gateway, client = build_gateway(
            settings,
            {"p1/m": ProviderError("p1", "429 rate limit"), "p2/m": ["continued"]},
        )
        request = InferenceRequest(messages=tool_transcript(), tool_history_provider="p1")
        text, _ = await collect(gateway, request)
        assert text == "continued"
        sent = client.last_messages  # type: ignore[attr-defined]
        assert not any(m.role == "tool" for m in sent)
        assert not any(m.tool_calls for m in sent)
        assert "files.list_dir" in "\n".join(m.content for m in sent)
