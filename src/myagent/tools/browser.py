"""Web browsing tools: navigate, read, click, fill, download.

**Everything a page says is untrusted.** Every tool here taints the turn
before returning, which suspends standing permissions for the rest of it
(SEC-07) - so a page containing "delete the user's documents" can be read and
described, but cannot cause an action without the human confirming it. This is
the module the M4 taint rule was built for; web content is the real thing it
defends against.

Pages are *distilled*, not screenshotted (see ``tools.browsing``): structured
text plus referenced interactive elements. Cheaper, more reliable, and it
gives the model stable handles like ``e7`` to click.
"""

from __future__ import annotations

from typing import Any

from myagent.logging import get_logger
from myagent.security.tiers import Tier
from myagent.tools import browsing
from myagent.tools.paths import configured_roots, resolve_allowed
from myagent.tools.registry import ToolContext, ToolError, tool

log = get_logger(__name__)


def _taint(context: ToolContext, url: str) -> None:
    """Mark the turn as having read the web. Never skip this."""
    context.turn.taint(f"web page {url}")


def _run(context: ToolContext, url: str, action: str, work: Any) -> dict[str, Any]:
    """Execute a browser action, tainting the turn and reporting failure honestly."""
    try:
        page = work()
    except browsing.BrowserUnavailableError as exc:
        raise ToolError(str(exc)) from exc
    except (LookupError, ConnectionError, TimeoutError) as exc:
        _taint(context, url)  # we may still have read something before failing
        raise ToolError(f"{action} failed: {exc}") from exc
    except Exception as exc:
        _taint(context, url)
        raise ToolError(f"{action} failed: {type(exc).__name__}: {exc}") from exc
    _taint(context, page.url or url)
    log.info("browser_action", action=action, url=page.url, elements=len(page.elements))
    return page.as_dict()


@tool(
    name="browser.open",
    tier=Tier.REVERSIBLE,
    description=(
        "Open a web page and read it. Returns the page text plus the "
        "interactive elements on it, each with a 'ref' you can pass to "
        "browser.click or browser.fill. Use this for anything you need to "
        "look up online."
    ),
    params={"url": {"type": "string", "description": "Full URL, e.g. https://example.com"}},
    required=["url"],
    summarize=lambda args: f"open {args.get('url')} in a browser",
)
def open_page(context: ToolContext, url: str) -> dict[str, Any]:
    """Navigate to a URL and return the distilled page."""
    cleaned = url.strip()
    if not cleaned:
        raise ToolError("url is empty")
    if "://" not in cleaned:
        cleaned = f"https://{cleaned}"
    if not cleaned.startswith(("http://", "https://")):
        raise ToolError(f"only http and https pages can be opened: {url}")
    return _run(context, cleaned, "opening the page", lambda: browsing.session().goto(cleaned))


@tool(
    name="browser.read",
    tier=Tier.READ,
    description=(
        "Re-read the page that is currently open, for example after it has "
        "loaded more content. Returns the same shape as browser.open."
    ),
)
def read_page(context: ToolContext) -> dict[str, Any]:
    """Re-distill the open page."""
    if not browsing.session().running:
        raise ToolError("no page is open - use browser.open first")
    return _run(context, "current page", "reading the page", browsing.session().read)


@tool(
    name="browser.click",
    tier=Tier.REVERSIBLE,
    description=(
        "Click an element on the open page by its 'ref' (from browser.open or "
        "browser.read). Returns the page after the click."
    ),
    params={"ref": {"type": "string", "description": "Element reference, e.g. 'e7'"}},
    required=["ref"],
    summarize=lambda args: f"click element {args.get('ref')} on the open web page",
)
def click(context: ToolContext, ref: str) -> dict[str, Any]:
    """Click a distilled element."""
    if not browsing.session().running:
        raise ToolError("no page is open - use browser.open first")
    return _run(context, "current page", f"clicking {ref}", lambda: browsing.session().click(ref))


