"""Making a tool-using transcript portable between providers.

Native tool-call messages are provider-specific in practice, even though they
share OpenAI's shape. Observed on the launch portfolio (2026-08):

- **Gemini** rejects an assistant tool-call it did not itself produce:
  "Function call is missing a thought_signature in functionCall parts".
- **OpenRouter free models** can fail to render them at all:
  "Failed to apply prompt template: cannot convert value into pairs".

So mid-task failover - exactly when the assistant is most useful and most
likely to hit a rate limit - would break with a 400 from every candidate.

The fix: when handing a transcript to a *different* provider than the one
that generated its tool calls, flatten those exchanges into plain narration
("I used X ... Result: ..."). Every model understands prose, so any provider
can pick the task up mid-flight. The native protocol is still used on the
happy path, where it works and reads better to the model.
"""

from __future__ import annotations

from myagent.gateway.types import ChatMessage

MAX_RESULT_CHARS = 2_000  # keep flattened observations small: free tiers have tight TPM


def _describe_calls(message: ChatMessage) -> str:
    """Narrate an assistant message's tool calls as text."""
    parts = [f"{call.name}({call.arguments})" for call in message.tool_calls or []]
    joined = "; ".join(parts)
    prefix = f"{message.content.strip()} " if message.content.strip() else ""
    return f"{prefix}[I used: {joined}]"


def flatten_tool_history(messages: list[ChatMessage]) -> list[ChatMessage]:
    """Rewrite tool-call/tool-result pairs as plain assistant/user narration.

    Assistant tool calls become assistant prose; tool results become user
    messages labelled as observations, which is the classic ReAct shape and
    universally supported. Ordinary messages pass through untouched.
    """
    flattened: list[ChatMessage] = []
    for message in messages:
        if message.role == "assistant" and message.tool_calls:
            flattened.append(ChatMessage(role="assistant", content=_describe_calls(message)))
        elif message.role == "tool":
            body = message.content[:MAX_RESULT_CHARS]
            flattened.append(
                ChatMessage(role="user", content=f"[Result of your last action] {body}")
            )
        else:
            flattened.append(message)
    return _merge_adjacent(flattened)


def _merge_adjacent(messages: list[ChatMessage]) -> list[ChatMessage]:
    """Merge consecutive same-role messages.

    Flattening can produce user-after-user (several tool results in a row),
    which some providers reject; merging keeps the alternation valid.
    """
    merged: list[ChatMessage] = []
    for message in messages:
        if merged and merged[-1].role == message.role and message.role in ("user", "assistant"):
            merged[-1] = ChatMessage(
                role=message.role, content=f"{merged[-1].content}\n{message.content}"
            )
        else:
            merged.append(message)
    return merged


def has_tool_history(messages: list[ChatMessage]) -> bool:
    """True if the transcript contains provider-specific tool exchanges."""
    return any(message.role == "tool" or message.tool_calls for message in messages)
