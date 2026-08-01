"""App and process tools: launch, list, inspect.

Launching is REVERSIBLE (closing a window undoes it). Listing processes is
READ. Killing a process is CONFIRM_ALWAYS - it can destroy unsaved work.

Rung 1 of the desktop ladder (native APIs): ``os.startfile`` uses the same
file associations Explorer does, so "open this PDF" works without any UI
automation. UIA-based control of app *interiors* is M7.
"""

from __future__ import annotations

import os
import subprocess
import webbrowser
from typing import Any

import psutil

from myagent.security.tiers import Tier
from myagent.tools.applookup import find_application, list_known_applications
from myagent.tools.paths import configured_roots, resolve_allowed
from myagent.tools.registry import ToolContext, ToolError, tool

MAX_PROCESSES = 60


# Windows process-creation flags (subprocess re-exports only some of these).
DETACHED_PROCESS = 0x00000008
CREATE_BREAKAWAY_FROM_JOB = 0x01000000
CREATE_NEW_PROCESS_GROUP = 0x00000200


def _launch(target: str, direct: bool) -> None:
    """Start something so that it OUTLIVES MyAgent.

    The launcher puts every MyAgent process in a Windows job object with
    KILL_ON_JOB_CLOSE, and a job member's children join that job - so an app
    opened for the user was killed the moment MyAgent stopped. Neither
    ``subprocess.Popen`` nor ``os.startfile`` escapes on its own (both create
    the process from *this* process; measured, both died with the job).

    Two ways out, one per kind of target:

    - ``direct``: an executable we resolved to a real path. Launch it with
      CREATE_BREAKAWAY_FROM_JOB, which the job explicitly permits, so failures
      still surface as an exception we can report honestly.
    - otherwise: shortcuts, folders, files, and protocol URIs need the shell's
      association logic. Handing them to ``explorer.exe`` makes the *already
      running* Explorer do the launching, so the app is its child, not ours.
      The stub we spawn exits immediately.
    """
    argv = [target] if direct else ["explorer.exe", target]
    flags = CREATE_BREAKAWAY_FROM_JOB | CREATE_NEW_PROCESS_GROUP
    if direct:
        flags |= DETACHED_PROCESS  # no console window inherited from us
    try:
        _spawn(argv, flags)
    except OSError as exc:
        raise ToolError(f"Windows refused to open {target}: {exc}") from exc