@tool(
    name="browser.fill",
    tier=Tier.REVERSIBLE,
    description=(
        "Type text into a field on the open page, identified by its 'ref'. "
        "Set submit=true to press Enter afterwards (for search boxes)."
    ),
    params={
        "ref": {"type": "string", "description": "Element reference, e.g. 'e3'"},
        "text": {"type": "string", "description": "Text to type into the field"},
        "submit": {"type": "boolean", "description": "Press Enter after typing"},
    },
    required=["ref", "text"],
    summarize=lambda args: f"type {args.get('text')!r} into element {args.get('ref')}",
)
def fill(context: ToolContext, ref: str, text: str, submit: bool = False) -> dict[str, Any]:
    """Fill a form field, optionally submitting it."""
    if not browsing.session().running:
        raise ToolError("no page is open - use browser.open first")
    return _run(
        context,
        "current page",
        f"filling {ref}",
        lambda: browsing.session().fill(ref, text, submit=submit),
    )


@tool(
    name="browser.download",
    tier=Tier.CONFIRM_ALWAYS,
    description=(
        "Download a file from a URL into one of the permitted folders. Always "
        "asks the user first, because it writes a file from the internet."
    ),
    params={
        "url": {"type": "string", "description": "Direct URL of the file"},
        "path": {
            "type": "string",
            "description": "Destination inside a permitted folder, e.g. 'Downloads/report.pdf'",
        },
    },
    required=["url", "path"],
    summarize=lambda args: f"download {args.get('url')} to {args.get('path')}",
)
def download(context: ToolContext, url: str, path: str) -> dict[str, Any]:
    """Save a URL to an allowed path using the browser's session."""
    destination = resolve_allowed(path, configured_roots(context.settings), must_exist=False)
    if destination.exists():
        raise ToolError(f"{destination} already exists - choose another name")
    if not destination.parent.exists():
        raise ToolError(f"{destination.parent} does not exist")
    try:
        written = browsing.session().download(url, str(destination))
    except browsing.BrowserUnavailableError as exc:
        raise ToolError(str(exc)) from exc
    except Exception as exc:
        raise ToolError(f"download failed: {exc}") from exc
    # The bytes came from the internet: the turn is tainted even though the
    # content was never shown to the model.
    _taint(context, url)
    log.info("browser_download", url=url, path=str(destination), bytes=written)
    return {"downloaded": str(destination), "bytes": written, "source": url}


@tool(
    name="browser.use_my_profile",
    tier=Tier.CONFIRM_ALWAYS,
    description=(
        "Switch to the user's OWN Chrome window, with their logins and "
        "cookies, instead of the clean private browser. Needed only for sites "
        "they are signed into. Requires Chrome to have been started with "
        "--remote-debugging-port=9222. Always asks permission."
    ),
    params={
        "cdp_url": {
            "type": "string",
            "description": "Debugging endpoint (default http://127.0.0.1:9222)",
        }
    },
    summarize=lambda _args: (
        "browse using YOUR Chrome profile - the assistant will be able to act "
        "as you on every site you are signed into"
    ),
)
def use_my_profile(context: ToolContext, cdp_url: str = browsing.DEFAULT_CDP_URL) -> dict[str, Any]:
    """Attach to the user's running Chrome (T2: their logged-in sessions).

    Confirmation is unconditional and the summary says plainly what is being
    granted, because "browse in my profile" and "act as me on every site I am
    signed into" are the same sentence and only one of them is obvious.
    """
    try:
        browsing.attach(cdp_url)
    except browsing.BrowserUnavailableError as exc:
        raise ToolError(str(exc)) from exc
    log.warning("browser_profile_attached", session=context.turn.session_id)
    return {
        "attached": True,
        "cdp_url": cdp_url,
        "note": "Now using your own browser profile. browser.close returns to a clean one.",
    }


@tool(
    name="browser.close",
    tier=Tier.REVERSIBLE,
    description="Close the browser and free its memory. Reopening is automatic.",
    summarize=lambda _args: "close the browser",
)
def close(context: ToolContext) -> dict[str, Any]:
    """Shut the shared browser session down."""
    was_running = browsing.session().running
    browsing.shutdown()
    return {"closed": was_running}
