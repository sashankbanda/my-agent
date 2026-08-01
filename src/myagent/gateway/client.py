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
from typing import cast

import keyring
from openai import APIError, APITimeoutError, AsyncOpenAI, OpenAIError
from openai.types.chat import ChatCompletionMessageParam

from myagent.gateway.registry import Registry
from myagent.gateway.types import ChatMessage, ModelSpec, ProviderError
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

    async def stream(
        self,
        spec: ModelSpec,
        messages: list[ChatMessage],
        usage_out: dict[str, int],
        max_tokens: int | None = None,
    ) -> AsyncIterator[str]:
        """Stream text deltas for one completion.

        ``usage_out`` receives ``{"total_tokens": n}`` when the provider
        reports usage on its final stream chunk; callers fall back to an
        estimate when it stays empty.
        """
        client = self._client(spec.provider)
        # Roles are dynamic strings from our transcript; the SDK's TypedDict
        # unions cannot express that, hence the cast (values are shape-valid).
        payload_messages = cast(
            "list[ChatCompletionMessageParam]",
            [{"role": m.role, "content": m.content} for m in messages],
        )
        try:
            try:
                stream = await client.chat.completions.create(
                    model=spec.id,
                    messages=payload_messages,
                    stream=True,
                    stream_options={"include_usage": True},
                    max_tokens=max_tokens,
                )
            except APIError as exc:
                # Quirk: some OpenAI-compatible endpoints reject stream_options.
                if "stream_options" not in str(exc):
                    raise
                stream = await client.chat.completions.create(
                    model=spec.id,
                    messages=payload_messages,
                    stream=True,
                    max_tokens=max_tokens,
                )
            async for chunk in stream:
                if chunk.usage is not None and chunk.usage.total_tokens is not None:
                    usage_out["total_tokens"] = chunk.usage.total_tokens
                if chunk.choices and chunk.choices[0].delta.content:
                    yield chunk.choices[0].delta.content
        except (APIError, APITimeoutError) as exc:
            raise ProviderError(spec.provider, str(exc)) from exc
        except OpenAIError as exc:
            raise ProviderError(spec.provider, f"unexpected SDK error: {exc}") from exc
