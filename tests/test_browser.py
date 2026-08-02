"""Browsing tests: distillation, and the security property that matters.

The distillation tests drive a real headless Chromium against a local fixture
file - no network, but a genuine DOM, because the whole point of distillation
is what a real browser does with real CSS and scripts.

The security tests need no browser: they assert the rule that a page can talk
but never act.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from myagent.config import Settings, ToolSettings
from myagent.security.broker import PermissionBroker
from myagent.security.taint import TurnContext
from myagent.security.tiers import Decision, Tier
from myagent.tools import browser, browsing
from myagent.tools.registry import ToolContext, ToolError

FIXTURE = """
<html><head><title>Widget Docs</title></head><body>
<nav>site navigation</nav>
<h1>Widgets</h1>
<p>A widget is a small mechanical device used for demonstration purposes in
documentation and examples throughout the software industry at large.</p>
<a href="https://example.com/docs">Read the docs</a>
<button>Buy now</button>
<input type="text" name="email" placeholder="Your email">
<span style="display:none"><a href="https://hidden.example">invisible</a></span>
<script>var tracking = 1;</script>
</body></html>
"""


@pytest.fixture
def page_file(tmp_path: Path) -> str:
    path = tmp_path / "fixture.html"
    path.write_text(FIXTURE, encoding="utf-8")
    return path.as_uri()


@pytest.fixture
def live_browser() -> Any:
    """A real Chromium, closed afterwards. Skips if it is not installed."""
    session = browsing.BrowserSession()
    try:
        session.run(lambda page: page.title())
    except browsing.BrowserUnavailableError as exc:
        session.stop()
        pytest.skip(f"no browser available: {exc}")
    yield session
    session.stop()


@pytest.fixture
def context(settings: Settings, tmp_path: Path) -> ToolContext:
    scoped = settings.model_copy(update={"tools": ToolSettings(roots=[str(tmp_path)])})
    return ToolContext(
        turn=TurnContext(session_id="s"), db_path=settings.db_path(), settings=scoped
    )


class TestDistillation:
    def test_interactive_elements_are_extracted_with_refs(
        self, live_browser: Any, page_file: str
    ) -> None:
        page = live_browser.goto(page_file)
        roles = [element.role for element in page.elements]

        assert page.title == "Widget Docs"
        assert "link" in roles and "button" in roles and "input:text" in roles
        assert all(element.ref.startswith("e") for element in page.elements)

    def test_links_carry_their_destination(self, live_browser: Any, page_file: str) -> None:
        page = live_browser.goto(page_file)
        links = [element for element in page.elements if element.role == "link"]
        assert links[0].href == "https://example.com/docs"

    def test_invisible_elements_are_skipped(self, live_browser: Any, page_file: str) -> None:
        """A model must not be offered a control the user cannot see."""
        page = live_browser.goto(page_file)
        assert not any("hidden.example" in (element.href or "") for element in page.elements)

    def test_scripts_and_chrome_are_stripped_from_the_text(
        self, live_browser: Any, page_file: str
    ) -> None:
        page = live_browser.goto(page_file)
        assert "widget is a small mechanical device" in page.text
        assert "var tracking" not in page.text
        assert "site navigation" not in page.text

    def test_the_page_survives_being_read_twice(self, live_browser: Any, page_file: str) -> None:
        """Refs are reassigned on each read, not left stale from the last one."""
        live_browser.goto(page_file)
        again = live_browser.read()
        assert [element.ref for element in again.elements] == [
            f"e{index + 1}" for index in range(len(again.elements))
        ]

    def test_filling_a_field_by_ref(self, live_browser: Any, page_file: str) -> None:
        page = live_browser.goto(page_file)
        field = next(element for element in page.elements if element.role.startswith("input"))
        after = live_browser.fill(field.ref, "someone@example.com")
        refilled = next(element for element in after.elements if element.ref == field.ref)
        assert refilled.value == "someone@example.com"

    def test_an_unknown_ref_is_an_honest_error(self, live_browser: Any, page_file: str) -> None:
        live_browser.goto(page_file)
        with pytest.raises(LookupError, match="no element e99"):
            live_browser.click("e99")

    def test_long_pages_are_truncated_and_say_so(self, tmp_path: Path, live_browser: Any) -> None:
        """A page's text is resent on every later step; it cannot be unbounded."""
        big = tmp_path / "big.html"
        body = "<p>" + ("lorem ipsum dolor sit amet. " * 2000) + "</p>"
        big.write_text(f"<html><body>{body}</body></html>", encoding="utf-8")

        page = live_browser.goto(big.as_uri())
        assert len(page.text) <= browsing.MAX_TEXT_CHARS
        assert page.truncated is True


