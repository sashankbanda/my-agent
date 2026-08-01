"""Privacy classification: what is allowed to leave the device.

Two classes (v3 review, finding F8): ``cloud_ok`` (default, after the one-time
onboarding disclosure) and ``local_only`` (must never reach a cloud provider).
The secret-pattern scan below is the always-on backstop; explicit flags from
callers (and, from M2 on, per-memory-item classes) take precedence.

Enforcement lives in the gateway, below cognition: a ``local_only`` prompt is
physically never sent to a cloud candidate, regardless of what any prompt or
model "decides".
"""

from __future__ import annotations

import re

from myagent.gateway.types import ChatMessage, ModelSpec, PrivacyClass

# Patterns that indicate credentials or key material. Deliberately
# high-precision: false positives make the assistant refuse cloud work, so
# each pattern targets an unambiguous secret shape.
_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),  # OpenAI-style API keys
    re.compile(r"\bgsk_[A-Za-z0-9]{20,}\b"),  # Groq API keys
    re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"),  # Google API keys
    re.compile(r"\bsk-or-[A-Za-z0-9_-]{20,}\b"),  # OpenRouter API keys
    re.compile(r"\bghp_[A-Za-z0-9]{30,}\b"),  # GitHub personal access tokens
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{30,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),  # AWS access key ids
    re.compile(r"\bxox[abps]-[A-Za-z0-9-]{10,}\b"),  # Slack tokens
    re.compile(r"\beyJ[A-Za-z0-9_-]{20,}\.eyJ[A-Za-z0-9_-]{20,}\b"),  # JWTs
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),  # PEM private keys
    re.compile(r"(?i)\b(password|passwd|pwd)\s*[:=]\s*\S+"),  # password assignments
)


def contains_secret(text: str) -> bool:
    """True if any high-precision secret pattern matches."""
    return any(pattern.search(text) for pattern in _SECRET_PATTERNS)


def classify(messages: list[ChatMessage]) -> PrivacyClass:
    """Classify a prompt: any secret anywhere makes the whole prompt local-only."""
    for message in messages:
        if contains_secret(message.content):
            return PrivacyClass.LOCAL_ONLY
    return PrivacyClass.CLOUD_OK


def filter_candidates(candidates: list[ModelSpec], privacy_class: PrivacyClass) -> list[ModelSpec]:
    """Drop candidates a prompt of this class may not be sent to.

    ``local_only`` prompts keep only local providers. Until a local fallback
    model is installed (M8), that list is empty - the gateway then refuses
    with a clear error instead of leaking the prompt to the cloud.
    """
    if privacy_class is PrivacyClass.LOCAL_ONLY:
        return [spec for spec in candidates if spec.local]
    return candidates
