"""Overlay entry point: ``python -m myagent.overlay``.

A borderless, always-on-top orb that mirrors the assistant's state, plus the
last thing said. Click it to open the full HUD; right-click for a menu.

Architecture: the kernel's ``/events`` stream is consumed on a background
thread (websockets + asyncio) that pushes into a queue; tkinter drains the
queue from its own loop with ``after()``. Neither side blocks the other, and
a kernel restart just reconnects.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import queue
import threading
import tkinter as tk
import webbrowser
from typing import Any

import httpx
import websockets

from myagent.config import load_settings
from myagent.logging import configure_logging, get_logger

log = get_logger(__name__)

RECONNECT_DELAY_S = 2.0
POLL_MS = 100
TOPMOST_REASSERT_MS = 2000  # other apps steal always-on-top; take it back
PLATE_COLOUR = "#12141c"

# state -> (fill, outline, caption)
STATE_STYLE: dict[str, tuple[str, str, str]] = {
    "offline": ("#1a1d26", "#2b3040", "voice off"),
    "idle": ("#22375c", "#35507f", "ready"),
    "waiting": ("#22375c", "#35507f", "listening"),
    "listening": ("#1f5a41", "#46c48b", "hearing you"),
    "thinking": ("#5c4a22", "#e6b054", "thinking"),
    "speaking": ("#2f4bb0", "#6d8bff", "speaking"),
    "down": ("#3a1d1d", "#e06a6a", "kernel down"),
}


class EventClient:
    """Background reader of the kernel's live event stream."""

    def __init__(self, base_url: str, sink: queue.Queue[dict[str, Any]]) -> None:
        self._ws_url = base_url.replace("http://", "ws://").replace("https://", "wss://")
        self._sink = sink
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="overlay-events", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        asyncio.run(self._loop())

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                async with websockets.connect(f"{self._ws_url}/events") as socket:
                    self._sink.put({"type": "_connected"})
                    async for raw in socket:
                        if self._stop.is_set():
                            return
                        self._sink.put(json.loads(raw))
            except (OSError, websockets.WebSocketException):
                self._sink.put({"type": "_disconnected"})
                await asyncio.sleep(RECONNECT_DELAY_S)