class TestBrowserToolsTaint:
    """The property the M4 taint rule exists for."""

    def test_opening_a_page_taints_the_turn(
        self, context: ToolContext, page_file: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            browsing.BrowserSession,
            "goto",
            lambda self, url: browsing.Distilled(url=url, title="t", text="hello"),
        )
        browser.open_page(context, url="https://example.com")
        assert context.turn.tainted is True

    def test_a_failed_page_load_still_taints(
        self, context: ToolContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Partial reads count: we may have consumed content before failing."""

        def explode(self: Any, url: str) -> None:
            raise ConnectionError("reset by peer")

        monkeypatch.setattr(browsing.BrowserSession, "goto", explode)
        with pytest.raises(ToolError):
            browser.open_page(context, url="https://example.com")
        assert context.turn.tainted is True

    def test_a_missing_browser_is_reported_with_the_fix(
        self, context: ToolContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def missing(self: Any, url: str) -> None:
            raise browsing.BrowserUnavailableError("Playwright is not installed. Run: uv sync")

        monkeypatch.setattr(browsing.BrowserSession, "goto", missing)
        with pytest.raises(ToolError, match="uv sync"):
            browser.open_page(context, url="https://example.com")

    def test_non_web_schemes_are_refused(self, context: ToolContext) -> None:
        with pytest.raises(ToolError, match="only http and https"):
            browser.open_page(context, url="file:///C:/Windows/System32")

    def test_a_tainted_turn_forces_confirmation_for_any_write(
        self, settings: Settings, db: sqlite3.Connection
    ) -> None:
        """The integration that matters: a page said 'delete X', so ask first.

        Without this, a web page could reach a standing permission grant and
        act through it. With it, injected text can talk but never act.
        """
        broker = PermissionBroker(settings.db_path())
        turn = TurnContext(session_id="s")
        broker.add_grant("files.delete", scope="always", session_id=None)

        clean, _ = broker.authorize("files.delete", Tier.CONFIRM_ALWAYS, {}, turn)
        assert clean is Decision.ALLOW, "the grant applies on a clean turn"

        turn.taint("web page https://evil.example")
        decision, reason = broker.authorize("files.delete", Tier.CONFIRM_ALWAYS, {}, turn)

        assert decision is Decision.CONFIRM
        assert "untrusted" in reason and "evil.example" in reason

    def test_downloading_taints_even_though_nothing_was_read(
        self, context: ToolContext, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            browsing.BrowserSession,
            "download",
            lambda self, url, destination: Path(destination).write_bytes(b"data") or 4,
        )
        result = browser.download(context, url="https://example.com/f.txt", path="f.txt")
        assert result["bytes"] == 4
        assert context.turn.tainted is True

    def test_download_refuses_to_overwrite(self, context: ToolContext, tmp_path: Path) -> None:
        (tmp_path / "taken.txt").write_text("mine", encoding="utf-8")
        with pytest.raises(ToolError, match="already exists"):
            browser.download(context, url="https://example.com/f", path="taken.txt")

    def test_download_stays_inside_the_permitted_roots(self, context: ToolContext) -> None:
        with pytest.raises(ToolError):
            browser.download(context, url="https://example.com/f", path="../../escaped.txt")


class TestBrowserTiers:
    def test_reading_is_read_only_but_navigating_is_not(self) -> None:
        """Opening a page is an outward action; re-reading one is not."""
        from myagent.tools import registry

        registry.load_builtin_tools()
        assert registry.get_tool("browser.read").tier is Tier.READ
        assert registry.get_tool("browser.open").tier is Tier.REVERSIBLE

    def test_downloading_always_confirms(self) -> None:
        from myagent.tools import registry

        registry.load_builtin_tools()
        assert registry.get_tool("browser.download").tier is Tier.CONFIRM_ALWAYS


class TestRealProfileAttach:
    """Attaching to the user's own Chrome inherits every session they have.

    "Browse in my profile" and "act as me on every site I am signed into" are
    the same sentence; only one of them is obvious, so the tool is T2 and its
    confirmation says the second one out loud.
    """

    def test_it_always_confirms(self) -> None:
        from myagent.tools import registry

        registry.load_builtin_tools()
        assert registry.get_tool("browser.use_my_profile").tier is Tier.CONFIRM_ALWAYS

    def test_the_prompt_states_what_is_really_granted(self) -> None:
        from myagent.tools import registry

        registry.load_builtin_tools()
        summary = registry.get_tool("browser.use_my_profile").summary({})
        assert "signed into" in summary

    def test_a_missing_debug_port_explains_the_fix(
        self, context: ToolContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def refuse(cdp_url: str) -> None:
            raise browsing.BrowserUnavailableError(
                "could not attach to your browser at X. Start Chrome with remote debugging "
                "first: chrome.exe --remote-debugging-port=9222"
            )

        monkeypatch.setattr(browsing, "attach", refuse)
        with pytest.raises(ToolError, match="remote-debugging-port"):
            browser.use_my_profile(context)

    def test_plain_browsing_never_touches_the_real_profile(self) -> None:
        """The default session must be clean; attaching is opt-in only."""
        assert browsing.BrowserSession().cdp_url is None
        assert browsing.attached() is False
