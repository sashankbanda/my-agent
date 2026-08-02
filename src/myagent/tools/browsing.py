"""The browser session: one Chromium, owned by one thread.

Playwright's sync API is *thread-affine* - its objects belong to the thread
that created them - while tools run on whatever worker thread asyncio hands
out. Rather than paying browser startup on every call (seconds) or risking
cross-thread use (crashes), the browser lives on a dedicated thread and
receives work as closures through a queue.

Kept separate from ``tools/browser.py`` so the tool layer stays declarative
and this concurrency detail is testable on its own.

The page is *distilled*, never screenshotted: a structured list of text and
interactive elements is smaller, cheaper, and more reliable for a model to act
on than pixels, and it gives every clickable thing a stable reference.
"""

from __future__ import annotations

import queue
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from myagent.logging import get_logger

log = get_logger(__name__)

DEFAULT_TIMEOUT_MS = 20_000
COMMAND_TIMEOUT_S = 90.0
MAX_TEXT_CHARS = 6_000  # a page's text is resent on every later step; keep it small
MAX_ELEMENTS = 60
STARTUP_TIMEOUT_S = 60.0


class BrowserUnavailableError(RuntimeError):
    """Playwright or its Chromium download is missing.

    Raised with the exact command to fix it: a browser that silently does
    nothing is worse than one that says it is not installed.
    """


@dataclass
class Element:
    """One interactive thing on the page, addressable by ``ref``."""

    ref: str
    role: str
    name: str
    value: str = ""
    href: str = ""  # absolute target for links, so they can be followed directly

    def as_dict(self) -> dict[str, str]:
        """Compact form for the model (empty fields omitted)."""
        entry = {"ref": self.ref, "role": self.role, "name": self.name}
        if self.value:
            entry["value"] = self.value
        if self.href:
            entry["href"] = self.href
        return entry


@dataclass
class Distilled:
    """A page reduced to what a model can reason and act on."""

    url: str
    title: str
    text: str
    elements: list[Element] = field(default_factory=list)
    truncated: bool = False

    def as_dict(self) -> dict[str, Any]:
        """Tool-result form."""
        return {
            "url": self.url,
            "title": self.title,
            "text": self.text,
            "elements": [element.as_dict() for element in self.elements],
            "truncated": self.truncated,
        }


# Extracts text and interactive elements in one pass. Runs in the page so a
# single round trip replaces dozens of Playwright calls, and marks each
# element with a data attribute so a later click can find the same node even
# if the DOM has shifted underneath us.
_DISTILL_JS = """
(maxElements) => {
  const isVisible = (el) => {
    const style = window.getComputedStyle(el);
    if (style.visibility === 'hidden' || style.display === 'none') return false;
    if (parseFloat(style.opacity || '1') === 0) return false;
    const box = el.getBoundingClientRect();
    return box.width > 0 && box.height > 0;
  };
  const label = (el) => {
    const text = (
      el.getAttribute('aria-label') ||
      el.getAttribute('title') ||
      el.getAttribute('placeholder') ||
      el.innerText ||
      el.value ||
      el.getAttribute('name') ||
      ''
    );
    return text.replace(/\\s+/g, ' ').trim().slice(0, 120);
  };
  const roleOf = (el) => {
    const tag = el.tagName.toLowerCase();
    if (tag === 'a') return 'link';
    if (tag === 'button') return 'button';
    if (tag === 'select') return 'select';
    if (tag === 'textarea') return 'textarea';
    if (tag === 'input') return 'input:' + (el.type || 'text');
    return el.getAttribute('role') || tag;
  };

  document.querySelectorAll('[data-myagent-ref]').forEach(
    (el) => el.removeAttribute('data-myagent-ref'));

  const selector = 'a[href], button, input, select, textarea, [role=button], [onclick]';
  const elements = [];
  let index = 0;
  for (const el of document.querySelectorAll(selector)) {
    if (!isVisible(el)) continue;
    const name = label(el);
    if (!name && roleOf(el).startsWith('input') === false) continue;
    index += 1;
    const ref = 'e' + index;
    el.setAttribute('data-myagent-ref', ref);
    elements.push({
      ref: ref,
      role: roleOf(el),
      name: name,
      value: (el.value || '').toString().slice(0, 80),
      href: (el.tagName.toLowerCase() === 'a' ? (el.href || '') : ''),
    });
    if (elements.length >= maxElements) break;
  }

  const clone = document.body ? document.body.cloneNode(true) : null;
  if (clone) {
    clone.querySelectorAll('script, style, noscript, svg, nav, footer').forEach(
      (el) => el.remove());
  }
  const text = (clone ? clone.innerText || '' : '').replace(/\\n{3,}/g, '\\n\\n').trim();
  return { url: location.href, title: document.title, text: text, elements: elements };
}
"""


