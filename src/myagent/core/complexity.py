"""Deciding whether a turn needs the big model.

Most of what people say to an assistant is easy: chit-chat, short factual
questions, single mechanical actions. A 3B model on the CPU answers those in a
second and costs nothing. Hard reasoning, code, planning, and anything
multi-step still deserves the cloud model.

The classifier is deliberately *not* a model call - a model deciding whether to
call a model is both slow and circular. It scores cheap, legible signals:

- length and clause structure (long, comma-heavy input tends to be complex)
- explicit reasoning verbs ("why", "compare", "plan", "debug", "refactor")
- **action intent** - anything that wants something *done* on this machine, or
  asks about this machine's hardware, needs a tool call, and tool calling is
  where small models fail worst: they answer "you could open Task Manager and
  look" instead of just looking
- domain markers that small models handle badly (code, maths, legal, medical)
- conversation state (an in-flight tool chain stays with the model that started
  it, for the same tool-calling reason)

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

# Requests that want something DONE on this machine. The fast path already
# handles the phrasings it recognizes for free; anything that reaches here
# needs a tool call, and tool calling is exactly where a 3B model fails - it
# answers "you could open Task Manager and look" instead of calling the tool.
# Those turns go to the cloud, which calls tools reliably.
ACTION_MARKERS = (
    " open ",
    " launch ",
    " start ",
    " close ",
    " quit ",
    " kill ",
    " play ",
    " pause ",
    " delete ",
    " remove ",
    " move ",
    " rename ",
    " copy ",
    " create ",
    " make a ",
    " download ",
    " install ",
    " check ",
    " show ",
    " list ",
    " find ",
    " find out ",
    " search ",
    " look up ",
    " look for ",
    " google ",
    " research ",
    " browse ",
    " send ",
    " remind ",
    " set ",
    " turn on ",
    " turn off ",
    " volume ",
    " screenshot ",
    " my pc ",
    " my laptop ",
    " my computer ",
)

# Hardware and OS nouns. On their own these prove nothing - "what does CPU
# stand for" is general knowledge, not a question about this laptop. What makes
# it a tool question is either a possessive ("my cpu", "this disk") or a
# measurement word attached to it ("gpu usage", "disk space free").
_MACHINE_NOUN = (
    r"cpu|gpu|ram|memory|disk|storage|battery|wifi|wi-fi|network|bluetooth|"
    r"folder|directory|files?|apps?|process(?:es)?|window|browser|"
    r"downloads|desktop|documents"
)
_OWNED_MACHINE = re.compile(rf"\b(?:my|this|the)\s+(?:\w+\s+)?(?:{_MACHINE_NOUN})\b", re.I)
_MEASURED_MACHINE = re.compile(
    rf"\b(?:{_MACHINE_NOUN})\b\s*(?:usage|status|level|percent|percentage|load|space|"
    r"free|left|remaining|temperature|temp|running|open)",
    re.I,
)


def asks_about_this_machine(text: str) -> bool:
    """True when a hardware/OS noun refers to *this* computer, not the concept."""
    return bool(_OWNED_MACHINE.search(text) or _MEASURED_MACHINE.search(text))


CODE_PATTERN = re.compile(r"[{}<>]|\b(def|class|import|SELECT|function|const|var)\b")
MATH_PATTERN = re.compile(r"\d+\s*[-+*/^]\s*\d+|\b(integral|derivative|equation|matrix)\b", re.I)

SHORT_TURN_CHARS = 160
MANY_CLAUSES = 3
# Memory assembly caps the transcript well below this (facts 600 + retrieved
# 800 + recent 2400 + prompt), so this only trips when the prompt is genuinely
# unusual, not merely because the chat has been going a while.
MAX_LOCAL_CONTEXT_CHARS = 6_000


@dataclass
class Routing:
    """Where a turn should go, and why (the reason shows up in the HUD)."""

    use_local: bool
    reason: str
    needs_tool: bool = False  # the turn cannot be answered without acting


def classify(text: str, has_tool_history: bool = False, context_chars: int = 0) -> Routing:
    """Decide whether the local model can handle this turn.

    ``context_chars`` is the size of the transcript that will actually be sent,
    not the length of the conversation. Those differ: memory assembly caps
    context at a few thousand characters however long the chat gets, so a
    hundred-message session does not produce a hundred-message prompt. Judging
    by message count instead switched the local tier off entirely after about
    six exchanges - every "who wrote Hamlet" went to a cloud provider.
    """
    stripped = text.strip()
    lowered = f" {stripped.lower()} "

    if has_tool_history:
        return Routing(False, "continuing a tool sequence")
    if len(stripped) > SHORT_TURN_CHARS:
        return Routing(False, "long request")
    # Reasoning and structure are checked first: "why is my laptop slow" is a
    # question to think about, even though it mentions this machine.
    if any(marker in lowered for marker in HARD_MARKERS):
        return Routing(False, "needs reasoning")
    if any(marker in lowered for marker in STRUCTURE_MARKERS):
        return Routing(False, "multi-step request")
    if any(marker in lowered for marker in ACTION_MARKERS):
        return Routing(False, "wants an action taken", needs_tool=True)
    if asks_about_this_machine(stripped):
        return Routing(False, "asks about this machine", needs_tool=True)
    if CODE_PATTERN.search(stripped):
        return Routing(False, "code")
    if MATH_PATTERN.search(stripped):
        return Routing(False, "maths")
    if stripped.count(",") >= MANY_CLAUSES:
        return Routing(False, "many clauses")
    if context_chars > MAX_LOCAL_CONTEXT_CHARS:
        # Genuinely large prompts (many recalled facts, long retrieved
        # history) are where a 3B starts losing the thread.
        return Routing(False, "large context")
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

# Deflection: instead of using a tool, the model told the user to go do it
# themselves. This is the single most annoying failure mode - the assistant
# has hands and answers with a tutorial - so it is detected explicitly.
_DEFLECTED = (
    "i don't have direct access",
    "i do not have direct access",
    "i don't have access to",
    "i do not have access to",
    "i don't see any direct tool",
    "i'm unable to access",
    "i am unable to access",
    "i'm unable to directly",
    "i am unable to directly",
    "unable to directly check",
    "i can't directly check",
    "i cannot directly check",
    "you can check this by",
    "you can use file explorer",
    "you would need to",
    "you'll need to",
    "right-click",
    "open task manager",
    "task manager",
)
MIN_USEFUL_CHARS = 2


def looks_like_deflection(answer: str) -> bool:
    """True when a reply explains how to do something instead of doing it."""
    return any(phrase in answer.lower() for phrase in _DEFLECTED)


# Small models sometimes *write out* a tool call as prose instead of emitting
# it on the tool-call channel, leaking raw JSON into the answer. Observed from
# the 3B: 'subur "{ "name": "apps.list_processes", "arguments": {...}}"'.
_TOOL_LEAK = re.compile(r"[\"'](?:name|arguments|tool_call|function)[\"']\s*:", re.I)


def looks_like_tool_leak(answer: str) -> bool:
    """True when a reply contains a tool call the model failed to actually make."""
    return bool(_TOOL_LEAK.search(answer))


# qwen2.5 is a Chinese-origin model and drifts into Chinese unprompted - one
# English greeting was enough to trigger it. Detect the drift rather than
# trusting the system prompt alone, because a 3B follows instructions loosely.
_LATIN_END = 0x24F  # end of Latin Extended-B; anything past it is another script
# Generous: a genuinely drifted reply is ~100% foreign, while a legitimate
# English answer that quotes a foreign word or name stays well under this.
_FOREIGN_SHARE = 0.3

# If the user asked for another language, replying in it is correct, so the
# automatic correction must stand down.
_LANGUAGE_REQUEST = re.compile(
    r"\b(?:chinese|mandarin|cantonese|hindi|telugu|tamil|kannada|malayalam|marathi|"
    r"bengali|gujarati|punjabi|urdu|spanish|french|german|japanese|korean|arabic|"
    r"russian|portuguese|italian|dutch|turkish|hebrew|thai|vietnamese|"
    r"language|translate|translation)\b",
    re.I,
)


def _foreign_script_share(text: str) -> float:
    """Fraction of the letters that are outside the Latin alphabet."""
    letters = [character for character in text if character.isalpha()]
    if not letters:
        return 0.0
    foreign = sum(1 for character in letters if ord(character) > _LATIN_END)
    return foreign / len(letters)


# A different script is easy to spot; a different *language in the same
# script* is not. The 3B drifted into Spanish on an English question, which the
# script check sails straight past. Function words are the cheap giveaway:
# essentially every English sentence of any length contains one of these.
_ENGLISH_WORDS = (
    "the and is are was were you your that this with for from have has had not "
    "can will what when which there their about would should its they them but "
    "because how why been being does did into than then our"
)
# Function words from the languages a multilingual model drifts into. Chosen to
# avoid English homographs ("son", "die", "a" are deliberately absent).
_OTHER_WORDS = (
    "que por para con una los las del como pero mas esta estan porque muy "  # es
    "les des est une pour dans avec sur pas vous nous cette sont ils elle "  # fr
    "der das und ist nicht sie mit auf fur ein eine auch wird sind "  # de
    "nao uma voce tambem isso seu "  # pt
    "che non questo della anche perche sono gli"  # it
)
_ENGLISH_MARKERS = frozenset(_ENGLISH_WORDS.split())
_OTHER_MARKERS = frozenset(_OTHER_WORDS.split())
MIN_WORDS_FOR_LANGUAGE_CHECK = 6
MIN_OTHER_MARKERS = 2


def _looks_like_another_language(answer: str) -> bool:
    """True when a Latin-script answer is clearly not English.

    Deliberately conservative: it demands *zero* English function words and
    several foreign ones, so an English sentence quoting a foreign phrase is
    never flagged. Missing a drift costs one odd reply; a false positive costs
    a needless retry on every turn.
    """
    words = re.findall(r"[a-zà-öø-ÿ']+", answer.lower())
    if len(words) < MIN_WORDS_FOR_LANGUAGE_CHECK:
        return False
    if any(word in _ENGLISH_MARKERS for word in words):
        return False
    return sum(1 for word in words if word in _OTHER_MARKERS) >= MIN_OTHER_MARKERS


def wrong_language(user_text: str, answer: str) -> bool:
    """True when the reply switched language on the user unasked."""
    if _LANGUAGE_REQUEST.search(user_text):
        return False  # they asked for another language
    if _foreign_script_share(user_text) > _FOREIGN_SHARE:
        return False  # they wrote in that script; matching it is right
    if _foreign_script_share(answer) > _FOREIGN_SHARE:
        return True
    return _looks_like_another_language(answer)


def should_escalate(answer: str) -> tuple[bool, str]:
    """True if the local answer is not good enough to show the user."""
    text = answer.strip()
    if len(text) < MIN_USEFUL_CHARS:
        return True, "local model returned nothing"
    lowered = text.lower()
    if any(phrase in lowered for phrase in _UNCERTAIN):
        return True, "local model was unsure"
    if looks_like_deflection(text):
        return True, "local model explained instead of acting"
    # Small models sometimes loop a phrase; a very repetitive answer is broken.
    words = lowered.split()
    if len(words) > 30 and len(set(words)) < len(words) * 0.35:
        return True, "local model output was degenerate"
    return False, ""
