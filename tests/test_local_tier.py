"""Local-model tier: easy turns run on-device, hard ones still go to the cloud.

Covers the routing decision, the escalation safety net, and the privacy win
(a local model means secret-bearing prompts can be answered instead of refused).
"""

from __future__ import annotations

import sqlite3

import pytest

from myagent.config import Settings
from myagent.core import complexity, history
from myagent.core.loop import LEAKED_CALL_REPLY, AgentLoop, build_system_prompt
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
            # Anything that wants a tool called: a 3B model answers these with
            # instructions instead of calling the tool, which is the whole bug
            # this routing exists to prevent.
            ("close spotify", "wants an action taken"),
            ("gpu usage percentage", "asks about this machine"),
            ("my disk space", "asks about this machine"),
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


class TestAnswerStyle:
    """The prompt has to forbid the failure the user actually hit.

    Asked for GPU usage, the assistant replied "I don't have direct access...
    press Ctrl+Shift+Esc, go to the Performance tab" - a tutorial instead of a
    tool call, three paragraphs long.
    """

    def test_base_prompt_forbids_explaining_instead_of_acting(self) -> None:
        prompt = build_system_prompt()
        assert "DO THE THING" in prompt
        assert "BE SHORT" in prompt

    def test_voice_replies_are_capped_harder(self) -> None:
        """A spoken paragraph cannot be skimmed, so it must be shorter."""
        spoken = build_system_prompt(channel="voice")
        assert "SPOKEN ALOUD" in spoken
        assert "30 words" in spoken
        assert "SPOKEN ALOUD" not in build_system_prompt(channel="local")

    def test_local_model_gets_its_own_brevity_cap(self) -> None:
        assert "two short sentences" in build_system_prompt(local_model=True)
        assert "two short sentences" not in build_system_prompt(local_model=False)


class TestDeflectionEscalates:
    """A local answer that tells the user to go do it themselves is a failure.

    The cloud model has the same tools and calls them, so the turn is retried
    there rather than shown.
    """

    @pytest.mark.parametrize(
        "answer",
        [
            "I don't have direct access to real-time GPU data.",
            "I do not have access to that. You would need to check manually.",
            "Open Task Manager and look at the Performance tab.",
            "I don't see any direct tool available for checking GPU usage.",
        ],
    )
    def test_deflections_are_retried_on_the_cloud(self, answer: str) -> None:
        escalate, reason = complexity.should_escalate(answer)
        assert escalate is True
        assert reason == "local model explained instead of acting"

    @pytest.mark.parametrize(
        "answer",
        [
            "It's 92%, plugged in.",
            "Shakespeare wrote Hamlet.",
            "CPU stands for central processing unit.",
        ],
    )
    def test_good_answers_are_kept(self, answer: str) -> None:
        assert complexity.should_escalate(answer)[0] is False


class SequencedClient(FakeClient):
    """Returns a *different* scripted reply per call.

    FakeClient replays one script forever, which cannot express "the model
    answered badly, then answered well after being corrected".
    """

    def __init__(self, replies: list[list[str]]) -> None:
        super().__init__({})
        self._replies = list(replies)

    def stream(self, spec, messages, usage_out, max_tokens=None, tools=None, tool_calls_out=None):  # type: ignore[no-untyped-def]
        deltas = self._replies.pop(0) if self._replies else [""]
        self.scripts = {spec.key: deltas}
        return super().stream(spec, messages, usage_out, max_tokens, tools, tool_calls_out)


def build_sequenced(settings: Settings, replies: list[list[str]]) -> tuple[AgentLoop, FakeClient]:
    """A loop whose model gives each scripted reply in turn."""
    client = SequencedClient(replies)
    gateway = Gateway(
        registry=local_registry(),
        quota=QuotaGovernor(settings.db_path()),
        health=HealthTracker(settings.db_path()),
        client=client,
        db_path=settings.db_path(),
    )
    return AgentLoop(gateway, settings.db_path(), fast_path=False, local_tier=False), client


