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
from urllib.parse import quote_plus

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

# Sites people ask for by bare name. Without this, "open youtube" looked for
# an installed program called youtube, failed, and fell through to the model -
# which is a slow, token-costing way to reach the obvious answer.
SITES = {
    "youtube": "https://www.youtube.com",
    "google": "https://www.google.com",
    "gmail": "https://mail.google.com",
    "google drive": "https://drive.google.com",
    "drive": "https://drive.google.com",
    "maps": "https://maps.google.com",
    "google maps": "https://maps.google.com",
    "github": "https://github.com",
    "chatgpt": "https://chat.openai.com",
    "claude": "https://claude.ai",
    "gemini": "https://gemini.google.com",
    "whatsapp": "https://web.whatsapp.com",
    "netflix": "https://www.netflix.com",
    "prime video": "https://www.primevideo.com",
    "hotstar": "https://www.hotstar.com",
    "linkedin": "https://www.linkedin.com",
    "instagram": "https://www.instagram.com",
    "reddit": "https://www.reddit.com",
    "twitter": "https://twitter.com",
    "x": "https://x.com",
    "amazon": "https://www.amazon.in",
    "flipkart": "https://www.flipkart.com",
    "stackoverflow": "https://stackoverflow.com",
    "stack overflow": "https://stackoverflow.com",
    "wikipedia": "https://www.wikipedia.org",
}


@dataclass
class Intent:
    """A recognized request: which tool to run and how to word the reply."""

    name: str
    tool: str | None = None  # None -> answered locally with no tool at all
    args: dict[str, Any] = field(default_factory=dict)
    reply: str = ""  # used when tool is None
    formatter: str = ""  # key into FORMATTERS for tool results
    subject: str = ""  # what was actually asked about, so the reply answers *that*


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


# "open" and "list" start plenty of sentences that have nothing to do with
# apps or folders - "open up about yourself", "list three ideas for dinner".
# Those used to run a doomed tool call (which also scanned the Start Menu to
# build its error message) before falling back to the model. A real app or
# folder name is short and does not start with a pronoun or article.
MAX_TARGET_WORDS = 3
_NOT_TARGET_WORDS = (
    "me you your yourself my mine that this it us them again over up about "
    "how why what when who of the a an and or for to be do is are some any "
    "something anything everything"
)
_NOT_A_TARGET = frozenset(_NOT_TARGET_WORDS.split())


def _plausible_target(text: str) -> bool:
    """True when this could name an application, file, or folder."""
    words = text.split()
    if not words or len(words) > MAX_TARGET_WORDS:
        return False
    return words[0].lower() not in _NOT_A_TARGET


# Speech-to-text drops apostrophes and sometimes emits the curly U+2019 one,
# so every contraction here tolerates all three spellings of "what's".
# Missing this is why spoken questions fell through to the model.
_APOS = "['\N{RIGHT SINGLE QUOTATION MARK}]"
_WHATS = rf"(?:what(?:{_APOS}?s| is)|hows|how(?:{_APOS}s| is))"

# Each entry: compiled pattern -> builder(match) -> Intent
_OPEN = re.compile(
    r"^(?:please\s+)?(?:can you\s+)?(?:open|launch|start|run)\s+(?:up\s+)?"
    r"(?:my\s+|the\s+)?(.+?)\s*(?:for me)?[.!]?$",
    re.IGNORECASE,
)
_LIST_DIR = re.compile(
    rf"^(?:please\s+)?(?:{_WHATS}\s+in|list|show(?:\s+me)?|{_WHATS}\s+inside)\s+"
    r"(?:my\s+|the\s+)?(.+?)\s*(?:folder|directory)?[.?!]?$",
    re.IGNORECASE,
)
_STATUS = re.compile(
    rf"^(?:please\s+)?(?:how\s+much\s+|how\s+many\s+|{_WHATS}\s+)?(?:my\s+|the\s+)?"
    r"(?:current\s+)?(?:check\s+)?(?:my\s+|the\s+)?"
    r"(battery|cpu|gpu|memory|ram|disk|storage|system)\s*"
    # Any run of measurement words: "gpu usage percentage", "disk space left".
    # Measurement words plus the filler that surrounds them in speech, so
    # "disk space is left" and "memory do i have" both land here.
    r"(?:(?:status|usage|level|percentage|percent|space|left|load|utilization|"
    r"utilisation|remaining|free|available|at|is|are|do|i|have|has|got|there)\s*)*[.?!]?$",
    re.IGNORECASE,
)
_PROCESSES = re.compile(
    rf"^(?:{_WHATS}\s+running|list\s+processes|show\s+processes|"
    # "what is using the most memory right now", "which app is eating my cpu"
    rf"(?:{_WHATS}|which\s+(?:app|program|process))\s+(?:is\s+)?(?:using|eating|taking)"
    r"\s+(?:up\s+)?(?:the\s+most\s+|most\s+)?(?:my\s+)?(?:cpu|memory|ram)"
    r"(?:\s+the\s+most)?(?:\s+right\s+now)?)[.?!]?$",
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
    rf"list\s+(?:my\s+)?(?:facts|memories)|{_WHATS}\s+in\s+your\s+memory)[.?!]?$",
    re.IGNORECASE,
)
# Only unambiguous web searches: a bare "search for X" could mean the
# filesystem, so it is left to the model.
_WEB_SEARCH = re.compile(
    r"^(?:please\s+)?(?:can you\s+)?(?:google|search\s+(?:the\s+web|google|online)\s+for)"
    r"\s+(.+?)[.?!]?$",
    re.IGNORECASE,
)
_YOUTUBE_SEARCH = re.compile(
    r"^(?:please\s+)?(?:can you\s+)?(?:search\s+youtube\s+for|play|find)"
    r"\s+(.+?)\s+on\s+youtube[.?!]?$",
    re.IGNORECASE,
)
# Trailing "now" / "right now" / "currently" is how people actually ask, and
# missing it sent the question to a model that then invented a time.
_NOW = r"(?:\s+(?:right\s+)?now|\s+currently|\s+please)*"
_TIME = re.compile(
    rf"^(?:{_WHATS}\s+the\s+time|what\s+time\s+is\s+it|time){_NOW}[.?!]?$",
    re.IGNORECASE,
)
_DATE = re.compile(
    rf"^(?:{_WHATS}\s+(?:the\s+)?(?:date|day)(?:\s+today)?|what\s+day\s+is\s+it|"
    rf"what\s+is\s+today(?:{_APOS}s)?(?:\s+date)?){_NOW}[.?!]?$",
    re.IGNORECASE,
)


