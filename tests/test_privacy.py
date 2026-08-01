"""Privacy classification tests: the secret-pattern backstop and filtering."""

from __future__ import annotations

import pytest

from myagent.gateway.privacy import classify, contains_secret, filter_candidates
from myagent.gateway.types import ChatMessage, ModelSpec, PrivacyClass


@pytest.mark.parametrize(
    "text",
    [
        "my key is sk-abcdefghijKLMNOPQRST1234",
        "gsk_ABCDEFGHIJKLMNOPQRST12345",
        "AIzaSyA1234567890abcdefghijklmnopqrstu",
        "token ghp_abcdefghijklmnopqrstuvwxyz123456",
        "github_pat_11ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890",
        "aws AKIAIOSFODNN7EXAMPLE here",
        "slack xoxb-123456789012-abcdefghijkl",
        "-----BEGIN RSA PRIVATE KEY-----",
        "password: hunter2secret",
        "PASSWD=correct-horse-battery",
        "bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0",
    ],
)
def test_secret_patterns_detected(text: str) -> None:
    assert contains_secret(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "what's the weather like tomorrow?",
        "my password manager is great",  # mentions the word, assigns nothing
        "the sky is blue and skies are clear",
        "I walked 12345 steps today",
    ],
)
def test_ordinary_text_not_flagged(text: str) -> None:
    assert contains_secret(text) is False


def test_classify_any_secret_message_makes_prompt_local_only() -> None:
    messages = [
        ChatMessage(role="system", content="be helpful"),
        ChatMessage(role="user", content="here: password = swordfish99"),
    ]
    assert classify(messages) is PrivacyClass.LOCAL_ONLY


def test_classify_clean_prompt_is_cloud_ok() -> None:
    messages = [ChatMessage(role="user", content="plan my day")]
    assert classify(messages) is PrivacyClass.CLOUD_OK


def test_filter_local_only_excludes_cloud_models() -> None:
    cloud = ModelSpec(provider="p1", id="m")
    local = ModelSpec(provider="ollama", id="m", local=True)
    kept = filter_candidates([cloud, local], PrivacyClass.LOCAL_ONLY)
    assert kept == [local]


def test_filter_cloud_ok_keeps_everything() -> None:
    cloud = ModelSpec(provider="p1", id="m")
    local = ModelSpec(provider="ollama", id="m", local=True)
    assert filter_candidates([cloud, local], PrivacyClass.CLOUD_OK) == [cloud, local]
