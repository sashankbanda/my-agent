"""Tool-loop tests: multi-step reasoning, bounds, and error recovery.

The loop is driven with a scripted client so the agent logic is tested
deterministically, with no model flakiness (playbook: FakeLLM integration).
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from myagent.config import Settings, ToolSettings
from myagent.core import history
from myagent.core.loop import LEAKED_CALL_REPLY, AgentLoop
from myagent.gateway.gateway import Gateway
from myagent.gateway.health import HealthTracker
from myagent.gateway.quota import QuotaGovernor
from myagent.gateway.types import ChatMessage, ModelSpec, ToolCall
from myagent.security.broker import PermissionBroker
from myagent.security.confirm import Answer, ConfirmationService
from myagent.tools.executor import ToolExecutor
from myagent.tools.registry import load_builtin_tools
from tests.fakes import make_registry

load_builtin_tools()


class ScriptedClient:
    """Replays a list of turns: each is text plus optional tool calls."""

    def __init__(self, turns: list[tuple[str, list[ToolCall]]]) -> None:
        self.turns = turns
        self.index = 0
        self.seen_tools: list[list[str]] = []  # tool names offered per request
        self.transcripts: list[list[ChatMessage]] = []

    def stream(
        self,
        spec: ModelSpec,
        messages: list[ChatMessage],
        usage_out: dict[str, int],
        max_tokens: int | None = None,
        tools: list[dict[str, object]] | None = None,
        tool_calls_out: list[ToolCall] | None = None,
    ) -> AsyncIterator[str]:
        self.transcripts.append(list(messages))
        self.seen_tools.append(
            [] if not tools else [t["function"]["name"] for t in tools]  # type: ignore[index]
        )
        text, calls = self.turns[min(self.index, len(self.turns) - 1)]
        self.index += 1

        async def run() -> AsyncIterator[str]:
            for word in text.split(" "):
                yield word + " "
            if tool_calls_out is not None:
                tool_calls_out.extend(calls)

        return run()


def call(name: str, **args: object) -> ToolCall:
    return ToolCall(id=f"c{name}", name=name, arguments=json.dumps(args))


@pytest.fixture
def sandbox(tmp_path: Path) -> Path:
    root = tmp_path / "root"
    root.mkdir()
    (root / "a.txt").write_text("alpha", encoding="utf-8")
    return root


def build(
    settings: Settings,
    sandbox: Path,
    turns: list[tuple[str, list[ToolCall]]],
    answer: Answer | None = None,
    max_steps: int = 12,
) -> tuple[AgentLoop, ScriptedClient]:
    scoped = settings.model_copy(update={"tools": ToolSettings(roots=[str(sandbox)])})
    client = ScriptedClient(turns)
    gateway = Gateway(
        registry=make_registry(),
        quota=QuotaGovernor(scoped.db_path()),
        health=HealthTracker(scoped.db_path()),
        client=client,
        db_path=scoped.db_path(),
    )
    broker = PermissionBroker(scoped.db_path())
    confirmations = ConfirmationService()
    if answer is not None:
        import asyncio

        async def auto(payload: dict[str, object]) -> None:
            asyncio.get_running_loop().call_soon(
                confirmations.resolve, str(payload.get("id")), answer
            )

        confirmations.add_notifier(auto)
    executor = ToolExecutor(scoped.db_path(), scoped, broker, confirmations)
    loop = AgentLoop(
        gateway,
        scoped.db_path(),
        executor=executor,
        max_steps=max_steps,
        fast_path=False,  # these tests drive the model's tool loop directly
    )
    return loop, client


async def drain(loop: AgentLoop, session: str, text: str) -> str:
    """Collect the turn the way a real client does - honouring ``reset``.

    A reset means "discard what I sent you"; a consumer that ignores it shows
    text the loop deliberately withdrew.
    """
    delivered = ""
    async for chunk in loop.respond(session, text):
        if chunk.reset:
            delivered = ""
        delivered += chunk.delta
    return delivered


async def test_single_tool_call_then_answer(
    db: sqlite3.Connection, settings: Settings, sandbox: Path
) -> None:
    loop, client = build(
        settings,
        sandbox,
        [
            ("Let me look.", [call("files.list_dir", path=str(sandbox))]),
            ("You have one file: a.txt", []),
        ],
    )
    session = loop.ensure_session(None)
    await drain(loop, session, "what's in my folder?")

    # Two model calls: the request and the answer after the observation.
    assert client.index == 2
    # The second transcript must contain the tool result.
    roles = [m.role for m in client.transcripts[1]]
    assert "tool" in roles
    tool_message = next(m for m in client.transcripts[1] if m.role == "tool")
    assert "a.txt" in tool_message.content


async def test_multi_step_chain(db: sqlite3.Connection, settings: Settings, sandbox: Path) -> None:
    loop, _client = build(
        settings,
        sandbox,
        [
            ("Checking.", [call("files.list_dir", path=str(sandbox))]),
            ("Now organizing.", [call("files.make_dir", path=str(sandbox / "archive"))]),
            (
                "Moving it.",
                [
                    call(
                        "files.move",
                        source=str(sandbox / "a.txt"),
                        destination=str(sandbox / "archive/a.txt"),
                    )
                ],
            ),
            ("Done: moved a.txt into archive.", []),
        ],
    )
    session = loop.ensure_session(None)
    answer = await drain(loop, session, "organize my folder")
    assert (sandbox / "archive" / "a.txt").exists()
    assert "Done" in answer


async def test_tool_error_is_fed_back_for_recovery(
    db: sqlite3.Connection, settings: Settings, sandbox: Path
) -> None:
    """A failed tool must appear as an observation, not crash the turn."""
    loop, client = build(
        settings,
        sandbox,
        [
            ("Reading.", [call("files.read_text", path=str(sandbox / "missing.txt"))]),
            ("That file does not exist.", []),
        ],
    )
    session = loop.ensure_session(None)
    answer = await drain(loop, session, "read missing.txt")
    tool_message = next(m for m in client.transcripts[1] if m.role == "tool")
    assert "does not exist" in tool_message.content
    assert "does not exist" in answer


async def test_step_budget_stops_tool_use(
    db: sqlite3.Connection, settings: Settings, sandbox: Path
) -> None:
    """A model that loops forever is stopped and asked to summarize."""
    looping = [("Again.", [call("files.list_dir", path=str(sandbox))])]
    loop, client = build(settings, sandbox, looping, max_steps=3)
    session = loop.ensure_session(None)
    await drain(loop, session, "loop forever")

    assert client.index == 4  # 3 tool steps + one final summary request
    assert client.seen_tools[-1] == []  # tools withdrawn on the final request
    system_texts = [m.content for m in client.transcripts[-1] if m.role == "system"]
    assert any("reached this turn's limit" in text for text in system_texts)
    types = [row["type"] for row in db.execute("SELECT type FROM events")]
    assert "BudgetExceeded" in types


async def test_declined_tool_reported_honestly(
    db: sqlite3.Connection, settings: Settings, sandbox: Path
) -> None:
    loop, client = build(
        settings,
        sandbox,
        [
            ("Deleting.", [call("files.delete", path=str(sandbox / "a.txt"))]),
            ("Understood - I left the file alone.", []),
        ],
        answer=Answer(allowed=False),
    )
    session = loop.ensure_session(None)
    answer = await drain(loop, session, "delete a.txt")
    assert (sandbox / "a.txt").exists()
    tool_message = next(m for m in client.transcripts[1] if m.role == "tool")
    assert "declined" in tool_message.content
    assert "left the file alone" in answer


async def test_final_answer_is_persisted_without_tool_noise(
    db: sqlite3.Connection, settings: Settings, sandbox: Path
) -> None:
    loop, _ = build(
        settings,
        sandbox,
        [
            ("Looking.", [call("files.list_dir", path=str(sandbox))]),
            ("There is one file.", []),
        ],
    )
    session = loop.ensure_session(None)
    await drain(loop, session, "what's there?")
    messages = history.get_messages(settings.db_path(), session)
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert "one file" in messages[1]["content"]


async def test_plain_conversation_needs_no_tools(
    db: sqlite3.Connection, settings: Settings, sandbox: Path
) -> None:
    loop, client = build(settings, sandbox, [("Hello there!", [])])
    session = loop.ensure_session(None)
    answer = await drain(loop, session, "hi")
    assert client.index == 1
    assert "Hello there!" in answer


async def test_an_answer_with_a_real_tool_call_behind_it_is_kept(
    db: sqlite3.Connection, settings: Settings, sandbox: Path
) -> None:
    """Truth beats brevity.

    The anti-deflection guard replaces answers that only *describe* steps. An
    answer that actually ran a tool must survive even if it is wordy and
    mentions Explorer, or the guard would be discarding correct work.
    """
    loop, _ = build(
        settings,
        sandbox,
        turns=[
            ("Checking.", [call("files.list_dir", path=str(sandbox))]),
            ("You can use File Explorer too, but there is 1 item in that folder.", []),
        ],
    )
    session = loop.ensure_session(None)
    answer = await drain(loop, session, "list my downloads folder")

    assert "1 item" in answer
    assert answer.strip() != LEAKED_CALL_REPLY


async def test_a_described_action_with_no_tool_call_is_replaced(
    db: sqlite3.Connection, settings: Settings, sandbox: Path
) -> None:
    """The complaint, as a test: told to do it, it explained how instead."""
    guide = "You can use File Explorer and right-click to see that."
    loop, _ = build(settings, sandbox, turns=[(guide, []), (guide, [])])
    session = loop.ensure_session(None)
    answer = await drain(loop, session, "list my downloads folder")

    assert answer.strip() == LEAKED_CALL_REPLY
    assert "File Explorer" not in answer
