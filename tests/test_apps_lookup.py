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
    def test_open_folder_by_bare_name(
        self, context: ToolContext, sandbox: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        spawned: list[list[str]] = []
        monkeypatch.setattr(apps, "_spawn", lambda argv, flags: spawned.append(argv))

        result = apps.open_target(context, target="Downloads")

        assert result["kind"] == "folder"
        assert spawned and "Downloads" in spawned[0][-1]

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


class TestDetachedLaunch:
    """Apps must outlive MyAgent.

    The launcher runs every MyAgent process inside a Windows job object with
    KILL_ON_JOB_CLOSE, and a job member's children join that job - so an app
    opened for the user died the moment MyAgent stopped. Measured on this
    machine: a plain child does not survive; a CREATE_BREAKAWAY_FROM_JOB child
    and a shell-forwarded one both do.
    """

    def test_executables_break_away_from_the_job(
        self, context: ToolContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[tuple[list[str], int]] = []
        monkeypatch.setattr(apps, "_spawn", lambda argv, flags: seen.append((argv, flags)))
        monkeypatch.setattr(apps, "find_application", lambda name: ("exe", r"C:\fake\notepad.exe"))

        result = apps.open_target(context, target="notepad")

        argv, flags = seen[0]
        assert argv == [r"C:\fake\notepad.exe"], "launched directly, argv form, no shell"
        assert flags & apps.CREATE_BREAKAWAY_FROM_JOB, "would die with MyAgent"
        assert result["kind"] == "application"

    def test_shortcuts_are_forwarded_to_explorer(
        self, context: ToolContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Shortcuts need the shell's association logic, which Explorer owns."""
        seen: list[list[str]] = []
        monkeypatch.setattr(apps, "_spawn", lambda argv, flags: seen.append(argv))
        monkeypatch.setattr(
            apps, "find_application", lambda name: ("shortcut", r"C:\fake\Spotify.lnk")
        )

        apps.open_target(context, target="spotify")

        assert seen == [["explorer.exe", r"C:\fake\Spotify.lnk"]]

    def test_folders_are_forwarded_to_explorer(
        self, context: ToolContext, sandbox: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[list[str]] = []
        monkeypatch.setattr(apps, "_spawn", lambda argv, flags: seen.append(argv))

        result = apps.open_target(context, target="Downloads")

        assert seen == [["explorer.exe", str(sandbox / "Downloads")]]
        assert result["kind"] == "folder"

    def test_a_refused_launch_is_reported_not_swallowed(
        self, context: ToolContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def refuse(_argv: list[str], _flags: int) -> None:
            raise OSError("the system cannot find the file specified")

        monkeypatch.setattr(apps, "_spawn", refuse)
        monkeypatch.setattr(apps, "find_application", lambda name: ("exe", r"C:\fake\thing.exe"))

        with pytest.raises(ToolError, match="Windows refused to open"):
            apps.open_target(context, target="thing")

    def test_breakaway_is_retried_without_the_flag(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A job that forbids breakaway must not stop the app from opening."""
        attempts: list[int] = []

        def popen(argv: list[str], creationflags: int, **kwargs: object) -> None:
            attempts.append(creationflags)
            if creationflags & apps.CREATE_BREAKAWAY_FROM_JOB:
                raise OSError("access is denied")

        monkeypatch.setattr(apps.subprocess, "Popen", popen)
        apps._spawn(["x.exe"], apps.CREATE_BREAKAWAY_FROM_JOB | apps.DETACHED_PROCESS)

        assert len(attempts) == 2
        assert not attempts[1] & apps.CREATE_BREAKAWAY_FROM_JOB


class TestJobObjectPermitsBreakaway:
    def test_the_real_job_has_both_limits_set(self) -> None:
        """Read the flags back off a live job object.

        Without BREAKAWAY_OK, CREATE_BREAKAWAY_FROM_JOB is rejected with
        ERROR_ACCESS_DENIED and every app we open dies with MyAgent; without
        KILL_ON_JOB_CLOSE, our own satellites leak. Both must be on.
        """
        import ctypes
        from typing import Any

        from myagent import jobs

        group = jobs.ProcessGroup()
        try:
            assert group.active, "no job object on this platform"
            info = jobs._ExtendedLimitInformation()
            kernel32: Any = ctypes.windll.kernel32
            ok = kernel32.QueryInformationJobObject(
                group._handle,
                jobs.JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                ctypes.byref(info),
                ctypes.sizeof(info),
                None,
            )
            assert ok, "could not read the job's limits back"
            flags = info.BasicLimitInformation.LimitFlags
            assert flags & jobs.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            assert flags & jobs.JOB_OBJECT_LIMIT_BREAKAWAY_OK
        finally:
            group.close()


class TestBrowserResolution:
    def test_browser_words_resolve_to_the_configured_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ "Open my browser" must open the user's browser, not a hardcoded one."""
        monkeypatch.setattr(
            "myagent.tools.applookup.default_browser", lambda: r"C:\fake\firefox.exe"
        )
        assert find_application("browser") == ("exe", r"C:\fake\firefox.exe")
        assert find_application("web browser") == ("exe", r"C:\fake\firefox.exe")

    def test_unreadable_default_falls_back_to_normal_lookup(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("myagent.tools.applookup.default_browser", lambda: None)
        assert find_application("browser") is None

    def test_default_browser_is_an_existing_executable(self) -> None:
        """On a real Windows install this must resolve to a file on disk."""
        from myagent.tools.applookup import default_browser

        resolved = default_browser()
        assert resolved is None or Path(resolved).exists()


class TestGpuUsage:
    def test_gpu_is_opt_in(self, context: ToolContext, monkeypatch: pytest.MonkeyPatch) -> None:
        """The counter costs ~2s, so a plain status check must not pay it."""
        monkeypatch.setattr(apps, "gpu_usage", lambda: pytest.fail("should not be queried"))
        assert apps.system_status(context)["gpu"] is None

    def test_gpu_included_when_requested(
        self, context: ToolContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(apps, "gpu_usage", lambda: {"percent": 42.0})
        assert apps.system_status(context, include_gpu=True)["gpu"] == {"percent": 42.0}

    def test_real_counter_returns_a_percentage_or_nothing(self) -> None:
        """Against the live machine: a plausible reading, or an honest None."""
        reading = apps.gpu_usage()
        assert reading is None or 0.0 <= reading["percent"] <= 100.0
