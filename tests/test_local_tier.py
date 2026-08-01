"""Local-model tier: easy turns run on-device, hard ones still go to the cloud.

Covers the routing decision, the escalation safety net, and the privacy win
(a local model means secret-bearing prompts can be answered instead of refused).
"""

from __future__ import annotations

import sqlite3

import pytest

from myagent.config import Settings
from myagent.core import complexity
from myagent.core.loop import AgentLoop
from myagent.gateway.gateway import Gateway
from myagent.gateway.health import HealthTracker
from myagent.gateway.privacy import filter_candidates
from myagent.gateway.quota import QuotaGovernor
from myagent.gateway.registry import Registry, default_registry_path, load_registry
from myagent.gateway.types import (
    ChatMessage,
    InferenceRequest,
    ModelSpec,
    PrivacyClass,
    ProviderSpec,
    TaskClass,
)
from tests.fakes import FakeClient, Script


class TestComplexityRouting:
    @pytest.mark.parametrize(
        "text",
        [
            "how are you",
            "what's the capital of France",
            "tell me a joke",
            "who wrote Hamlet",
            "is it going to be cold tomorrow",
            "what does CPU stand for",
            "thanks, that helped",
            "say that again",
        ],
    )
    def test_easy_turns_go_local(self, text: str) -> None:
        assert complexity.classify(text).use_local is True

    @pytest.mark.parametrize(
        ("text", "expected_reason"),
        [
            ("why is my laptop slow", "needs reasoning"),
            ("explain how DNS works", "needs reasoning"),
            ("compare Rust and Go for a CLI", "needs reasoning"),
            ("write a python function to parse dates", "needs reasoning"),
            ("debug this error: KeyError foo", "needs reasoning"),
            ("what is 3847 * 291", "maths"),
            ("plan my week around three deadlines", "needs reasoning"),
            ("def add(a, b): return a + b -- is this right?", "code"),
            ("open chrome and then check my email if it is after 9", "multi-step request"),
        ],
    )
    def test_hard_turns_go_to_the_cloud(self, text: str, expected_reason: str) -> None:
        routing = complexity.classify(text)
        assert routing.use_local is False
        assert routing.reason == expected_reason

    def test_long_input_goes_to_the_cloud(self) -> None:
        assert complexity.classify("tell me about " + "x" * 200).use_local is False

    def test_tool_sequences_stay_with_the_strong_model(self) -> None:
        """Small models are weakest at tool calling; never hand one a chain."""
        routing = complexity.classify("and the second one?", has_tool_history=True)
        assert routing.use_local is False
        assert "tool" in routing.reason

    def test_deep_conversations_go_to_the_cloud(self) -> None:
        assert complexity.classify("and then?", history_depth=40).use_local is False


class TestEscalation:
    @pytest.mark.parametrize(
        "answer",
        [
            "",
            "   ",
            "I'm not sure about that.",
            "I don't know.",
            "As an AI, I cannot help with that.",
        ],
    )
    def test_unusable_answers_escalate(self, answer: str) -> None:
        escalate, _reason = complexity.should_escalate(answer)
        assert escalate is True

    def test_degenerate_repetition_escalates(self) -> None:
        escalate, reason = complexity.should_escalate("the the the " * 20)
        assert escalate is True
        assert "degenerate" in reason

    def test_good_answers_are_kept(self) -> None:
        escalate, _ = complexity.should_escalate("The capital of France is Paris.")
        assert escalate is False


class TestRegistryWiring:
    def test_checked_in_registry_has_a_local_provider(self) -> None:
        registry = load_registry(default_registry_path())
        local_models = [model for model in registry.all_models if model.local]
        assert local_models, "no local model configured"
        assert all(model.trains_on_data is False for model in local_models)

    def test_simple_task_prefers_the_local_model(self) -> None:
        registry = load_registry(default_registry_path())
        first = registry.candidates(TaskClass.SIMPLE)[0]
        assert first.local is True

    def test_conversation_still_prefers_the_cloud(self) -> None:
        registry = load_registry(default_registry_path())
        first = registry.candidates(TaskClass.CONVERSATION)[0]
        assert first.local is False

    def test_conversation_can_fall_back_to_local_when_offline(self) -> None:
        registry = load_registry(default_registry_path())
        assert any(model.local for model in registry.candidates(TaskClass.CONVERSATION))

    def test_secret_prompts_can_now_be_served_locally(self) -> None:
        """The privacy win: local_only used to mean 'refuse'."""
        registry = load_registry(default_registry_path())
        candidates = registry.candidates(TaskClass.CONVERSATION)
        allowed = filter_candidates(candidates, PrivacyClass.LOCAL_ONLY)
        assert allowed, "a secret-bearing prompt has nowhere to run"
        assert all(model.local for model in allowed)


