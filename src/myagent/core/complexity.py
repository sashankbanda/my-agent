"""Deciding whether a turn needs the big model.

Most of what people say to an assistant is easy: chit-chat, short factual
questions, single mechanical actions. A 3B model on the CPU answers those in a
second and costs nothing. Hard reasoning, code, planning, and anything
multi-step still deserves the cloud model.

The classifier is deliberately *not* a model call - a model deciding whether to
call a model is both slow and circular. It scores cheap, legible signals:

- length and clause structure (long, comma-heavy input tends to be complex)
- explicit reasoning verbs ("why", "compare", "plan", "debug", "refactor")
- domain markers that small models handle badly (code, maths, legal, medical)
- conversation state (an in-flight tool chain stays with the model that started
  it, because tool-calling is where small models are weakest)

Bias: **when in doubt, escalate.** A slow correct answer beats a fast wrong
one, and the local tier is an optimization, never a downgrade the user did not
ask for.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Any of these on their own means "use the strong model".
HARD_MARKERS = (
    "why",
    "how come",
    "explain",
    "compare",
    "analyse",
    "analyze",
    "evaluate",
    "plan ",
    "strategy",
    "design",
    "architect",
    "debug",
    "refactor",
    "optimize",
    "optimise",
    "prove",
    "derive",
    "calculate",
    "estimate",
    "translate",
    "summarize",
    "summarise",
    "rewrite",
    "draft",
    "write me",
    "write a",
    "code",
    "script",
    "function",
    "regex",
    "sql",
    "error",
    "exception",
    "traceback",
    "stack trace",
    "algorithm",
    "trade-off",
    "tradeoff",
    "pros and cons",
    "should i",
    "recommend",
    "advice",
    "diagnose",
)

# Multi-step or conditional phrasing: the model needs to plan.
STRUCTURE_MARKERS = (" and then ", " after that ", " if ", " unless ", " otherwise ", " but only ")

CODE_PATTERN = re.compile(r"[{}<>]|\b(def|class|import|SELECT|function|const|var)\b")
MATH_PATTERN = re.compile(r"\d+\s*[-+*/^]\s*\d+|\b(integral|derivative|equation|matrix)\b", re.I)

SHORT_TURN_CHARS = 160
MANY_CLAUSES = 3


@dataclass
class Routing:
    """Where a turn should go, and why (the reason shows up in the HUD)."""

    use_local: bool
    reason: str


def classify(text: str, has_tool_history: bool = False, history_depth: int = 0) -> Routing:
    """Decide whether the local model can handle this turn."""
    stripped = text.strip()
    lowered = f" {stripped.lower()} "

    if has_tool_history:
        return Routing(False, "continuing a tool sequence")
    if len(stripped) > SHORT_TURN_CHARS:
        return Routing(False, "long request")
    if any(marker in lowered for marker in HARD_MARKERS):
        return Routing(False, "needs reasoning")
    if any(marker in lowered for marker in STRUCTURE_MARKERS):
        return Routing(False, "multi-step request")
    if CODE_PATTERN.search(stripped):
        return Routing(False, "code")
    if MATH_PATTERN.search(stripped):
        return Routing(False, "maths")
    if stripped.count(",") >= MANY_CLAUSES:
        return Routing(False, "many clauses")
    if history_depth > 12:
        # Long conversations carry context a small model handles poorly.
        return Routing(False, "long conversation")
    return Routing(True, "simple request")


# Signs that the local model produced something unusable and the turn should be
# retried on the strong model. Checked against the *complete* local answer.
_UNCERTAIN = (
    "i don't know",
    "i do not know",
    "i'm not sure",
    "i am not sure",
    "as an ai",
    "i cannot help",
    "i can't help",
    "unable to answer",
    "i don't have enough",
    "cannot determine",
)
MIN_USEFUL_CHARS = 2


def should_escalate(answer: str) -> tuple[bool, str]:
    """True if the local answer is not good enough to show the user."""
    text = answer.strip()
    if len(text) < MIN_USEFUL_CHARS:
        return True, "local model returned nothing"
    lowered = text.lower()
    if any(phrase in lowered for phrase in _UNCERTAIN):
        return True, "local model was unsure"
    # Small models sometimes loop a phrase; a very repetitive answer is broken.
    words = lowered.split()
    if len(words) > 30 and len(set(words)) < len(words) * 0.35:
        return True, "local model output was degenerate"
    return False, ""
