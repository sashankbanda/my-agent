"""The Gateway: ranked-cascade routing with preemptive quota and failover.

``Gateway.complete`` is the single entry point for all LLM inference in the
kernel. For each request it:

1. classifies privacy (unless the caller already did),
2. resolves the ranked candidate list for the task class,
3. drops privacy-excluded candidates,
4. walks the list, skipping cooling-down providers and empty quota buckets
   (preemptive - an exhausted model is never even attempted),
5. streams from the first candidate that works; on failure it records health,
   emits a ``ProviderDegraded`` event, and moves to the next candidate.

Mid-stream failover: if a provider dies after emitting text, the gateway
yields a ``reset=True`` chunk - consumers discard accumulated text and the
answer restarts on the next candidate.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, AsyncIterator
from pathlib import Path
from typing import Any, Protocol

from myagent.db import connection
from myagent.events import EventType, append_event
from myagent.gateway.health import HealthTracker
from myagent.gateway.portability import flatten_tool_history, has_tool_history
from myagent.gateway.privacy import classify, filter_candidates
from myagent.gateway.quota import QuotaGovernor
from myagent.gateway.registry import Registry
from myagent.gateway.types import (
    AllProvidersExhaustedError,
    ChatMessage,
    InferenceChunk,
    InferenceRequest,
    ModelSpec,
    NoEligibleModelError,
    ProviderError,
    ToolCall,
)
from myagent.logging import get_logger
from myagent.tokens import estimate_tokens

log = get_logger(__name__)


class StreamingClient(Protocol):
    """What the gateway needs from a provider client (tests substitute fakes)."""

    def stream(
        self,
        spec: ModelSpec,
        messages: list[ChatMessage],
        usage_out: dict[str, int],
        max_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_calls_out: list[ToolCall] | None = None,
    ) -> AsyncIterator[str]: ...


class Gateway:
    """Sole LLM egress point (see architecture invariant #1)."""

    def __init__(
        self,
        registry: Registry,
        quota: QuotaGovernor,
        health: HealthTracker,
        client: StreamingClient,
        db_path: Path,
    ) -> None:
        self._registry = registry
        self._quota = quota
        self._health = health
        self._client = client
        self._db_path = db_path

    async def complete(self, request: InferenceRequest) -> AsyncGenerator[InferenceChunk]:
        """Stream one completion, failing over across candidates as needed.

        Returns an async *generator*: callers that stop consuming early must
        ``aclose()`` it (the loop does) so provider connections are released.
        """
        privacy_class = request.privacy_class or classify(request.messages)
        ranked = self._registry.candidates(request.task_class)
        candidates = filter_candidates(ranked, privacy_class)
        if not candidates:
            raise NoEligibleModelError(
                f"no model may serve a {privacy_class} prompt for task "
                f"'{request.task_class}' (a local fallback model is not installed)"
            )

        attempted = 0
        last_error: ProviderError | None = None
        for spec in candidates:
            if not self._health.is_available(spec.provider):
                log.debug("candidate_cooling_down", model=spec.key)
                continue
            if not self._quota.can_use(spec, interactive=request.interactive):
                log.debug("candidate_quota_exhausted", model=spec.key)
                continue

            # A transcript carrying another provider's tool calls is not
            # portable (Gemini and some OpenRouter models reject it outright),
            # so narrate those exchanges instead when switching providers.
            messages = request.messages
            if (
                request.tool_history_provider
                and request.tool_history_provider != spec.provider
                and has_tool_history(messages)
            ):
                messages = flatten_tool_history(messages)
                log.info("flattened_tool_history", to=spec.provider)

            attempted += 1
            self._quota.record_request(spec)
            self._emit(
                EventType.INFERENCE_ROUTED,
                {"model": spec.key, "task": request.task_class.value},
                request.trace_id,
            )
            usage: dict[str, int] = {}
            emitted_text = ""
            tool_calls: list[ToolCall] = []
            try:
                async for delta in self._client.stream(
                    spec,
                    messages,
                    usage,
                    request.max_tokens,
                    request.tools,
                    tool_calls,
                ):
                    emitted_text += delta
                    yield InferenceChunk(delta=delta, model_key=spec.key)
            except ProviderError as exc:
                last_error = exc
                self._health.record_failure(spec.provider)
                self._emit(
                    EventType.PROVIDER_DEGRADED,
                    {"provider": spec.provider, "model": spec.key, "error": str(exc)},
                    request.trace_id,
                )
                log.warning("provider_failed", model=spec.key, error=str(exc))
                if emitted_text:
                    yield InferenceChunk(reset=True, model_key=spec.key)
                continue

            self._health.record_success(spec.provider)
            tokens = usage.get("total_tokens", estimate_tokens(emitted_text))
            self._quota.record_tokens(spec, tokens)
            yield InferenceChunk(
                done=True,
                model_key=spec.key,
                tokens=tokens,
                tool_calls=tool_calls or None,
            )
            return

        if attempted == 0:
            self._emit(
                EventType.QUOTA_EXHAUSTED,
                {"task": request.task_class.value, "candidates": [c.key for c in candidates]},
                request.trace_id,
            )
            raise AllProvidersExhaustedError(
                f"all candidates for task '{request.task_class}' are quota-exhausted "
                "or cooling down; the request was not sent anywhere"
            )
        raise AllProvidersExhaustedError(
            f"every eligible provider failed for task '{request.task_class}'"
        ) from last_error

    def _emit(self, type_: EventType, data: dict[str, object], trace_id: str | None) -> None:
        with connection(self._db_path) as conn:
            append_event(conn, type_, data, trace_id)