def local_registry() -> Registry:
    """Two providers: a local one ranked first for SIMPLE, cloud for the rest."""
    providers = {
        "ollama": ProviderSpec(name="ollama", base_url="http://x/v1", api_key_ref="k", local=True),
        "cloud": ProviderSpec(name="cloud", base_url="https://y/v1", api_key_ref="k"),
    }
    models = {
        "ollama/small": ModelSpec(provider="ollama", id="small", local=True),
        "cloud/big": ModelSpec(provider="cloud", id="big"),
    }
    routing = {
        TaskClass.SIMPLE: ["ollama/small", "cloud/big"],
        TaskClass.CONVERSATION: ["cloud/big", "ollama/small"],
    }
    return Registry(providers, models, routing)


def build(
    settings: Settings, scripts: dict[str, Script], local_tier: bool = True
) -> tuple[AgentLoop, FakeClient]:
    client = FakeClient(scripts)
    gateway = Gateway(
        registry=local_registry(),
        quota=QuotaGovernor(settings.db_path()),
        health=HealthTracker(settings.db_path()),
        client=client,
        db_path=settings.db_path(),
    )
    return AgentLoop(gateway, settings.db_path(), fast_path=False, local_tier=local_tier), client


async def drain(loop: AgentLoop, session: str, text: str) -> str:
    answer = ""
    async for chunk in loop.respond(session, text):
        if chunk.reset:
            answer = ""
        answer += chunk.delta
    return answer


async def test_easy_question_runs_on_the_local_model(
    db: sqlite3.Connection, settings: Settings
) -> None:
    loop, client = build(
        settings, {"ollama/small": ["Paris."], "cloud/big": ["should not be used"]}
    )
    session = loop.ensure_session(None)
    answer = await drain(loop, session, "what's the capital of France")
    assert client.calls == ["ollama/small"], "an easy question must not hit the cloud"
    assert answer.strip() == "Paris."


async def test_hard_question_runs_on_the_cloud(db: sqlite3.Connection, settings: Settings) -> None:
    loop, client = build(settings, {"cloud/big": ["Because of X."]})
    session = loop.ensure_session(None)
    await drain(loop, session, "why is my laptop slow")
    assert client.calls == ["cloud/big"]


async def test_weak_local_answer_escalates_to_the_cloud(
    db: sqlite3.Connection, settings: Settings
) -> None:
    """The user must never be shown the small model's 'I don't know'."""
    loop, client = build(
        settings,
        {"ollama/small": ["I'm not sure."], "cloud/big": ["Here is the real answer."]},
    )
    session = loop.ensure_session(None)
    answer = await drain(loop, session, "who wrote Hamlet")
    assert client.calls == ["ollama/small", "cloud/big"]
    assert answer.strip() == "Here is the real answer."  # the reset discarded the weak one
    types = [row["type"] for row in db.execute("SELECT type FROM events")]
    assert "EscalatedToCloud" in types


async def test_local_tier_can_be_disabled(db: sqlite3.Connection, settings: Settings) -> None:
    loop, client = build(settings, {"cloud/big": ["cloud answer"]}, local_tier=False)
    session = loop.ensure_session(None)
    await drain(loop, session, "what's the capital of France")
    assert client.calls == ["cloud/big"]


async def test_local_model_serves_a_secret_prompt(
    db: sqlite3.Connection, settings: Settings
) -> None:
    """Secrets are answered on-device rather than refused outright."""
    loop, client = build(settings, {"ollama/small": ["Understood."]})
    gateway = loop._gateway  # reaching in: this asserts routing, not behaviour
    request = InferenceRequest(
        messages=[ChatMessage(role="user", content="my api key is sk-abcdefghij1234567890")],
        task_class=TaskClass.CONVERSATION,
    )
    received = ""
    async for chunk in gateway.complete(request):
        received += chunk.delta
    assert client.calls == ["ollama/small"], "a secret must only reach the local model"
    assert received.strip() == "Understood."