class TestToolDeflectionGuard:
    """An action request must never be answered with a how-to guide.

    Escalating to the cloud is not enough on its own: when every free tier is
    rate-limited the local model is the only one left, so the correction has
    to work in place.
    """

    def test_action_requests_are_marked_as_needing_a_tool(self) -> None:
        assert complexity.classify("close spotify").needs_tool is True
        assert complexity.classify("how much disk space is left").needs_tool is True
        assert complexity.classify("who wrote Hamlet").needs_tool is False

    @pytest.mark.parametrize(
        "answer",
        [
            "I'm unable to directly check your disk space.",
            "You can use File Explorer to see that.",
            "Right-click the Start button and choose Properties.",
            "Open Task Manager and look at the Performance tab.",
        ],
    )
    def test_deflections_are_detected(self, answer: str) -> None:
        assert complexity.looks_like_deflection(answer) is True

    def test_real_answers_are_not_flagged(self) -> None:
        assert complexity.looks_like_deflection("You have 352.3 GB free of 511 GB.") is False

    @pytest.mark.parametrize(
        "answer",
        [
            'subur "{ "name": "apps.list_processes", "arguments": {"limit": null}} "',
            '{"name": "apps.open", "arguments": {"target": "chrome"}}',
            'Let me check: "function": "apps.system_status"',
        ],
    )
    def test_written_out_tool_calls_are_detected(self, answer: str) -> None:
        """A 3B sometimes writes the call as prose instead of making it."""
        assert complexity.looks_like_tool_leak(answer) is True

    def test_prose_is_not_mistaken_for_a_leak(self) -> None:
        assert complexity.looks_like_tool_leak("Your name is on the account.") is False

    async def test_deflection_is_corrected_in_place(
        self, db: sqlite3.Connection, settings: Settings
    ) -> None:
        """First reply deflects, second one is kept - the user sees only the second."""
        loop, client = build_sequenced(
            settings,
            [
                ["I don't have direct access. Open Task Manager and look."],
                ["You have 352 GB free."],
            ],
        )
        session = loop.ensure_session(None)
        answer = await drain(loop, session, "how much disk space is left")

        assert answer.strip() == "You have 352 GB free."
        assert client.calls == ["cloud/big", "cloud/big"], "the model was asked again"
        types = [row["type"] for row in db.execute("SELECT type FROM events ORDER BY id")]
        assert "ToolNudge" in types

    async def test_leaked_tool_call_is_never_shown(
        self, db: sqlite3.Connection, settings: Settings
    ) -> None:
        """If the correction fails too, say so - do not print raw JSON."""
        leak = '{"name": "apps.list_processes", "arguments": {}}'
        loop, _ = build_sequenced(settings, [[leak], [leak]])
        session = loop.ensure_session(None)
        answer = await drain(loop, session, "what is using the most memory")

        assert answer.strip() == LEAKED_CALL_REPLY
        assert "arguments" not in answer
        stored = history.get_messages(settings.db_path(), session)[-1]
        assert stored["content"] == LEAKED_CALL_REPLY, "garbage must not enter history"

    async def test_plain_answers_are_untouched(
        self, db: sqlite3.Connection, settings: Settings
    ) -> None:
        loop, client = build_sequenced(settings, [["You have 352 GB free."]])
        session = loop.ensure_session(None)
        answer = await drain(loop, session, "how much disk space is left")

        assert answer.strip() == "You have 352 GB free."
        assert client.calls == ["cloud/big"], "no needless second call"

    async def test_a_how_to_guide_with_no_tool_call_is_replaced(
        self, db: sqlite3.Connection, settings: Settings
    ) -> None:
        """Corrected once and still deflecting: say it failed, don't lecture."""
        guide = "You can use File Explorer to check that yourself."
        loop, _ = build_sequenced(settings, [[guide], [guide]])
        session = loop.ensure_session(None)
        answer = await drain(loop, session, "how much disk space is left")

        assert answer.strip() == LEAKED_CALL_REPLY
        assert "File Explorer" not in answer

    async def test_conversation_answers_are_never_touched(
        self, db: sqlite3.Connection, settings: Settings
    ) -> None:
        """The guard applies only to action requests, not to chat."""
        loop, _ = build_sequenced(settings, [["You would need to read the play to find out."]])
        session = loop.ensure_session(None)
        answer = await drain(loop, session, "who wrote Hamlet")

        assert answer.strip() == "You would need to read the play to find out."


class TestLanguageDrift:
    """qwen2.5 is Chinese-origin and switches language unprompted.

    Observed live: an English "Hey Jarvis!" came back as
    "嗨\uff01有什麼可以幫你的嗎\uff1f". One line in the system prompt is not enough for a
    3B, so the drift is detected and corrected.
    """

    def test_drift_to_another_script_is_caught(self) -> None:
        assert complexity.wrong_language("Hey Jarvis!", "嗨\uff01有什麼可以幫你的嗎\uff1f") is True

    def test_english_answers_are_fine(self) -> None:
        assert complexity.wrong_language("what is the time", "It's 8:01 AM.") is False

    def test_a_quoted_foreign_word_is_not_drift(self) -> None:
        answer = "The Japanese word for cat is neko, written 猫."
        assert complexity.wrong_language("what is cat in japanese", answer) is False

    def test_matching_the_users_script_is_correct(self) -> None:
        """If they write Chinese, answering in Chinese is the right thing."""
        assert complexity.wrong_language("你好吗", "我很好\uff0c谢谢\u3002") is False

    def test_an_explicit_request_is_honoured(self) -> None:
        assert complexity.wrong_language("reply in chinese please", "你好") is False
        assert complexity.wrong_language("translate this to hindi", "नमस्ते") is False

    def test_empty_text_is_not_drift(self) -> None:
        assert complexity.wrong_language("hi", "") is False

    async def test_a_drifted_reply_is_retried_not_shown(
        self, db: sqlite3.Connection, settings: Settings
    ) -> None:
        loop, client = build_sequenced(
            settings, [["嗨\uff01有什麼可以幫你的嗎\uff1f"], ["Hi! What do you need?"]]
        )
        session = loop.ensure_session(None)
        answer = await drain(loop, session, "Hey Jarvis!")

        assert answer.strip() == "Hi! What do you need?"
        assert len(client.calls) == 2, "the model was asked again"
        types = [row["type"] for row in db.execute("SELECT type FROM events ORDER BY id")]
        assert "LanguageCorrected" in types

    async def test_the_correction_is_sent_only_once(
        self, db: sqlite3.Connection, settings: Settings
    ) -> None:
        """A model that keeps drifting must not loop forever."""
        loop, client = build_sequenced(settings, [["你好"], ["你好"], ["你好"]])
        session = loop.ensure_session(None)
        await drain(loop, session, "Hey Jarvis!")

        assert len(client.calls) == 2