# The wake word often lands inside the transcript ("Hey Javis, what's the
# time"), and Whisper spells it however it heard it. Stripping the prefix is
# what makes "hey jarvis open chrome" the same request as "open chrome".
_WAKE_PREFIX = re.compile(
    r"^(?:hey|hi|hello|ok|okay)?[\s,]*"
    r"(?:jarvis|javis|jervis|jarviss|jaravis|alexa|mycroft|my croft)\b[\s,.!?]*",
    re.IGNORECASE,
)


def strip_wake_word(text: str) -> str:
    """Remove a leading wake word, leaving the actual request."""
    return _WAKE_PREFIX.sub("", text.strip(), count=1).strip()


def match(text: str) -> Intent | None:
    """Recognize a simple request, or return None to use the model."""
    stripped = text.strip()
    if not stripped:
        return None
    without_wake = strip_wake_word(stripped)
    if not without_wake:
        return Intent(name="greeting", reply="Hey. What do you need?")
    stripped = without_wake
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
        subject = found.group(1).lower()
        return Intent(
            name="status",
            tool="apps.system_status",
            formatter="status",
            args={"include_gpu": True} if subject == "gpu" else {},
            subject=subject,
        )
    if found := _LIST_DIR.match(stripped):
        target = found.group(1).strip().strip("\"'")
        if target and _plausible_target(target) and not _looks_like_url(target):
            return Intent(
                name="list_dir",
                tool="files.list_dir",
                args={"path": target},
                formatter="list_dir",
            )
    if found := _YOUTUBE_SEARCH.match(stripped):
        query = found.group(1).strip()
        if query:
            return Intent(
                name="youtube_search",
                tool="apps.open_url",
                args={"url": f"https://www.youtube.com/results?search_query={quote_plus(query)}"},
                formatter="open_url",
            )
    if found := _WEB_SEARCH.match(stripped):
        query = found.group(1).strip()
        if query:
            return Intent(
                name="web_search",
                tool="apps.open_url",
                args={"url": f"https://www.google.com/search?q={quote_plus(query)}"},
                formatter="open_url",
            )
    if found := _OPEN.match(stripped):
        target = found.group(1).strip().strip("\"'")
        if not target:
            return None
        site = SITES.get(target.lower().removesuffix(" website").removesuffix(" web"))
        if site is None and not _plausible_target(target) and not _looks_like_url(target):
            return None  # "open up about yourself" is not an application
        if site is not None:
            return Intent(
                name="open_site", tool="apps.open_url", args={"url": site}, formatter="open_url"
            )
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


def _format_status(result: dict[str, Any], intent: Intent) -> str:
    """Answer the question that was asked, not every reading available.

    "What's my battery?" wants a percentage, not a four-part hardware report -
    dumping everything is the same over-answering that makes an assistant
    tiring to talk to.
    """
    memory = result.get("memory", {})
    disk = result.get("disk", {})
    battery = result.get("battery")
    gpu = result.get("gpu")

    subject = intent.subject
    if subject == "battery":
        if not battery:
            return "This machine has no battery (or Windows isn't reporting one)."
        state = "plugged in" if battery.get("plugged_in") else "on battery"
        return f"{battery.get('percent')}%, {state}."
    if subject == "cpu":
        return f"CPU is at {result.get('cpu_percent')}%."
    if subject == "gpu":
        if not gpu:
            return "Windows didn't report a GPU utilization counter on this machine."
        return f"GPU is at {gpu.get('percent')}%."
    if subject in ("memory", "ram"):
        return f"Memory is {memory.get('used_percent')}% used of {memory.get('total_gb')} GB."
    if subject in ("disk", "storage"):
        return f"{disk.get('free_gb')} GB free of {disk.get('total_gb')} GB."

    pieces = [
        f"CPU {result.get('cpu_percent')}%",
        f"memory {memory.get('used_percent')}% of {memory.get('total_gb')} GB",
        f"disk {disk.get('free_gb')} GB free of {disk.get('total_gb')} GB",
    ]
    if gpu:
        pieces.append(f"GPU {gpu.get('percent')}%")
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
