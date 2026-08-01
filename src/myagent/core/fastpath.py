"""Local fast path: handle simple commands without calling a model.

"Open Chrome", "what's my battery", "list my downloads", "hi" - none of these
need a language model. Routing them through one costs tokens from a limited
free tier, adds ~1s of latency, and can fail when a provider rate-limits.

This module matches such requests deterministically and answers from real tool
output, spending **zero tokens**. Design rules:

1. **Conservative**: a pattern must be unambiguous. Anything with conjunctions,
   questions about the result, or extra clauses falls through to the model.
   A wrong fast-path answer is worse than a slow correct one.
2. **Same security**: matched intents still execute through the tool executor,
   so the permission broker, taint tracking, and audit log all apply. The fast
   path chooses *what* to call, never *whether it is allowed*.
3. **Honest replies**: text is formatted from the tool's actual result, never
   from the assumption that it worked.
4. **Fallback**: if the tool errors, the turn is handed to the model, which can
   recover (try another name, ask a question) instead of dead-ending.

The roadmap's "workflow compiler" is the mature version of this idea: the
system gets cheaper and faster the more predictable your habits are.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

GREETINGS = {
    "hi",
    "hello",
    "hey",
    "yo",
    "hiya",
    "good morning",
    "good afternoon",
    "good evening",
    "morning",
    "evening",
}
ACKS = {
    "thanks",
    "thank you",
    "thanks!",
    "ty",
    "ok",
    "okay",
    "cool",
    "nice",
    "got it",
    "great",
    "perfect",
    "sounds good",
    "never mind",
    "nevermind",
    "stop",
    "cancel",
}

# Words that make a request more than a single mechanical action; when any
# appears, defer to the model rather than guessing.
COMPLEXITY_MARKERS = (
    " and ",
    " then ",
    " after ",
    " if ",
    " but ",
    " because ",
    " why ",
    " compare ",
    " summarize ",
    " explain ",
    " instead ",
    " unless ",
)
MAX_FASTPATH_CHARS = 120


@dataclass
class Intent:
    """A recognized request: which tool to run and how to word the reply."""

    name: str
    tool: str | None = None  # None -> answered locally with no tool at all
    args: dict[str, Any] = field(default_factory=dict)
    reply: str = ""  # used when tool is None
    formatter: str = ""  # key into FORMATTERS for tool results


def _looks_like_url(target: str) -> bool:
    """True for things a browser should open rather than the shell."""
    lowered = target.lower()
    if lowered.startswith(("http://", "https://", "www.")):
        return True
    # A dotted token with a plausible TLD and no spaces, e.g. "youtube.com".
    return bool(re.fullmatch(r"[\w-]+(\.[\w-]+)+(/\S*)?", lowered)) and "." in lowered


def _too_complex(text: str) -> bool:
    padded = f" {text} "
    return len(text) > MAX_FASTPATH_CHARS or any(marker in padded for marker in COMPLEXITY_MARKERS)


# Each entry: compiled pattern -> builder(match) -> Intent
_OPEN = re.compile(
    r"^(?:please\s+)?(?:can you\s+)?(?:open|launch|start|run)\s+(?:up\s+)?"
    r"(?:my\s+|the\s+)?(.+?)\s*(?:for me)?[.!]?$",
    re.IGNORECASE,
)
_LIST_DIR = re.compile(
    r"^(?:please\s+)?(?:what(?:'s| is)\s+in|list|show(?:\s+me)?|what(?:'s| is)\s+inside)\s+"
    r"(?:my\s+|the\s+)?(.+?)\s*(?:folder|directory)?[.?!]?$",
    re.IGNORECASE,
)
_STATUS = re.compile(
    r"^(?:please\s+)?(?:what(?:'s| is)\s+(?:my\s+)?|check\s+(?:my\s+)?"
    r"|how(?:'s| is)\s+(?:my\s+)?|show\s+(?:me\s+)?(?:my\s+)?)?"
    r"(battery|cpu|memory|ram|disk|storage|system)\s*"
    r"(?:status|usage|level|percentage|percent|space|left)?[.?!]?$",
    re.IGNORECASE,
)
_PROCESSES = re.compile(
    r"^(?:what(?:'s| is)\s+running|list\s+processes|show\s+processes|"
    r"what(?:'s| is)\s+(?:using|eating)\s+(?:my\s+)?(?:cpu|memory|ram)"
    r"(?:\s+the\s+most)?)[.?!]?$",
    re.IGNORECASE,
)
_APPS = re.compile(
    r"^(?:what\s+(?:apps|applications|programs)\s+(?:can you|do i have)"
    r"(?:\s+open|\s+installed)?|list\s+(?:my\s+)?(?:apps|applications|programs))[.?!]?$",
    re.IGNORECASE,
)
_REMEMBER = re.compile(
    r"^(?:please\s+)?remember\s+(?:that\s+|this[:,]?\s+)?(.+?)[.!]?$", re.IGNORECASE
)
_WHAT_REMEMBERED = re.compile(
    r"^(?:what\s+do\s+you\s+(?:remember|know)\s+about\s+me|"
    r"list\s+(?:my\s+)?(?:facts|memories)|what(?:'s| is)\s+in\s+your\s+memory)[.?!]?$",
    re.IGNORECASE,
)
_TIME = re.compile(
    r"^(?:what(?:'s| is)\s+the\s+time|what\s+time\s+is\s+it|time)[.?!]?$", re.IGNORECASE
)
_DATE = re.compile(
    r"^(?:what(?:'s| is)\s+(?:the\s+)?(?:date|day)(?:\s+today)?|what\s+day\s+is\s+it)[.?!]?$",
    re.IGNORECASE,
)


def match(text: str) -> Intent | None:
    """Recognize a simple request, or return None to use the model."""
    stripped = text.strip()
    if not stripped:
        return None
    lowered = stripped.lower().rstrip("!.?")

    if lowered in GREETINGS:
        return Intent(name="greeting", reply="Hey. What do you need?")
    if lowered in ACKS:
        return Intent(name="ack", reply="Sure.")
    if _too_complex(stripped):
        return None

    if _TIME.match(stripped):
        now = datetime.now().astimezone()
        return Intent(name="time", reply=f"It's {now.strftime('%I:%M %p').lstrip('0')}.")
    if _DATE.match(stripped):
        now = datetime.now().astimezone()
        return Intent(name="date", reply=f"It's {now.strftime('%A, %d %B %Y')}.")

    if found := _REMEMBER.match(stripped):
        fact = found.group(1).strip()
        if fact:
            return Intent(
                name="remember",
                tool="memory.remember",
                args={"content": fact},
                formatter="remember",
            )
    if _WHAT_REMEMBERED.match(stripped):
        return Intent(name="list_facts", tool="memory.list_facts", formatter="facts")
    if _APPS.match(stripped):
        return Intent(name="apps", tool="apps.list_applications", formatter="apps")
    if _PROCESSES.match(stripped):
        return Intent(name="processes", tool="apps.list_processes", formatter="processes")
    if found := _STATUS.match(stripped):
        return Intent(
            name="status",
            tool="apps.system_status",
            formatter="status",
            args={},
        )
    if found := _LIST_DIR.match(stripped):
        target = found.group(1).strip().strip("\"'")
        if target and not _looks_like_url(target):
            return Intent(
                name="list_dir",
                tool="files.list_dir",
                args={"path": target},
                formatter="list_dir",
            )
    if found := _OPEN.match(stripped):
        target = found.group(1).strip().strip("\"'")
        if not target:
            return None
        if _looks_like_url(target):
            return Intent(
                name="open_url",
                tool="apps.open_url",
                args={"url": target},
                formatter="open_url",
            )
        return Intent(name="open", tool="apps.open", args={"target": target}, formatter="open")
    return None


# -- reply formatting: always from the tool's real result ---------------------


def _format_open(result: dict[str, Any], intent: Intent) -> str:
    kind = result.get("kind", "thing")
    if kind == "application":
        return f"Opened {intent.args.get('target')}."
    if kind == "folder":
        return f"Opened the {intent.args.get('target')} folder."
    return f"Opened {result.get('opened')}."


def _format_list_dir(result: dict[str, Any], intent: Intent) -> str:
    entries = result.get("entries", [])
    if not entries:
        return f"{result.get('path')} is empty."
    files = [entry["name"] for entry in entries if entry.get("kind") == "file"]
    folders = [entry["name"] for entry in entries if entry.get("kind") == "dir"]
    parts: list[str] = []
    if folders:
        parts.append(f"{len(folders)} folder{'s' if len(folders) != 1 else ''}")
    if files:
        parts.append(f"{len(files)} file{'s' if len(files) != 1 else ''}")
    shown = ", ".join((folders + files)[:8])
    more = "" if len(entries) <= 8 else f", and {len(entries) - 8} more"
    return f"{' and '.join(parts)}: {shown}{more}."


def _format_status(result: dict[str, Any], _intent: Intent) -> str:
    memory = result.get("memory", {})
    disk = result.get("disk", {})
    battery = result.get("battery")
    pieces = [
        f"CPU {result.get('cpu_percent')}%",
        f"memory {memory.get('used_percent')}% of {memory.get('total_gb')} GB",
        f"disk {disk.get('free_gb')} GB free of {disk.get('total_gb')} GB",
    ]
    if battery:
        plugged = "plugged in" if battery.get("plugged_in") else "on battery"
        pieces.append(f"battery {battery.get('percent')}% ({plugged})")
    return "; ".join(pieces) + "."


def _format_processes(result: dict[str, Any], _intent: Intent) -> str:
    top = result.get("processes", [])[:5]
    if not top:
        return "I couldn't read the process list."
    listed = ", ".join(f"{item['name']} ({item['memory_mb']} MB)" for item in top)
    return f"Heaviest right now: {listed}."


def _format_apps(result: dict[str, Any], _intent: Intent) -> str:
    apps = result.get("applications", [])
    if not apps:
        return "I couldn't find any installed applications to list."
    return f"{result.get('count')} apps I can open, including: {', '.join(apps[:10])}."


def _format_remember(result: dict[str, Any], _intent: Intent) -> str:
    return f"Got it - I'll remember that {result.get('remembered')}."


def _format_facts(result: dict[str, Any], _intent: Intent) -> str:
    facts = result.get("facts", [])
    if not facts:
        return "I haven't stored any facts about you yet."
    listed = "; ".join(fact["content"] for fact in facts[:8])
    count = int(result.get("count", len(facts)))
    noun = "thing" if count == 1 else "things"
    return f"{count} {noun} I remember: {listed}."


def _format_open_url(result: dict[str, Any], _intent: Intent) -> str:
    return f"Opened {result.get('opened')} in your browser."


FORMATTERS = {
    "open": _format_open,
    "open_url": _format_open_url,
    "list_dir": _format_list_dir,
    "status": _format_status,
    "processes": _format_processes,
    "apps": _format_apps,
    "remember": _format_remember,
    "facts": _format_facts,
}


def format_reply(intent: Intent, result: dict[str, Any]) -> str:
    """Turn a tool result into a sentence, or a plain summary if unformatted."""
    formatter = FORMATTERS.get(intent.formatter)
    if formatter is None:
        return str(result)
    return formatter(result, intent)