class BrowserSession:
    """A long-lived Chromium, driven from any thread.

    Commands are closures taking the Playwright ``Page``; they are executed on
    the browser's own thread and their result (or exception) handed back.
    """

    def __init__(self, headless: bool = True, cdp_url: str | None = None) -> None:
        """``cdp_url`` attaches to an already-running browser instead of launching one."""
        self._headless = headless
        self._cdp_url = cdp_url
        self._commands: queue.Queue[tuple[Callable[[Any], Any], queue.Queue[Any]]] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._startup_error: BaseException | None = None
        self._stopping = threading.Event()
        self._lock = threading.Lock()

    # -- lifecycle ----------------------------------------------------------

    def _ensure_started(self) -> None:
        """Launch the browser thread on first use, once, from any caller."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._ready.clear()
            self._startup_error = None
            self._stopping.clear()
            self._thread = threading.Thread(target=self._run, name="browser", daemon=True)
            self._thread.start()
        if not self._ready.wait(STARTUP_TIMEOUT_S):
            raise BrowserUnavailableError("the browser did not start within 60s")
        if self._startup_error is not None:
            raise BrowserUnavailableError(str(self._startup_error))

    def _run(self) -> None:
        """The browser thread: own Playwright, then serve commands until stopped."""
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self._startup_error = BrowserUnavailableError(
                "Playwright is not installed. Run: uv sync --group web"
            )
            self._ready.set()
            return

        try:
            with sync_playwright() as playwright:
                try:
                    browser, context, attached = self._connect(playwright)
                except BrowserUnavailableError as exc:
                    self._startup_error = exc
                    self._ready.set()
                    return
                context.set_default_timeout(DEFAULT_TIMEOUT_MS)
                # Attached sessions reuse the window the user already has open,
                # so their tab is not stolen and their history stays coherent.
                pages = list(context.pages)
                page = pages[0] if attached and pages else context.new_page()
                log.info("browser_started", headless=self._headless, attached=attached)
                self._ready.set()
                self._serve(page)
                if not attached:
                    browser.close()  # never close a browser the user owns
        except Exception as exc:
            log.exception("browser_thread_failed")
            self._startup_error = exc
            self._ready.set()
        finally:
            log.info("browser_stopped")

    def _connect(self, playwright: Any) -> tuple[Any, Any, bool]:
        """Launch a clean browser, or attach to the user's running one.

        Returns ``(browser, context, attached)``. Attaching is the risky path -
        it inherits every logged-in session in that profile - so it never
        happens implicitly: only a CONFIRM_ALWAYS tool sets ``cdp_url``.
        """
        if self._cdp_url:
            try:
                browser = playwright.chromium.connect_over_cdp(self._cdp_url)
            except Exception as exc:
                raise BrowserUnavailableError(
                    f"could not attach to your browser at {self._cdp_url} ({exc}). "
                    "Start Chrome with remote debugging first: "
                    "chrome.exe --remote-debugging-port=9222"
                ) from exc
            contexts = list(browser.contexts)
            if not contexts:
                raise BrowserUnavailableError("attached to the browser but it has no open window")
            return browser, contexts[0], True

        try:
            browser = playwright.chromium.launch(headless=self._headless)
        except Exception as exc:
            raise BrowserUnavailableError(
                f"Chromium could not start ({exc}). Run: uv run playwright install chromium"
            ) from exc
        return browser, browser.new_context(), False

    def _serve(self, page: Any) -> None:
        while not self._stopping.is_set():
            try:
                command, reply = self._commands.get(timeout=0.5)
            except queue.Empty:
                continue
            if command is _STOP:
                reply.put((None, None))
                return
            try:
                reply.put((command(page), None))
            except Exception as exc:
                reply.put((None, exc))

    def stop(self) -> None:
        """Close the browser and its thread; safe if never started."""
        with self._lock:
            thread = self._thread
            if thread is None or not thread.is_alive():
                self._thread = None
                return
        self._stopping.set()
        reply: queue.Queue[Any] = queue.Queue()
        self._commands.put((_STOP, reply))
        thread.join(timeout=15)
        with self._lock:
            self._thread = None

    @property
    def running(self) -> bool:
        """True while the browser thread is alive."""
        return self._thread is not None and self._thread.is_alive()

    @property
    def cdp_url(self) -> str | None:
        """The debugging endpoint this session attached to, if any."""
        return self._cdp_url

    # -- command dispatch ---------------------------------------------------

    def run(self, command: Callable[[Any], Any]) -> Any:
        """Execute ``command(page)`` on the browser thread and return its result."""
        self._ensure_started()
        reply: queue.Queue[Any] = queue.Queue()
        self._commands.put((command, reply))
        try:
            result, error = reply.get(timeout=COMMAND_TIMEOUT_S)
        except queue.Empty as exc:
            raise TimeoutError("the browser did not respond within 90s") from exc
        if error is not None:
            raise error
        return result

    # -- page operations ----------------------------------------------------

    def goto(self, url: str) -> Distilled:
        """Navigate, wait for the DOM, and return the distilled page."""

        def command(page: Any) -> Distilled:
            page.goto(url, wait_until="domcontentloaded")
            return _distill(page)

        return self.run(command)

    def read(self) -> Distilled:
        """Re-distill whatever page is currently open."""
        return self.run(_distill)

    def click(self, ref: str) -> Distilled:
        """Click a previously distilled element and return the resulting page."""

        def command(page: Any) -> Distilled:
            locator = page.locator(f"[data-myagent-ref='{ref}']")
            if locator.count() == 0:
                raise LookupError(f"no element {ref} on this page - read it again")
            locator.first.click()
            page.wait_for_load_state("domcontentloaded")
            return _distill(page)

        return self.run(command)

    def fill(self, ref: str, text: str, submit: bool = False) -> Distilled:
        """Type into a field, optionally pressing Enter afterwards."""

        def command(page: Any) -> Distilled:
            locator = page.locator(f"[data-myagent-ref='{ref}']")
            if locator.count() == 0:
                raise LookupError(f"no element {ref} on this page - read it again")
            locator.first.fill(text)
            if submit:
                locator.first.press("Enter")
                page.wait_for_load_state("domcontentloaded")
            return _distill(page)

        return self.run(command)

    def download(self, url: str, destination: str) -> int:
        """Save ``url`` to ``destination`` using the browser's session.

        Goes through the browser rather than a bare HTTP client so cookies and
        headers from the current session apply - otherwise "download that
        file" fails on anything behind a login.
        """

        def command(page: Any) -> int:
            response = page.request.get(url)
            if not response.ok:
                raise ConnectionError(f"{url} returned HTTP {response.status}")
            body = response.body()
            with open(destination, "wb") as handle:
                handle.write(body)
            return len(body)

        return self.run(command)


class _Stop:
    """Sentinel command telling the browser thread to shut down."""


_STOP: Any = _Stop()


def _distill(page: Any) -> Distilled:
    """Reduce the live page to text plus addressable interactive elements."""
    raw = page.evaluate(_DISTILL_JS, MAX_ELEMENTS)
    text = raw.get("text") or ""
    truncated = len(text) > MAX_TEXT_CHARS
    return Distilled(
        url=raw.get("url", ""),
        title=raw.get("title", ""),
        text=text[:MAX_TEXT_CHARS],
        elements=[
            Element(
                ref=item["ref"],
                role=item["role"],
                name=item["name"],
                value=item.get("value", ""),
                href=item.get("href", ""),
            )
            for item in raw.get("elements", [])
        ],
        truncated=truncated,
    )


# One browser for the process. Tools share it so a conversation can navigate,
# read, and click across several turns on the same page.
_SESSION: BrowserSession | None = None
_SESSION_LOCK = threading.Lock()

DEFAULT_CDP_URL = "http://127.0.0.1:9222"


def session() -> BrowserSession:
    """The process-wide browser session, created on first use."""
    global _SESSION
    with _SESSION_LOCK:
        if _SESSION is None:
            _SESSION = BrowserSession()
        return _SESSION


def attach(cdp_url: str = DEFAULT_CDP_URL) -> BrowserSession:
    """Replace the shared session with one attached to the user's browser.

    Their logged-in sessions become reachable, which is the point and also the
    danger, so this is only ever reached through a confirmed tool call.
    """
    global _SESSION
    with _SESSION_LOCK:
        if _SESSION is not None:
            _SESSION.stop()
        _SESSION = BrowserSession(headless=False, cdp_url=cdp_url)
    _SESSION.run(lambda page: page.url)  # fail now, with a useful message
    log.warning("browser_attached_to_user_profile", cdp_url=cdp_url)
    return _SESSION


def attached() -> bool:
    """True when the shared session is driving the user's own browser."""
    return _SESSION is not None and _SESSION.cdp_url is not None


def shutdown() -> None:
    """Close the shared session (kernel shutdown, and between tests)."""
    global _SESSION
    with _SESSION_LOCK:
        if _SESSION is not None:
            _SESSION.stop()
            _SESSION = None
