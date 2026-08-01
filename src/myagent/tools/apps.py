"""App and process tools: launch, list, inspect.

Launching is REVERSIBLE (closing a window undoes it). Listing processes is
READ. Killing a process is CONFIRM_ALWAYS - it can destroy unsaved work.

Rung 1 of the desktop ladder (native APIs): ``os.startfile`` uses the same
file associations Explorer does, so "open this PDF" works without any UI
automation. UIA-based control of app *interiors* is M7.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from typing import Any

import psutil

from myagent.security.tiers import Tier
from myagent.tools.paths import configured_roots, resolve_allowed
from myagent.tools.registry import ToolContext, ToolError, tool

MAX_PROCESSES = 60


@tool(
    name="apps.open",
    tier=Tier.REVERSIBLE,
    description=(
        "Open an application by name (e.g. 'notepad', 'code') or open a file "
        "or folder with its default program. Files must be inside the "
        "permitted folders."
    ),
    params={
        "target": {
            "type": "string",
            "description": "Program name, or a path to a file/folder to open",
        }
    },
    required=["target"],
    summarize=lambda args: f"open {args.get('target')}",
)
def open_target(context: ToolContext, target: str) -> dict[str, Any]:
    """Launch a program, or open a path with its associated application."""
    if not target.strip():
        raise ToolError("target is empty")

    executable = shutil.which(target)
    if executable is not None:
        # Resolved absolute path, argv form, no shell: nothing to inject into.
        subprocess.Popen(
            [executable],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return {"opened": executable, "kind": "program"}

    # Windows file association (same as double-clicking); path is allowlisted.
    path = resolve_allowed(target, configured_roots(context.settings), must_exist=True)
    os.startfile(path)
    return {"opened": str(path), "kind": "path"}


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


@tool(
    name="apps.system_status",
    tier=Tier.READ,
    description="Report CPU, memory, disk, and battery status for this machine.",
)
def system_status(context: ToolContext) -> dict[str, Any]:
    """Hardware snapshot (FR-DESK-06)."""
    memory = psutil.virtual_memory()
    disk = psutil.disk_usage(os.environ.get("SYSTEMDRIVE", "C:") + "\\")
    battery = psutil.sensors_battery()
    return {
        "cpu_percent": psutil.cpu_percent(interval=0.3),
        "cpu_count": psutil.cpu_count(logical=True),
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
