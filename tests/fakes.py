"""Test doubles for the gateway: a scripted provider client and registries.

``FakeClient`` satisfies the ``StreamingClient`` protocol. Behavior is scripted
per model key:

    ["Hello ", "world"]          -> stream these deltas, then succeed
    ProviderError(...)           -> fail before the first token
    ("fail_after", n, deltas)    -> yield n deltas from the list, then fail
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from myagent.gateway.registry import Registry
from myagent.gateway.types import (
    ChatMessage,
    ModelSpec,
    ProviderError,
    ProviderSpec,
    ToolCall,
)

Script = list[str] | ProviderError | tuple[str, int, list[str]]


class FakeClient:
    """Scripted StreamingClient; records which models were attempted."""

    def __init__(self, scripts: dict[str, Script]) -> None:
        self.scripts = scripts
        self.calls: list[str] = []
        self.last_messages: list[ChatMessage] = []  # transcript as the provider saw it

    def stream(
        self,
        spec: ModelSpec,
        messages: list[ChatMessage],
        usage_out: dict[str, int],
        max_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_calls_out: list[ToolCall] | None = None,
    ) -> AsyncIterator[str]:
        self.calls.append(spec.key)
        self.last_messages = list(messages)
        script = self.scripts[spec.key]

        async def run() -> AsyncIterator[str]:
            if isinstance(script, ProviderError):
                raise script
            if isinstance(script, tuple):
                _, count, deltas = script
                for delta in deltas[:count]:
                    yield delta
                raise ProviderError(spec.provider, "killed mid-stream")
            for delta in script:
                yield delta
            usage_out["total_tokens"] = sum(len(d) for d in script)

        return run()


def make_registry(rpm: int = 100, rpd: int = 1000, tpm: int = 100_000) -> Registry:
    """Three cloud providers (p1..p3) with one model each, ranked a -> b -> c."""
    providers = {
        name: ProviderSpec(
            name=name, base_url=f"https://{name}.example/v1", api_key_ref=f"{name}_key"
        )
        for name in ("p1", "p2", "p3")
    }
    models = {}
    for name in ("p1", "p2", "p3"):
        spec = ModelSpec(provider=name, id="m", rpm=rpm, rpd=rpd, tpm=tpm)
        models[spec.key] = spec
    from myagent.gateway.types import TaskClass

    routing = {TaskClass.CONVERSATION: ["p1/m", "p2/m", "p3/m"]}
    return Registry(providers, models, routing)
