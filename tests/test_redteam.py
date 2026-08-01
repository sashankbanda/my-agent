"""Red-team suite: the M4 exit gate.

Every test here is an attack that must fail. These are release gates - if one
regresses, the assistant is dangerous, not merely broken. Each test names the
requirement it defends.
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

from myagent.config import Settings, ToolSettings
from myagent.security.broker import PermissionBroker
from myagent.security.confirm import Answer, ConfirmationService
from myagent.security.taint import TurnContext
from myagent.security.tiers import Decision, Tier
from myagent.tools import files, shell
from myagent.tools.executor import ToolExecutor
from myagent.tools.paths import resolve_allowed
from myagent.tools.registry import ToolContext, ToolError, load_builtin_tools

load_builtin_tools()


@pytest.fixture
def sandbox(tmp_path: Path) -> Path:
    root = tmp_path / "root"
    root.mkdir(parents=True)
    (root / "keep.txt").write_text("keep me", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("outside the wall", encoding="utf-8")
    return root


@pytest.fixture
def scoped_settings(settings: Settings, sandbox: Path) -> Settings:
    return settings.model_copy(update={"tools": ToolSettings(roots=[str(sandbox)])})


@pytest.fixture
def context(scoped_settings: Settings, sandbox: Path) -> ToolContext:
    return ToolContext(
        turn=TurnContext(session_id="s"),
        db_path=scoped_settings.db_path(),
        settings=scoped_settings,
    )


# --- SEC-05: filesystem allowlist ---------------------------------------------


@pytest.mark.parametrize(
    "attack",
    [
        "../secret.txt",
        "..\\secret.txt",
        "sub/../../secret.txt",
        "./../secret.txt",
        "../../Windows/System32/drivers/etc/hosts",
        "C:/Windows/System32/config/SAM",
        "%SYSTEMROOT%/System32/config/SAM",
        "~/../../secret.txt",
    ],
)
def test_path_traversal_is_blocked(context: ToolContext, sandbox: Path, attack: str) -> None:
    """Traversal, absolute escapes, env vars, and ~ must all fail closed."""
    candidate = (
        attack if Path(attack).is_absolute() or attack.startswith("%") else str(sandbox / attack)
    )
    with pytest.raises(ToolError, match=r"outside the permitted folders|does not exist"):
        files.read_text(context, path=candidate)


def test_symlink_escape_is_blocked(context: ToolContext, sandbox: Path, tmp_path: Path) -> None:
    """A symlink pointing outside the roots must not grant access."""
    link = sandbox / "escape"
    try:
        link.symlink_to(tmp_path / "secret.txt")
    except OSError:
        pytest.skip("symlink creation requires privileges on this machine")
    with pytest.raises(ToolError, match="outside the permitted folders"):
        files.read_text(context, path=str(link))


def test_write_outside_roots_is_blocked(context: ToolContext, tmp_path: Path) -> None:
    with pytest.raises(ToolError, match="outside the permitted folders"):
        files.write_text(context, path=str(tmp_path / "planted.txt"), content="pwned")
    assert not (tmp_path / "planted.txt").exists()


def test_delete_outside_roots_is_blocked(context: ToolContext, tmp_path: Path) -> None:
    with pytest.raises(ToolError, match="outside the permitted folders"):
        files.delete(context, path=str(tmp_path / "secret.txt"))
    assert (tmp_path / "secret.txt").exists()


def test_resolve_allowed_accepts_paths_inside_root(sandbox: Path) -> None:
    assert resolve_allowed(str(sandbox / "keep.txt"), [sandbox]).name == "keep.txt"


# --- shell: no operator injection --------------------------------------------


def test_shell_refuses_interpreters(context: ToolContext, sandbox: Path) -> None:
    """A shell interpreter would re-parse operators inside its own process.

    Regression guard for a real finding: `cmd /c echo hi && echo pwned > f`
    created the file, because cmd - not our argv - did the chaining.
    """
    marker = sandbox / "pwned.txt"
    for attack in (
        f'cmd /c echo hi && echo pwned > "{marker}"',
        f'powershell -c "echo pwned > {marker}"',
        f"bash -c 'echo pwned > {marker}'",
    ):
        with pytest.raises(ToolError, match=r"shell interpreter|shell operators"):
            shell.run(context, command=attack, cwd=str(sandbox))
    assert not marker.exists()


def test_shell_rejects_operator_syntax(context: ToolContext, sandbox: Path) -> None:
    """Even for non-interpreters, operators are refused rather than passed on."""
    for attack in ("git status && rm -rf .", "type notes.txt | findstr x", "echo x > out.txt"):
        with pytest.raises(ToolError, match="shell operators"):
            shell.run(context, command=attack, cwd=str(sandbox))


def test_shell_runs_a_plain_program(context: ToolContext, sandbox: Path) -> None:
    """The allowed shape: one program, arguments, no operators."""
    result = shell.run(context, command="where.exe where", cwd=str(sandbox))
    assert result["exit_code"] == 0
    assert "where" in result["stdout"].lower()


def test_shell_cwd_must_be_inside_roots(context: ToolContext, tmp_path: Path) -> None:
    with pytest.raises(ToolError, match="outside the permitted folders"):
        shell.run(context, command="where.exe where", cwd=str(tmp_path))


def test_shell_output_taints_the_turn(context: ToolContext, sandbox: Path) -> None:
    shell.run(context, command="where.exe where", cwd=str(sandbox))
    assert context.turn.tainted is True


def test_shell_rejects_empty_command(context: ToolContext) -> None:
    with pytest.raises(ToolError, match="empty"):
        shell.run(context, command="   ")


# --- SEC-07: taint escalation suspension -------------------------------------


def test_taint_suspends_standing_grants(db: sqlite3.Connection, settings: Settings) -> None:
    """The core injection defense: reading untrusted content voids grants."""
    broker = PermissionBroker(settings.db_path())
    broker.add_grant("files.delete", "always", None)
    turn = TurnContext(session_id="s")

    # Clean turn: the grant applies.
    assert broker.authorize("files.delete", Tier.CONFIRM_ALWAYS, {}, turn)[0] is Decision.ALLOW

    # After reading a file, the same call must ask the human again.
    turn.taint("file contents (invoice.txt)")
    decision, reason = broker.authorize("files.delete", Tier.CONFIRM_ALWAYS, {}, turn)
    assert decision is Decision.CONFIRM
    assert "untrusted content" in reason
    assert "invoice.txt" in reason  # the user is told WHY


def test_taint_also_escalates_reversible_writes(db: sqlite3.Connection, settings: Settings) -> None:
    broker = PermissionBroker(settings.db_path())
    turn = TurnContext(session_id="s")
    assert broker.authorize("files.move", Tier.REVERSIBLE, {}, turn)[0] is Decision.ALLOW
    turn.taint("web page")
    assert broker.authorize("files.move", Tier.REVERSIBLE, {}, turn)[0] is Decision.CONFIRM


def test_taint_does_not_block_reading(db: sqlite3.Connection, settings: Settings) -> None:
    """Reading stays free after taint - only *acting* escalates."""
    broker = PermissionBroker(settings.db_path())
    turn = TurnContext(session_id="s")
    turn.taint("file contents")
    assert broker.authorize("files.list_dir", Tier.READ, {}, turn)[0] is Decision.ALLOW


# --- executor: end-to-end enforcement ----------------------------------------


def build_executor(
    settings: Settings, answer: Answer | None = None
) -> tuple[ToolExecutor, PermissionBroker, ConfirmationService]:
    broker = PermissionBroker(settings.db_path())
    confirmations = ConfirmationService()
    if answer is not None:

        async def auto_answer(payload: dict[str, object]) -> None:
            request_id = str(payload.get("id"))
            asyncio.get_running_loop().call_soon(confirmations.resolve, request_id, answer)

        confirmations.add_notifier(auto_answer)
    executor = ToolExecutor(settings.db_path(), settings, broker, confirmations)
    return executor, broker, confirmations


async def test_executor_denies_when_user_declines(
    db: sqlite3.Connection, scoped_settings: Settings, sandbox: Path
) -> None:
    executor, _, _ = build_executor(scoped_settings, Answer(allowed=False))
    turn = TurnContext(session_id="s")
    result = await executor.execute("files.delete", {"path": str(sandbox / "keep.txt")}, turn)
    assert "declined" in result["error"]
    assert (sandbox / "keep.txt").exists()  # nothing happened


async def test_executor_runs_after_approval_and_stores_grant(
    db: sqlite3.Connection, scoped_settings: Settings, sandbox: Path
) -> None:
    executor, broker, _ = build_executor(scoped_settings, Answer(allowed=True, scope="session"))
    turn = TurnContext(session_id="s")
    result = await executor.execute("files.delete", {"path": str(sandbox / "keep.txt")}, turn)
    assert result.get("deleted") is True
    assert not (sandbox / "keep.txt").exists()
    assert broker.has_grant("files.delete", "s") is True


async def test_executor_denies_with_no_confirmation_channel(
    db: sqlite3.Connection, scoped_settings: Settings, sandbox: Path
) -> None:
    """No UI connected must mean denial, never silent approval."""
    executor, _, _ = build_executor(scoped_settings, answer=None)
    turn = TurnContext(session_id="s")
    result = await executor.execute("files.delete", {"path": str(sandbox / "keep.txt")}, turn)
    assert "error" in result
    assert (sandbox / "keep.txt").exists()


async def test_kill_switch_stops_execution_mid_task(
    db: sqlite3.Connection, scoped_settings: Settings, sandbox: Path
) -> None:
    """SEC-04: engaging the stop blocks the very next tool call."""
    executor, broker, _ = build_executor(scoped_settings, Answer(allowed=True))
    turn = TurnContext(session_id="s")
    first = await executor.execute("files.list_dir", {"path": str(sandbox)}, turn)
    assert "entries" in first

    broker.kill_switch.engage()
    blocked = await executor.execute("files.list_dir", {"path": str(sandbox)}, turn)
    assert "emergency stop" in blocked["error"]


async def test_kill_switch_wins_race_against_pending_confirmation(
    db: sqlite3.Connection, scoped_settings: Settings, sandbox: Path
) -> None:
    """Approving, then hitting stop before execution, must still not run."""
    broker = PermissionBroker(scoped_settings.db_path())
    confirmations = ConfirmationService()

    async def approve_then_kill(payload: dict[str, object]) -> None:
        broker.kill_switch.engage()  # stop pressed while the prompt is open
        asyncio.get_running_loop().call_soon(
            confirmations.resolve, str(payload.get("id")), Answer(allowed=True)
        )

    confirmations.add_notifier(approve_then_kill)
    executor = ToolExecutor(scoped_settings.db_path(), scoped_settings, broker, confirmations)
    result = await executor.execute(
        "files.delete", {"path": str(sandbox / "keep.txt")}, TurnContext(session_id="s")
    )
    assert "emergency stop" in result["error"]
    assert (sandbox / "keep.txt").exists()


async def test_unknown_tool_is_a_recoverable_error(
    db: sqlite3.Connection, scoped_settings: Settings
) -> None:
    executor, _, _ = build_executor(scoped_settings)
    result = await executor.execute("files.nuke_everything", {}, TurnContext(session_id="s"))
    assert "unknown tool" in result["error"]


async def test_bad_arguments_are_recoverable(
    db: sqlite3.Connection, scoped_settings: Settings
) -> None:
    executor, _, _ = build_executor(scoped_settings)
    result = await executor.execute("files.list_dir", {"wrong": "arg"}, TurnContext(session_id="s"))
    assert "invalid arguments" in result["error"]


async def test_every_call_is_audited(
    db: sqlite3.Connection, scoped_settings: Settings, sandbox: Path
) -> None:
    executor, _, _ = build_executor(scoped_settings)
    await executor.execute("files.list_dir", {"path": str(sandbox)}, TurnContext(session_id="s"))
    types = [row["type"] for row in db.execute("SELECT type FROM events ORDER BY id")]
    assert "ToolCallRequested" in types
    assert "PermissionDecided" in types
    assert "ToolCallCompleted" in types
