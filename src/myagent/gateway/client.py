"""Provider clients: the only code that talks to LLM providers.

All three launch providers (Groq, Gemini, OpenRouter) expose OpenAI-compatible
chat-completions endpoints, so one ``openai`` AsyncClient per provider covers
them (v3 review, finding F6). Provider wire quirks are handled here and
nowhere else; SDK exceptions never escape - they become ``ProviderError``.

API keys come from the Windows Credential Manager (service "myagent"),
referenced by name from the registry. Keys are read lazily and cached for the
process lifetime; they are never logged.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, cast

import keyring
from openai import APIError, APITimeoutError, AsyncOpenAI, OpenAIError
from openai.types.chat import ChatCompletionMessageParam

from myagent.gateway.registry import Registry
from myagent.gateway.types import ChatMessage, ModelSpec, ProviderError, ToolCall
from myagent.logging import get_logger

log = get_logger(__name__)

KEYRING_SERVICE = "myagent"
REQUEST_TIMEOUT_SECONDS = 60.0


class MissingCredentialError(ProviderError):
    """The registry references a credential that is not in the keyring."""

    def __init__(self, provider: str, api_key_ref: str) -> None:
        super().__init__(
            provider,
            f"no credential '{api_key_ref}' in Windows Credential Manager "
            f"(service '{KEYRING_SERVICE}'); set it with: "
            f"uv run python scripts/doctor.py --set-key {provider}",
        )
        self.api_key_ref = api_key_ref


def get_api_key(provider: str, api_key_ref: str) -> str:
    """Fetch a provider API key from the OS credential store."""
    key = keyring.get_password(KEYRING_SERVICE, api_key_ref)
    if not key:
        raise MissingCredentialError(provider, api_key_ref)
    return key


class ProviderClientPool:
    """Lazily constructed AsyncOpenAI client per provider."""

    def __init__(self, registry: Registry) -> None:
        self._registry = registry
        self._clients: dict[str, AsyncOpenAI] = {}

    def _client(self, provider: str) -> AsyncOpenAI:
        if provider not in self._clients:
            spec = self._registry.provider(provider)
            self._clients[provider] = AsyncOpenAI(
                base_url=spec.base_url,
                api_key=get_api_key(provider, spec.api_key_ref),
                timeout=REQUEST_TIMEOUT_SECONDS,
                max_retries=0,  # retrying is the gateway's job (failover), not the SDK's
            )
        return self._clients[provider]

    def _payload(self, messages: list[ChatMessage]) -> list[ChatCompletionMessageParam]:
        """Convert our transcript to the wire format.

        Roles are dynamic strings and tool-call shapes vary; the SDK's
        TypedDict unions cannot express that, hence the cast (values are
        shape-valid for every provider we support).
        """
        payload: list[dict[str, Any]] = []
        for message in messages:
            entry: dict[str, Any] = {"role": message.role, "content": message.content}
            if message.tool_calls:
                entry["tool_calls"] = [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {"name": call.name, "arguments": call.arguments},
                    }
                    for call in message.tool_calls
                ]
            if message.tool_call_id is not None:
                entry["tool_call_id"] = message.tool_call_id
            payload.append(entry)
        return cast("list[ChatCompletionMessageParam]", payload)

    async def stream(
        self,
        spec: ModelSpec,
        messages: list[ChatMessage],
        usage_out: dict[str, int],
        max_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_calls_out: list[ToolCall] | None = None,
    ) -> AsyncIterator[str]:
        """Stream text deltas for one completion.

        ``usage_out`` receives ``{"total_tokens": n}`` when the provider
        reports usage on its final stream chunk; callers fall back to an
        estimate when it stays empty. ``tool_calls_out``, when provided,
        collects any tool calls the model requested (assembled from deltas).
        """
        client = self._client(spec.provider)
        payload_messages = self._payload(messages)
        # Tool-call deltas arrive fragmented by index; assemble as we go.
        partial: dict[int, dict[str, str]] = {}
        request: dict[str, Any] = {
            "model": spec.id,
            "messages": payload_messages,
            "stream": True,
            "max_tokens": max_tokens,
        }
        if tools:
            request["tools"] = tools
        try:
            try:
                stream = await client.chat.completions.create(
                    **request, stream_options={"include_usage": True}
                )
            except APIError as exc:
                # Quirk: some OpenAI-compatible endpoints reject stream_options.
                if "stream_options" not in str(exc):
                    raise
                stream = await client.chat.completions.create(**request)
            async for chunk in stream:
                if chunk.usage is not None and chunk.usage.total_tokens is not None:
                    usage_out["total_tokens"] = chunk.usage.total_tokens
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                if delta.content:
                    yield delta.content
                for fragment in delta.tool_calls or []:
                    slot = partial.setdefault(
                        fragment.index, {"id": "", "name": "", "arguments": ""}
                    )
                    if fragment.id:
                        slot["id"] = fragment.id
                    if fragment.function is not None:
                        if fragment.function.name:
                            slot["name"] = fragment.function.name
                        if fragment.function.arguments:
                            slot["arguments"] += fragment.function.arguments
        except (APIError, APITimeoutError) as exc:
            raise ProviderError(spec.provider, str(exc)) from exc
        except OpenAIError as exc:
            raise ProviderError(spec.provider, f"unexpected SDK error: {exc}") from exc

        if tool_calls_out is not None:
            for index in sorted(partial):
                slot = partial[index]
                if slot["name"]:
                    tool_calls_out.append(
                        ToolCall(
                            id=slot["id"] or f"call_{index}",
                            name=slot["name"],
                            arguments=slot["arguments"] or "{}",
                        )
                    )
