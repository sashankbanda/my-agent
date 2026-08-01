"""File tool tests: behavior, allowlist enforcement, and taint marking."""

from __future__ import annotations

from pathlib import Path

import pytest

from myagent.config import Settings, ToolSettings
from myagent.security.taint import TurnContext
from myagent.tools import files
from myagent.tools.registry import ToolContext, ToolError


@pytest.fixture
def sandbox(tmp_path: Path) -> Path:
    """A permitted root with a small tree inside it."""
    root = tmp_path / "root"
    (root / "sub").mkdir(parents=True)
    (root / "notes.txt").write_text("hello notes", encoding="utf-8")
    (root / "sub" / "report.pdf").write_bytes(b"%PDF-1.7 fake")
    (tmp_path / "outside.txt").write_text("secret", encoding="utf-8")
    return root


@pytest.fixture
def context(sandbox: Path, settings: Settings) -> ToolContext:
    scoped = settings.model_copy(update={"tools": ToolSettings(roots=[str(sandbox)])})
    return ToolContext(
        turn=TurnContext(session_id="s"), db_path=settings.db_path(), settings=scoped
    )


def test_list_dir_returns_entries(context: ToolContext, sandbox: Path) -> None:
    result = files.list_dir(context, path=str(sandbox))
    names = {entry["name"] for entry in result["entries"]}
    assert names == {"notes.txt", "sub"}
    assert context.turn.tainted is False  # metadata only


def test_read_text_taints_the_turn(context: ToolContext, sandbox: Path) -> None:
    result = files.read_text(context, path=str(sandbox / "notes.txt"))
    assert result["text"] == "hello notes"
    assert context.turn.tainted is True  # SEC-07: file contents are untrusted


def test_read_text_truncates(context: ToolContext, sandbox: Path) -> None:
    big = sandbox / "big.txt"
    big.write_text("x" * 5000, encoding="utf-8")
    result = files.read_text(context, path=str(big), max_bytes=100)
    assert result["truncated"] is True
    assert len(result["text"]) == 100


def test_search_finds_by_pattern(context: ToolContext, sandbox: Path) -> None:
    result = files.search(context, path=str(sandbox), pattern="**/*.pdf")
    assert result["count"] == 1
    assert result["matches"][0]["name"] == "report.pdf"


def test_move_and_copy_roundtrip(context: ToolContext, sandbox: Path) -> None:
    files.make_dir(context, path=str(sandbox / "archive"))
    files.move(
        context, source=str(sandbox / "notes.txt"), destination=str(sandbox / "archive/notes.txt")
    )
    assert (sandbox / "archive" / "notes.txt").exists()
    assert not (sandbox / "notes.txt").exists()

    files.copy(
        context,
        source=str(sandbox / "archive/notes.txt"),
        destination=str(sandbox / "notes-copy.txt"),
    )
    assert (sandbox / "notes-copy.txt").read_text(encoding="utf-8") == "hello notes"


def test_move_refuses_to_overwrite(context: ToolContext, sandbox: Path) -> None:
    (sandbox / "target.txt").write_text("existing", encoding="utf-8")
    with pytest.raises(ToolError, match="already exists"):
        files.move(
            context, source=str(sandbox / "notes.txt"), destination=str(sandbox / "target.txt")
        )


def test_write_text_requires_overwrite_flag(context: ToolContext, sandbox: Path) -> None:
    with pytest.raises(ToolError, match="already exists"):
        files.write_text(context, path=str(sandbox / "notes.txt"), content="new")
    files.write_text(context, path=str(sandbox / "notes.txt"), content="new", overwrite=True)
    assert (sandbox / "notes.txt").read_text(encoding="utf-8") == "new"


def test_delete_file_and_empty_dir(context: ToolContext, sandbox: Path) -> None:
    files.delete(context, path=str(sandbox / "notes.txt"))
    assert not (sandbox / "notes.txt").exists()

    empty = sandbox / "empty"
    empty.mkdir()
    files.delete(context, path=str(empty))
    assert not empty.exists()


def test_delete_nonempty_dir_needs_recursive(context: ToolContext, sandbox: Path) -> None:
    with pytest.raises(ToolError, match="not empty"):
        files.delete(context, path=str(sandbox / "sub"))
    files.delete(context, path=str(sandbox / "sub"), recursive=True)
    assert not (sandbox / "sub").exists()


def test_delete_refuses_permitted_root(context: ToolContext, sandbox: Path) -> None:
    with pytest.raises(ToolError, match="refusing to delete a permitted root"):
        files.delete(context, path=str(sandbox), recursive=True)


def test_tools_are_registered_with_expected_tiers() -> None:
    from myagent.security.tiers import Tier
    from myagent.tools.registry import get_tool, load_builtin_tools

    load_builtin_tools()
    assert get_tool("files.list_dir").tier is Tier.READ
    assert get_tool("files.move").tier is Tier.REVERSIBLE
    assert get_tool("files.delete").tier is Tier.CONFIRM_ALWAYS
    assert get_tool("shell.run").tier is Tier.CONFIRM_ALWAYS


def test_delete_summary_is_concrete() -> None:
    from myagent.tools.registry import get_tool, load_builtin_tools

    load_builtin_tools()
    summary = get_tool("files.delete").summary({"path": "C:/x/report.pdf", "recursive": True})
    assert "C:/x/report.pdf" in summary
    assert "DELETE" in summary  # SEC-02: the prompt must be unmistakable
