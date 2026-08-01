"""App resolution and URL opening tests.

Regression coverage for a real complaint: "open chrome" and "open Downloads"
both failed with 'outside my permitted folders', because app lookup only
searched PATH and bare folder names were resolved against the process CWD.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from myagent.config import Settings, ToolSettings
from myagent.security.taint import TurnContext
from myagent.tools import apps
from myagent.tools.applookup import ALIASES, find_application, list_known_applications
from myagent.tools.paths import resolve_allowed
from myagent.tools.registry import ToolContext, ToolError


@pytest.fixture
def sandbox(tmp_path: Path) -> Path:
    root = tmp_path / "root"
    (root / "Downloads").mkdir(parents=True)
    (root / "notes.txt").write_text("hi", encoding="utf-8")
    return root


@pytest.fixture
def context(settings: Settings, sandbox: Path) -> ToolContext:
    scoped = settings.model_copy(update={"tools": ToolSettings(roots=[str(sandbox)])})
    return ToolContext(
        turn=TurnContext(session_id="s"), db_path=settings.db_path(), settings=scoped
    )


class TestPathResolution:
    def test_bare_folder_name_resolves_against_roots(self, sandbox: Path) -> None:
        """'Downloads' must mean <root>/Downloads, not CWD/Downloads."""
        resolved = resolve_allowed("Downloads", [sandbox], must_exist=True)
        assert resolved == (sandbox / "Downloads").resolve()

    def test_root_name_resolves_to_the_root_itself(self, tmp_path: Path) -> None:
        """ "Downloads" must find ~/Downloads when that IS a permitted root.

        Regression: it previously tried Desktop/Downloads, Documents/Downloads,
        ... and failed with "path does not exist" for the real folder.
        """
        real_root = Path.home() / "Downloads"
        if not real_root.exists():
            pytest.skip("no ~/Downloads on this machine")
        roots = [Path.home() / "Desktop", real_root]
        assert resolve_allowed("Downloads", roots, must_exist=True) == real_root.resolve()

    def test_relative_subpath_resolves(self, sandbox: Path) -> None:
        resolved = resolve_allowed("Downloads/../notes.txt", [sandbox], must_exist=True)
        assert resolved.name == "notes.txt"

    def test_absolute_path_still_enforced(self, sandbox: Path, tmp_path: Path) -> None:
        with pytest.raises(ToolError, match="outside the permitted folders"):
            resolve_allowed(str(tmp_path / "elsewhere.txt"), [sandbox])

    def test_traversal_still_blocked_via_relative_names(self, sandbox: Path) -> None:
        with pytest.raises(ToolError, match="outside the permitted folders"):
            resolve_allowed("../secret.txt", [sandbox])

    def test_missing_file_inside_root_says_so(self, sandbox: Path) -> None:
        """A permitted-but-absent path must not be reported as 'outside'."""
        with pytest.raises(ToolError, match="does not exist"):
            resolve_allowed("Downloads/nope.txt", [sandbox], must_exist=True)


class TestApplicationLookup:
    def test_finds_a_windows_builtin(self) -> None:
        found = find_application("notepad")
        assert found is not None
        kind, target = found
        assert kind in ("exe", "shortcut")
        assert "notepad" in target.lower()

    def test_finds_explorer_by_spoken_name(self) -> None:
        """'file explorer' is what people say; explorer.exe is what Windows knows."""
        found = find_application("file explorer")
        assert found is not None
        assert "explorer" in found[1].lower()

    def test_protocol_alias_is_reported_as_uri(self) -> None:
        found = find_application("settings")
        assert found == ("uri", ALIASES["settings"])

    def test_unknown_application_returns_none(self) -> None:
        assert find_application("definitely-not-installed-xyz") is None

    def test_start_menu_listing_is_available(self) -> None:
        names = list_known_applications(limit=10)
        assert isinstance(names, list)  # may be empty on a bare CI image


class TestOpenTool:
    def test_open_folder_by_bare_name(self, context: ToolContext, sandbox: Path) -> None:
        opened: list[str] = []
        import myagent.tools.apps as apps_module

        original = apps_module.os.startfile
        apps_module.os.startfile = lambda path: opened.append(str(path))  # type: ignore[assignment]
        try:
            result = apps.open_target(context, target="Downloads")
        finally:
            apps_module.os.startfile = original  # type: ignore[assignment]
        assert result["kind"] == "folder"
        assert opened and "Downloads" in opened[0]

    def test_unknown_target_error_lists_alternatives(self, context: ToolContext) -> None:
        with pytest.raises(ToolError, match="could not find an application"):
            apps.open_target(context, target="totally-not-a-thing-xyz")

    def test_empty_target_rejected(self, context: ToolContext) -> None:
        with pytest.raises(ToolError, match="empty"):
            apps.open_target(context, target="  ")


class TestOpenUrl:
    def test_adds_scheme_and_opens(self, context: ToolContext) -> None:
        seen: list[str] = []
        import myagent.tools.apps as apps_module

        original = apps_module.webbrowser.open
        apps_module.webbrowser.open = lambda url: seen.append(url) or True  # type: ignore[assignment]
        try:
            result = apps.open_url(context, url="youtube.com")
        finally:
            apps_module.webbrowser.open = original  # type: ignore[assignment]
        assert result["opened"] == "https://youtube.com"
        assert seen == ["https://youtube.com"]

    def test_rejects_non_web_schemes(self, context: ToolContext) -> None:
        with pytest.raises(ToolError, match="only http and https"):
            apps.open_url(context, url="file:///C:/Windows/System32")

    def test_rejects_empty(self, context: ToolContext) -> None:
        with pytest.raises(ToolError, match="empty"):
            apps.open_url(context, url="")
