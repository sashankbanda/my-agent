"""One-command start: ``uv run python -m myagent.start``.

Starts the kernel, waits until it is actually serving, then starts the voice
satellite and the overlay orb, and opens the HUD in the browser. Ctrl+C (or
closing this window) stops everything it started.

Why this exists: running three processes by hand and reading their logs was
the main friction in daily use. Each part is still independently runnable -
this only removes the choreography.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
import webbrowser
from dataclasses import dataclass, field

import httpx

from myagent.config import load_settings
from myagent.jobs import ProcessGroup, kill_tree
from myagent.logging import configure_logging, get_logger

log = get_logger(__name__)

BOOT_TIMEOUT_S = 45.0
SHUTDOWN_GRACE_S = 5.0


@dataclass
class Child:
    """A process this launcher owns."""

    name: str
    argv: list[str]
    group: ProcessGroup | None = None
    process: subprocess.Popen[bytes] | None = None

    def start(self) -> None:
        self.process = subprocess.Popen(self.argv)
        if self.group is not None:
            self.group.add(self.process.pid)
        log.info("started", component=self.name, pid=self.process.pid)

    @property
    def alive(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def stop(self) -> None:
        """Stop this child and anything it spawned.

        A venv python.exe is a shim that re-executes the real interpreter, so
        terminating only our direct child can leave the actual process behind -
        hence the tree kill.
        """
        if self.process is None or self.process.poll() is not None:
            return
        kill_tree(self.process.pid)
        self.process.terminate()
        try:
            self.process.wait(timeout=SHUTDOWN_GRACE_S)
        except subprocess.TimeoutExpired:
            self.process.kill()
        log.info("stopped", component=self.name)


@dataclass
class Supervisor:
    """Starts children, restarts the ones that crash, stops them all on exit.

    Children join a Windows job object, so they are terminated by the OS even
    if this launcher is force-killed rather than interrupted.
    """

    children: list[Child] = field(default_factory=list)
    group: ProcessGroup = field(default_factory=ProcessGroup)

    def add(self, name: str, module: str, *extra: str) -> Child:
        child = Child(name=name, argv=[sys.executable, "-m", module, *extra], group=self.group)
        self.children.append(child)
        return child

    def stop_all(self) -> None:
        for child in reversed(self.children):
            child.stop()
        self.group.close()  # backstop: the OS kills anything still standing

    def watch(self) -> None:
        """Keep children alive until interrupted.

        The kernel is essential: if it dies, everything stops. Voice and the
        overlay are conveniences, so they are restarted quietly.
        """
        try:
            while True:
                time.sleep(1.0)
                for child in self.children:
                    if child.alive:
                        continue
                    if child.name == "kernel":
                        log.error("kernel_exited", action="shutting down")
                        return
                    log.warning("restarting", component=child.name)
                    child.start()
        except KeyboardInterrupt:
            print("\nstopping MyAgent...")


def wait_for_kernel(base_url: str, timeout: float = BOOT_TIMEOUT_S) -> bool:
    """Poll /health until the kernel answers (models load on first import)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            response = httpx.get(f"{base_url}/health", timeout=2)
            if response.status_code == 200:
                return True
        except httpx.HTTPError:
            pass
        time.sleep(0.4)
    return False


def port_in_use(host: str, port: int) -> bool:
    """True if something already answers on the kernel's port."""
    import socket

    with socket.socket() as probe:
        probe.settimeout(0.5)
        return probe.connect_ex((host, port)) == 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-voice", action="store_true", help="text only")
    parser.add_argument("--no-overlay", action="store_true", help="skip the orb")
    parser.add_argument("--no-browser", action="store_true", help="do not open the HUD")
    parser.add_argument(
        "--corner",
        default="bottom-right",
        choices=["bottom-right", "bottom-left", "top-right", "top-left"],
    )
    args = parser.parse_args()

    settings = load_settings()
    configure_logging(settings.logging)
    host, port = settings.server.host, settings.server.port
    base_url = f"http://{host}:{port}"

    if port_in_use(host, port):
        print(f"Something is already listening on {host}:{port}.")
        print("That is probably an older MyAgent kernel. Stop it first:")
        print(
            "  $pid = (Get-NetTCPConnection -LocalPort "
            f"{port} -State Listen).OwningProcess; Stop-Process -Id $pid -Force"
        )
        raise SystemExit(1)

    supervisor = Supervisor()
    kernel = supervisor.add("kernel", "myagent")
    kernel.start()

    print("starting MyAgent...")
    if not wait_for_kernel(base_url):
        print("the kernel did not come up in time; see its output above")
        supervisor.stop_all()
        raise SystemExit(1)
    print(f"  kernel   {base_url}")

    if not args.no_voice:
        supervisor.add("voice", "myagent.voice").start()
        print("  voice    starting (models load on first run)")
    if not args.no_overlay:
        supervisor.add("overlay", "myagent.overlay", "--corner", args.corner).start()
        print("  overlay  orb on screen (drag it; click opens the HUD)")
    if not args.no_browser:
        webbrowser.open(base_url)
        print("  HUD      opened in your browser")

    if supervisor.group.active:
        print("\nready. press Ctrl+C here to stop everything.\n")
    else:
        print("\nready. press Ctrl+C to stop (job objects unavailable: check for")
        print("leftover processes if this window is force-closed).\n")
    try:
        supervisor.watch()
    finally:
        supervisor.stop_all()


if __name__ == "__main__":
    main()