class Overlay:
    """The orb window."""

    def __init__(self, base_url: str, corner: str) -> None:
        self.base_url = base_url
        self.events: queue.Queue[dict[str, Any]] = queue.Queue()
        self.client = EventClient(base_url, self.events)
        self.state = "down"
        self.caption_text = "connecting…"

        self.root = tk.Tk()
        self.root.title("MyAgent")
        self.root.overrideredirect(True)  # no title bar
        self.root.configure(bg=PLATE_COLOUR)
        # Slight translucency reads as an overlay without risking invisibility:
        # -transparentcolor on a borderless window rendered the whole orb
        # unseeable on this machine, so the plate is drawn opaque instead.
        with contextlib.suppress(tk.TclError):
            self.root.attributes("-alpha", 0.94)
        with contextlib.suppress(tk.TclError):  # keep it out of alt-tab
            self.root.attributes("-toolwindow", True)

        self.width, self.height = 210, 78
        self._place(corner)

        self.canvas = tk.Canvas(
            self.root,
            width=self.width,
            height=self.height,
            bg=PLATE_COLOUR,
            highlightthickness=0,
        )
        self.canvas.pack()

        # Rounded plate behind the orb so text stays readable on any wallpaper.
        self._plate = self._rounded_rect(2, 2, self.width - 2, self.height - 2, 16, PLATE_COLOUR)
        self._orb = self.canvas.create_oval(
            16, 19, 56, 59, fill="#22375c", outline="#35507f", width=2
        )
        self._caption = self.canvas.create_text(
            70, 30, anchor="w", fill="#e7e9f0", font=("Segoe UI", 10, "bold"), text="…"
        )
        self._detail = self.canvas.create_text(
            70, 50, anchor="w", fill="#8d94a6", font=("Segoe UI", 8), text=""
        )

        self._bind_interactions()
        self.client.start()
        self.root.after(POLL_MS, self._drain)
        self._keep_on_top()

    # -- window helpers -----------------------------------------------------

    def _place(self, corner: str) -> None:
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        margin = 24
        positions = {
            "bottom-right": (screen_w - self.width - margin, screen_h - self.height - 70),
            "bottom-left": (margin, screen_h - self.height - 70),
            "top-right": (screen_w - self.width - margin, margin),
            "top-left": (margin, margin),
        }
        x, y = positions.get(corner, positions["bottom-right"])
        self.root.geometry(f"{self.width}x{self.height}+{x}+{y}")

    def _keep_on_top(self) -> None:
        """Re-assert always-on-top.

        A single ``-topmost`` at startup loses to apps that later claim the
        same flag (browsers going fullscreen, installers), which is how the
        orb ends up hidden behind a maximized window.
        """
        with contextlib.suppress(tk.TclError):
            self.root.attributes("-topmost", True)
            self.root.lift()
        self.root.after(TOPMOST_REASSERT_MS, self._keep_on_top)

    def _rounded_rect(self, x1: int, y1: int, x2: int, y2: int, radius: int, colour: str) -> int:
        """Canvas has no rounded rectangle; a smoothed polygon is the idiom."""
        points = [
            x1 + radius,
            y1,
            x2 - radius,
            y1,
            x2,
            y1,
            x2,
            y1 + radius,
            x2,
            y2 - radius,
            x2,
            y2,
            x2 - radius,
            y2,
            x1 + radius,
            y2,
            x1,
            y2,
            x1,
            y2 - radius,
            x1,
            y1 + radius,
            x1,
            y1,
        ]
        return self.canvas.create_polygon(points, smooth=True, fill=colour, outline="#262c38")

    def _bind_interactions(self) -> None:
        self.canvas.bind("<Button-1>", self._start_drag)
        self.canvas.bind("<B1-Motion>", self._drag)
        self.canvas.bind("<ButtonRelease-1>", self._end_drag)
        self.canvas.bind("<Double-Button-1>", lambda _event: self.open_hud())
        self.canvas.bind("<Button-3>", self._show_menu)

        self.menu = tk.Menu(self.root, tearoff=0, bg="#14171f", fg="#e7e9f0")
        self.menu.add_command(label="Open HUD", command=self.open_hud)
        self.menu.add_separator()
        self.menu.add_command(label="Emergency stop", command=lambda: self._post("/kill"))
        self.menu.add_command(
            label="Re-enable actions", command=lambda: self._post("/kill/release")
        )
        self.menu.add_separator()
        self.menu.add_command(label="Quit overlay", command=self.quit)

        self._drag_origin: tuple[int, int] | None = None
        self._moved = False

    def _start_drag(self, event: tk.Event) -> None:  # type: ignore[type-arg]
        self._drag_origin = (event.x, event.y)
        self._moved = False

    def _drag(self, event: tk.Event) -> None:  # type: ignore[type-arg]
        if self._drag_origin is None:
            return
        self._moved = True
        x = self.root.winfo_pointerx() - self._drag_origin[0]
        y = self.root.winfo_pointery() - self._drag_origin[1]
        self.root.geometry(f"+{x}+{y}")

    def _end_drag(self, _event: tk.Event) -> None:  # type: ignore[type-arg]
        if not self._moved:  # a click, not a drag
            self.open_hud()
        self._drag_origin = None

    def _show_menu(self, event: tk.Event) -> None:  # type: ignore[type-arg]
        self.menu.tk_popup(event.x_root, event.y_root)

    # -- actions ------------------------------------------------------------

    def open_hud(self) -> None:
        webbrowser.open(self.base_url)

    def _post(self, path: str) -> None:
        with contextlib.suppress(httpx.HTTPError):
            httpx.post(f"{self.base_url}{path}", timeout=5)

    def quit(self) -> None:
        self.client.stop()
        self.root.destroy()

    # -- rendering ----------------------------------------------------------

    def _render(self) -> None:
        fill, outline, caption = STATE_STYLE.get(self.state, STATE_STYLE["idle"])
        self.canvas.itemconfigure(self._orb, fill=fill, outline=outline)
        self.canvas.itemconfigure(self._caption, text=caption)
        self.canvas.itemconfigure(self._detail, text=self.caption_text[:34])

    def _drain(self) -> None:
        """Apply everything the reader thread has queued, then reschedule."""
        while True:
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break
            self._apply(event)
        self._render()
        self.root.after(POLL_MS, self._drain)

    def _apply(self, event: dict[str, Any]) -> None:
        kind = event.get("type")
        data = event.get("data") or {}
        if kind == "_connected":
            self.state = "idle"
            self.caption_text = "connected"
        elif kind == "_disconnected":
            self.state = "down"
            self.caption_text = "kernel not running"
        elif kind == "VoiceState":
            self.state = str(data.get("state", "idle"))
        elif kind == "UserSaid":
            self.caption_text = f"you: {data.get('text', '')}"
        elif kind == "AssistantSaid":
            self.caption_text = str(data.get("text", ""))
        elif kind == "ToolCallRequested":
            self.caption_text = f"using {data.get('tool')}"
        elif kind == "ToolCallCompleted":
            ok = data.get("ok")
            self.caption_text = f"{data.get('tool')} {'done' if ok else 'failed'}"
        elif kind == "KillSwitchEngaged":
            self.caption_text = "EMERGENCY STOP"
        elif kind == "VoiceDisconnected":
            self.state = "offline"

    def run(self) -> None:
        self.root.mainloop()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=None, help="kernel base URL")
    parser.add_argument(
        "--corner",
        default="bottom-right",
        choices=["bottom-right", "bottom-left", "top-right", "top-left"],
        help="where to place the orb",
    )
    args = parser.parse_args()

    settings = load_settings()
    configure_logging(settings.logging)
    base_url = args.url or f"http://{settings.server.host}:{settings.server.port}"
    log.info("overlay_starting", url=base_url)
    Overlay(base_url, args.corner).run()


if __name__ == "__main__":
    main()
