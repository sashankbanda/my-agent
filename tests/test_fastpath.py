"""Fast-path tests: simple commands must cost zero tokens - and stay safe.

Two properties matter most and are both asserted end to end:
1. a matched command makes NO provider call at all
2. it still goes through the permission broker (no security bypass)
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import ClassVar

import pytest

from myagent.config import Settings, ToolSettings
from myagent.core import fastpath, history
from myagent.core.loop import FAST_PATH_MODEL, AgentLoop
from myagent.gateway.gateway import Gateway
from myagent.gateway.health import HealthTracker
from myagent.gateway.quota import QuotaGovernor
from myagent.security.broker import PermissionBroker
from myagent.security.confirm import ConfirmationService
from myagent.tools.executor import ToolExecutor
from myagent.tools.registry import load_builtin_tools
from tests.fakes import FakeClient, Script, make_registry

load_builtin_tools()


class TestMatching:
    @pytest.mark.parametrize(
        ("text", "expected_intent", "expected_tool"),
        [
            ("hi", "greeting", None),
            ("Hello", "greeting", None),
            ("thanks", "ack", None),
            ("what time is it", "time", None),
            ("what's the date", "date", None),
            ("open chrome", "open", "apps.open"),
            ("Open Premiere Pro", "open", "apps.open"),
            ("launch my downloads folder", "open", "apps.open"),
            ("open youtube.com", "open_url", "apps.open_url"),
            ("open https://github.com", "open_url", "apps.open_url"),
            ("what's in my downloads", "list_dir", "files.list_dir"),
            ("list Documents", "list_dir", "files.list_dir"),
            ("what's my battery", "status", "apps.system_status"),
            ("check cpu usage", "status", "apps.system_status"),
            ("how's my disk space", "status", "apps.system_status"),
            ("what's running", "processes", "apps.list_processes"),
            ("what apps can you open", "apps", "apps.list_applications"),
            ("remember that I prefer dark mode", "remember", "memory.remember"),
            ("what do you remember about me", "list_facts", "memory.list_facts"),
        ],
    )
    def test_recognized(self, text: str, expected_intent: str, expected_tool: str | None) -> None:
        intent = fastpath.match(text)
        assert intent is not None, f"{text!r} should be handled locally"
        assert intent.name == expected_intent
        assert intent.tool == expected_tool

    @pytest.mark.parametrize(
        "text",
        [
            "open chrome and search for python tutorials",  # two actions
            "why is my battery draining so fast",  # needs reasoning
            "what's in my downloads, and delete the old ones",  # conjunction
            "summarize the files in Documents",  # needs a model
            "explain what cpu usage means",  # explanation
            "can you open a file if it exists",  # conditional
            "write a poem about my battery",  # creative
            "compare my disk usage to last week",  # comparison
            "delete everything in downloads",  # destructive: model + confirmation
            "move my invoices into a folder by year",  # multi-step judgement
            "",  # nothing
            "   ",
        ],
    )
    def test_deferred_to_the_model(self, text: str) -> None:
        assert fastpath.match(text) is None, f"{text!r} must not be fast-pathed"

    def test_long_input_defers(self) -> None:
        assert fastpath.match("open " + "x" * 200) is None

    def test_url_detection(self) -> None:
        assert fastpath._looks_like_url("youtube.com") is True
        assert fastpath._looks_like_url("https://a.b/c") is True
        assert fastpath._looks_like_url("chrome") is False
        assert fastpath._looks_like_url("Premiere Pro") is False


class TestFormatting:
    def test_status_reply_uses_real_numbers(self) -> None:
        intent = fastpath.Intent(name="status", formatter="status")
        reply = fastpath.format_reply(
            intent,
            {
                "cpu_percent": 12.5,
                "memory": {"total_gb": 16.0, "used_percent": 61.0},
                "disk": {"total_gb": 476.0, "free_gb": 92.3, "used_percent": 80.6},
                "battery": {"percent": 92, "plugged_in": False},
            },
        )
        assert "12.5%" in reply
        assert "92.3 GB free" in reply
        assert "92% (on battery)" in reply

    def test_list_dir_reply_counts_and_names(self) -> None:
        intent = fastpath.Intent(name="list_dir", formatter="list_dir")
        reply = fastpath.format_reply(
            intent,
            {
                "path": "C:/Users/x/Downloads",
                "entries": [
                    {"name": "a.pdf", "kind": "file"},
                    {"name": "b.zip", "kind": "file"},
                    {"name": "old", "kind": "dir"},
                ],
            },
        )
        assert "1 folder" in reply and "2 files" in reply
        assert "a.pdf" in reply

    def test_empty_folder_reply(self) -> None:
        intent = fastpath.Intent(name="list_dir", formatter="list_dir")
        reply = fastpath.format_reply(intent, {"path": "D:/empty", "entries": []})
        assert "empty" in reply

    def test_fact_count_is_grammatical(self) -> None:
        intent = fastpath.Intent(name="list_facts", formatter="facts")
        one = fastpath.format_reply(intent, {"count": 1, "facts": [{"content": "likes tea"}]})
        many = fastpath.format_reply(
            intent, {"count": 2, "facts": [{"content": "a"}, {"content": "b"}]}
        )
        assert "1 thing I remember" in one
        assert "2 things I remember" in many


# -- end to end ---------------------------------------------------------------


@pytest.fixture
def sandbox(tmp_path: Path) -> Path:
    root = tmp_path / "root"
    (root / "Downloads").mkdir(parents=True)
    (root / "Downloads" / "invoice.pdf").write_bytes(b"%PDF")
    return root


def build(
    settings: Settings, sandbox: Path, fast_path: bool = True
) -> tuple[AgentLoop, FakeClient]:
    scoped = settings.model_copy(update={"tools": ToolSettings(roots=[str(sandbox)])})
    scripts: dict[str, Script] = {"p1/m": ["the model was called"]}
    client = FakeClient(scripts)
    gateway = Gateway(
        registry=make_registry(),
        quota=QuotaGovernor(scoped.db_path()),
        health=HealthTracker(scoped.db_path()),
        client=client,
        db_path=scoped.db_path(),
    )
    broker = PermissionBroker(scoped.db_path())
    confirmations = ConfirmationService()
    executor = ToolExecutor(scoped.db_path(), scoped, broker, confirmations)
    loop = AgentLoop(gateway, scoped.db_path(), executor=executor, fast_path=fast_path)
    return loop, client


async def run(loop: AgentLoop, session: str, text: str) -> str:
    reply = ""
    async for chunk in loop.respond(session, text):
        reply += chunk.delta
    return reply


async def test_greeting_costs_no_provider_call(
    db: sqlite3.Connection, settings: Settings, sandbox: Path
) -> None:
    loop, client = build(settings, sandbox)
    session = loop.ensure_session(None)
    reply = await run(loop, session, "hi")
    assert client.calls == [], "a greeting must not reach a provider"
    assert reply.strip() != ""
    stored = history.get_messages(settings.db_path(), session)[-1]
    assert stored["model"] == FAST_PATH_MODEL  # visibly free in history


async def test_status_command_runs_the_tool_without_the_model(
    db: sqlite3.Connection, settings: Settings, sandbox: Path
) -> None:
    loop, client = build(settings, sandbox)
    session = loop.ensure_session(None)
    reply = await run(loop, session, "what's my battery")
    assert client.calls == []
    # Answers the question that was asked - a battery reading, not a dump of
    # every hardware statistic the tool happens to return.
    assert "%" in reply
    assert "CPU" not in reply
    assert "disk" not in reply
    types = [row["type"] for row in db.execute("SELECT type FROM events ORDER BY id")]
    assert "FastPathHandled" in types
    assert "ToolCallCompleted" in types  # the tool really ran
    assert "InferenceRouted" not in types  # ...and no inference happened


async def test_fast_path_still_passes_through_the_broker(
    db: sqlite3.Connection, settings: Settings, sandbox: Path
) -> None:
    """No security bypass: the broker decides, exactly as for model calls."""
    loop, _client = build(settings, sandbox)
    session = loop.ensure_session(None)
    await run(loop, session, "what's in my Downloads")
    decisions = [
        row["type"] for row in db.execute("SELECT type FROM events WHERE type='PermissionDecided'")
    ]
    assert decisions, "the broker must have been consulted"


async def test_kill_switch_blocks_fast_path_too(
    db: sqlite3.Connection, settings: Settings, sandbox: Path
) -> None:
    loop, client = build(settings, sandbox)
    session = loop.ensure_session(None)
    scoped = settings.model_copy(update={"tools": ToolSettings(roots=[str(sandbox)])})
    PermissionBroker(scoped.db_path()).kill_switch.engage()
    try:
        await run(loop, session, "what's my battery")
    finally:
        PermissionBroker(scoped.db_path()).kill_switch.release()
    # Denied tool -> falls back to the model rather than claiming success.
    assert client.calls, "a blocked fast path should fall back to the model"


async def test_tool_failure_falls_back_to_the_model(
    db: sqlite3.Connection, settings: Settings, sandbox: Path
) -> None:
    """A mechanical miss should become a smart retry, not a dead end."""
    loop, client = build(settings, sandbox)
    session = loop.ensure_session(None)
    reply = await run(loop, session, "open totally-not-installed-xyz")
    assert client.calls == ["p1/m"], "the model should have been asked to recover"
    assert "the model was called" in reply


async def test_complex_request_uses_the_model(
    db: sqlite3.Connection, settings: Settings, sandbox: Path
) -> None:
    loop, client = build(settings, sandbox)
    session = loop.ensure_session(None)
    await run(loop, session, "look at my downloads and tell me what to delete")
    assert client.calls == ["p1/m"]


async def test_fast_path_can_be_disabled(
    db: sqlite3.Connection, settings: Settings, sandbox: Path
) -> None:
    loop, client = build(settings, sandbox, fast_path=False)
    session = loop.ensure_session(None)
    await run(loop, session, "hi")
    assert client.calls == ["p1/m"]


async def test_remember_stores_a_fact_locally(
    db: sqlite3.Connection, settings: Settings, sandbox: Path
) -> None:
    from myagent.memory import store

    loop, client = build(settings, sandbox)
    session = loop.ensure_session(None)
    reply = await run(loop, session, "remember that I prefer dark roast coffee")
    assert client.calls == []
    facts = [fact["content"] for fact in store.list_facts(settings.db_path())]
    assert any("dark roast" in fact for fact in facts)
    assert "dark roast" in reply


def test_confirmation_required_tools_are_not_fast_pathed() -> None:
    """Nothing destructive is reachable without the model and a prompt."""
    dangerous = {"files.delete", "shell.run", "apps.close", "memory.forget"}
    for phrase in ("delete my downloads", "run npm install", "close chrome", "forget everything"):
        intent = fastpath.match(phrase)
        assert intent is None or intent.tool not in dangerous, f"{phrase!r} reached {intent}"


class TestSpokenPhrasings:
    """Speech-to-text output, not tidy typing.

    Whisper drops apostrophes and sometimes emits the curly one; each miss
    used to send a free, instant question to a paid model instead.
    """

    @pytest.mark.parametrize(
        "text",
        ["what's my battery", "whats my battery", "what\u2019s my battery", "battery"],
    )
    def test_battery_is_recognized_however_it_is_written(self, text: str) -> None:
        intent = fastpath.match(text)
        assert intent is not None
        assert intent.subject == "battery"

    @pytest.mark.parametrize(
        ("text", "subject"),
        [
            ("gpu usage percentage", "gpu"),
            ("cpu usage", "cpu"),
            ("disk space left", "disk"),
            ("hows my ram", "ram"),
        ],
    )
    def test_measurement_words_do_not_break_the_match(self, text: str, subject: str) -> None:
        intent = fastpath.match(text)
        assert intent is not None
        assert intent.subject == subject

    def test_gpu_asks_the_tool_for_the_expensive_counter(self) -> None:
        intent = fastpath.match("gpu usage")
        assert intent is not None
        assert intent.args == {"include_gpu": True}

    def test_cpu_does_not_pay_for_the_gpu_counter(self) -> None:
        intent = fastpath.match("cpu usage")
        assert intent is not None
        assert intent.args == {}


class TestAnswersTheQuestionAsked:
    """Over-answering was a complaint in its own right.

    "What's my battery percentage" got a four-part hardware report; the
    formatter now returns the reading that was asked for.
    """

    STATUS: ClassVar[dict[str, object]] = {
        "cpu_percent": 12.0,
        "memory": {"total_gb": 16.0, "used_percent": 51.0},
        "disk": {"total_gb": 500.0, "free_gb": 120.0, "used_percent": 76.0},
        "battery": {"percent": 92, "plugged_in": True},
        "gpu": {"percent": 6.1},
    }

    def test_battery_question_gets_only_the_battery(self) -> None:
        intent = fastpath.Intent(name="status", formatter="status", subject="battery")
        reply = fastpath.format_reply(intent, self.STATUS)
        assert reply == "92%, plugged in."

    def test_gpu_question_gets_only_the_gpu(self) -> None:
        intent = fastpath.Intent(name="status", formatter="status", subject="gpu")
        assert fastpath.format_reply(intent, self.STATUS) == "GPU is at 6.1%."

    def test_missing_gpu_counter_is_admitted_not_invented(self) -> None:
        intent = fastpath.Intent(name="status", formatter="status", subject="gpu")
        reply = fastpath.format_reply(intent, {**self.STATUS, "gpu": None})
        assert "didn't report" in reply

    def test_general_status_still_reports_everything(self) -> None:
        intent = fastpath.Intent(name="status", formatter="status", subject="system")
        reply = fastpath.format_reply(intent, self.STATUS)
        assert "CPU" in reply and "battery" in reply and "disk" in reply


class TestWebShortcuts:
    def test_site_names_open_the_site_not_a_missing_app(self) -> None:
        """'open youtube' used to hunt for an installed program and fail."""
        intent = fastpath.match("open youtube")
        assert intent is not None
        assert intent.tool == "apps.open_url"
        assert intent.args["url"] == "https://www.youtube.com"

    def test_google_query_becomes_a_search(self) -> None:
        intent = fastpath.match("google best laptop 2026")
        assert intent is not None
        assert intent.args["url"] == "https://www.google.com/search?q=best+laptop+2026"

    def test_play_on_youtube_searches_youtube(self) -> None:
        intent = fastpath.match("play despacito on youtube")
        assert intent is not None
        assert "youtube.com/results?search_query=despacito" in intent.args["url"]

    def test_bare_search_is_left_to_the_model(self) -> None:
        """'search for X' could mean the filesystem, so do not guess."""
        assert fastpath.match("search for invoices") is None

    def test_installed_apps_still_win_over_site_names(self) -> None:
        intent = fastpath.match("open chrome")
        assert intent is not None
        assert intent.tool == "apps.open"


class TestWakeWordInTranscript:
    """The wake word lands inside the transcript, spelled however it was heard.

    Live example: "Hey Javis!" reached the model and came back in Chinese,
    when it should have been a free greeting.
    """

    @pytest.mark.parametrize(
        "text", ["Hey Javis!", "hey jarvis", "Jarvis.", "hey jervis,", "Alexa"]
    )
    def test_a_bare_wake_word_is_a_greeting(self, text: str) -> None:
        intent = fastpath.match(text)
        assert intent is not None
        assert intent.name == "greeting"

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("Hey Jarvis, what is the time now?", "time"),
            ("jarvis open chrome", "open"),
            ("Hey Javis, what's my battery", "status"),
        ],
    )
    def test_the_request_after_the_wake_word_still_matches(self, text: str, expected: str) -> None:
        intent = fastpath.match(text)
        assert intent is not None
        assert intent.name == expected

    def test_ordinary_words_are_not_stripped(self) -> None:
        assert fastpath.strip_wake_word("hey there") == "hey there"
        assert fastpath.strip_wake_word("open chrome") == "open chrome"


class TestTimePhrasings:
    @pytest.mark.parametrize(
        "text",
        [
            "what's the time now",
            "whats the time now?",
            "what time is it right now",
            "what's the time",
            "time now",
        ],
    )
    def test_trailing_now_still_matches(self, text: str) -> None:
        """Missing this sent the question to a model, which invented a time."""
        intent = fastpath.match(text)
        assert intent is not None
        assert intent.name == "time"

    def test_the_reply_comes_from_the_clock(self) -> None:
        from datetime import datetime

        intent = fastpath.match("what's the time now")
        assert intent is not None
        assert intent.tool is None  # no tool, no model, no chance to hallucinate
        assert datetime.now().astimezone().strftime("%p") in intent.reply