def _spawn(argv: list[str], flags: int) -> None:
    """Create the process, retrying without breakaway if it is not allowed.

    A job that forbids breakaway rejects the flag outright (ERROR_ACCESS_DENIED).
    Running inside one is better than not launching at all, so fall back.
    """
    try:
        subprocess.Popen(
            argv,
            creationflags=flags,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
    except OSError:
        subprocess.Popen(
            argv,
            creationflags=flags & ~CREATE_BREAKAWAY_FROM_JOB,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )


@tool(
    name="apps.open",
    tier=Tier.REVERSIBLE,
    description=(
        "Open an installed application by name ('chrome', 'spotify', 'vs code', "
        "'file explorer'), or open a file or folder ('Downloads', "
        "'Documents/notes.txt'). Folder and file paths must be inside the "
        "permitted folders; application names may be anything installed."
    ),
    params={
        "target": {
            "type": "string",
            "description": "Application name, or a file/folder path to open",
        }
    },
    required=["target"],
    summarize=lambda args: f"open {args.get('target')}",
)
def open_target(context: ToolContext, target: str) -> dict[str, Any]:
    """Open an app, a folder, or a file.

    Applications are resolved the way Windows itself does (PATH, App Paths
    registry, Start Menu shortcuts) rather than PATH alone, because GUI apps
    are almost never on PATH. Only *paths* are constrained to the permitted
    roots - launching an installed program is not a filesystem operation.
    """
    if not target.strip():
        raise ToolError("target is empty")

    found = find_application(target)
    if found is not None:
        kind, resolved = found
        _launch(resolved, direct=kind == "exe")
        return {"opened": resolved, "kind": "application"}

    try:
        path = resolve_allowed(target, configured_roots(context.settings), must_exist=True)
    except ToolError as exc:
        known = ", ".join(list_known_applications(limit=25))
        raise ToolError(
            f"could not find an application called '{target}', and it is not an "
            f"openable path either ({exc}). Installed apps include: {known}"
        ) from exc
    _launch(str(path), direct=False)  # folders and documents need associations
    return {"opened": str(path), "kind": "folder" if path.is_dir() else "file"}


@tool(
    name="apps.open_url",
    tier=Tier.REVERSIBLE,
    description=(
        "Open a web page in the default browser. Use this for 'open youtube', "
        "'search for X', or any link the user asks to visit."
    ),
    params={"url": {"type": "string", "description": "Full URL, e.g. https://example.com"}},
    required=["url"],
    summarize=lambda args: f"open {args.get('url')} in the browser",
)
def open_url(context: ToolContext, url: str) -> dict[str, Any]:
    """Open a URL in the user's default browser."""
    cleaned = url.strip()
    if not cleaned:
        raise ToolError("url is empty")
    if "://" not in cleaned:
        cleaned = f"https://{cleaned}"
    if not cleaned.startswith(("http://", "https://")):
        raise ToolError(f"only http and https links can be opened: {url}")
    webbrowser.open(cleaned)
    return {"opened": cleaned, "kind": "url"}


@tool(
    name="apps.list_applications",
    tier=Tier.READ,
    description=(
        "List installed applications that can be opened by name. Returns a "
        "sample, not an exhaustive list - apps.open also finds apps that are "
        "not listed here."
    ),
    params={"limit": {"type": "integer", "description": "How many to return (default 40)"}},
)
def list_applications(context: ToolContext, limit: int = 40) -> dict[str, Any]:
    """Installed application names, from the Start Menu.

    Deliberately capped: this result is resent to the model on every later
    step of the turn, and a few hundred names is enough to blow a free tier's
    tokens-per-minute budget on its own.
    """
    names = list_known_applications(limit=max(1, min(limit, 100)))
    return {"count": len(names), "applications": names}


@tool(
    name="apps.list_processes",
    tier=Tier.READ,
    description=(
        "List the most resource-heavy running processes with CPU and memory "
        "usage. Useful for 'what is slowing my laptop down?'."
    ),
    params={"limit": {"type": "integer", "description": "How many to return (default 15)"}},
)
def list_processes(context: ToolContext, limit: int = 15) -> dict[str, Any]:
    """Top processes by memory use."""
    count = max(1, min(limit, MAX_PROCESSES))
    entries: list[dict[str, Any]] = []
    for process in psutil.process_iter(["pid", "name", "memory_info", "cpu_percent"]):
        try:
            info = process.info
            memory = info.get("memory_info")
            entries.append(
                {
                    "pid": info["pid"],
                    "name": info.get("name") or "?",
                    "memory_mb": round(memory.rss / 1e6, 1) if memory else None,
                    "cpu_percent": info.get("cpu_percent"),
                }
            )
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    entries.sort(key=lambda entry: entry["memory_mb"] or 0, reverse=True)
    return {"count": min(count, len(entries)), "processes": entries[:count]}


GPU_COUNTER_TIMEOUT_S = 12


def gpu_usage() -> dict[str, Any] | None:
    """Current GPU load, or None when this machine cannot report it.

    psutil has no GPU support, so this reads Windows' own performance
    counters - the same numbers Task Manager shows. Utilization is split
    across many per-engine instances (3D, Copy, VideoDecode...), and the
    meaningful figure is their sum, capped at 100%.

    Deliberately not nvidia-smi: that only works on NVIDIA hardware, while
    the counter exists for every GPU including integrated ones.
    """
    script = (
        "$ErrorActionPreference='Stop';"
        "$s=(Get-Counter '\\GPU Engine(*)\\Utilization Percentage').CounterSamples"
        "|Measure-Object -Property CookedValue -Sum;"
        "[math]::Round([math]::Min($s.Sum,100),1)"
    )
    try:
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            timeout=GPU_COUNTER_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    try:
        percent = float(completed.stdout.strip())
    except ValueError:
        return None
    return {"percent": percent}


@tool(
    name="apps.system_status",
    tier=Tier.READ,
    description=(
        "Report CPU, memory, disk, battery - and, with include_gpu, GPU load - "
        "for this machine. This is the tool for any question about how loaded "
        "or how full this computer is: answer from it rather than telling the "
        "user to open Task Manager."
    ),
    params={
        "include_gpu": {
            "type": "boolean",
            "description": (
                "Also read GPU utilization (adds ~2s; pass true when asked about the GPU)"
            ),
        }
    },
)
def system_status(context: ToolContext, include_gpu: bool = False) -> dict[str, Any]:
    """Hardware snapshot (FR-DESK-06).

    GPU is opt-in because its counter costs ~2s while everything else here is
    instant, and most status questions are not about the GPU.
    """
    memory = psutil.virtual_memory()
    disk = psutil.disk_usage(os.environ.get("SYSTEMDRIVE", "C:") + "\\")
    battery = psutil.sensors_battery()
    return {
        "cpu_percent": psutil.cpu_percent(interval=0.3),
        "cpu_count": psutil.cpu_count(logical=True),
        "gpu": gpu_usage() if include_gpu else None,
        "memory": {
            "total_gb": round(memory.total / 1e9, 1),
            "used_percent": memory.percent,
        },
        "disk": {
            "total_gb": round(disk.total / 1e9, 1),
            "free_gb": round(disk.free / 1e9, 1),
            "used_percent": disk.percent,
        },
        "battery": None
        if battery is None
        else {"percent": round(battery.percent), "plugged_in": battery.power_plugged},
    }


@tool(
    name="apps.close",
    tier=Tier.CONFIRM_ALWAYS,
    description=(
        "Terminate a running process by pid. This can lose unsaved work, so "
        "it always requires confirmation."
    ),
    params={"pid": {"type": "integer", "description": "Process id to terminate"}},
    required=["pid"],
    summarize=lambda args: f"terminate process {args.get('pid')} (unsaved work may be lost)",
)
def close(context: ToolContext, pid: int) -> dict[str, Any]:
    """Terminate a process politely; report whether it exited."""
    if pid == os.getpid():
        raise ToolError("refusing to terminate the assistant's own process")
    try:
        process = psutil.Process(pid)
        name = process.name()
        process.terminate()
        process.wait(timeout=10)
    except psutil.NoSuchProcess as exc:
        raise ToolError(f"no such process: {pid}") from exc
    except psutil.AccessDenied as exc:
        raise ToolError(f"access denied terminating pid {pid}") from exc
    except psutil.TimeoutExpired:
        return {"pid": pid, "terminated": False, "note": "process did not exit within 10s"}
    return {"pid": pid, "name": name, "terminated": True}
