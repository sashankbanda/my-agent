"""Gateway contract types.

These types are the boundary between the rest of the kernel and the gateway:
callers build an ``InferenceRequest`` and consume ``InferenceChunk`` items.
Provider SDK types never cross this boundary.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class TaskClass(StrEnum):
    """What kind of work an inference request is; drives routing."""

    TRIAGE = "triage"
    CONVERSATION = "conversation"
    PLANNING = "planning"
    LONG_CONTEXT = "long_context"
    VISION = "vision"
    BACKGROUND = "background"


class PrivacyClass(StrEnum):
    """Where a prompt is allowed to travel.

    CLOUD_OK: may be sent to any configured provider (user accepted the
    free-tier disclosure at onboarding).
    LOCAL_ONLY: must never leave the device - secrets or user-marked content.
    """

    CLOUD_OK = "cloud_ok"
    LOCAL_ONLY = "local_only"


class ChatMessage(BaseModel):
    """One turn in the model-facing conversation transcript."""

    role: str
    content: str


class InferenceRequest(BaseModel):
    """A request for one streamed completion."""

    messages: list[ChatMessage]
    task_class: TaskClass = TaskClass.CONVERSATION
    privacy_class: PrivacyClass | None = None  # None -> classified by the gateway
    interactive: bool = True  # background work respects the quota headroom reserve
    max_tokens: int | None = None
    trace_id: str | None = None


class InferenceChunk(BaseModel):
    """One streamed unit of a completion.

    ``reset=True`` means a provider failed mid-stream and the gateway is
    restarting the answer on the next candidate: the consumer must discard
    all text accumulated so far for this request.
    """

    delta: str = ""
    model_key: str
    reset: bool = False
    done: bool = False
    tokens: int | None = None  # populated on the done chunk when known


class GatewayError(Exception):
    """Base class for gateway failures."""


class ProviderError(GatewayError):
    """A single provider call failed (network, 4xx/5xx, timeout)."""

    def __init__(self, provider: str, message: str) -> None:
        super().__init__(f"{provider}: {message}")
        self.provider = provider


class NoEligibleModelError(GatewayError):
    """No candidate model satisfies the routing + privacy constraints."""


class AllProvidersExhaustedError(GatewayError):
    """Every eligible candidate was quota-empty, cooling down, or failed."""


class ModelSpec(BaseModel):
    """One model entry from the registry (config data)."""

    provider: str
    id: str
    speed: str = "medium"
    context: int = 8192
    supports_tools: bool = False
    supports_vision: bool = False
    trains_on_data: bool = True
    local: bool = False  # local providers (M8) are eligible for LOCAL_ONLY prompts
    rpm: int = Field(default=10, ge=1)
    rpd: int = Field(default=100, ge=1)
    tpm: int = Field(default=10_000, ge=1)

    @property
    def key(self) -> str:
        """Stable identifier used in routing tables and quota buckets."""
        return f"{self.provider}/{self.id}"


class ProviderSpec(BaseModel):
    """One provider entry from the registry (config data)."""

    name: str
    base_url: str
    api_key_ref: str
    local: bool = False
