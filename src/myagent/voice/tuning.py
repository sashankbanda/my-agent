"""Choosing a wake phrase by measurement instead of taste.

The only question that matters is "how reliably does speech-to-text hear THIS
phrase in MY voice", and it has a numeric answer. Synthesised speech gives a
first approximation - it already showed that "hey ev" is hopeless, scoring
0.67 against itself and 0.67 against unrelated speech - but a synthetic voice
is not the user's voice, and accent is exactly the variable that made the
pretrained wake models score 0.00 here.

So the tuner records the user saying each candidate, transcribes on-device,
and scores two things that both matter:

- **hit rate**: how often the phrase was recognised at all
- **margin**: how far its score sits above what ordinary speech scores, which
  is what decides false triggers

A phrase that matches itself perfectly is still bad if random conversation
scores nearly as high. "hey ev" fails exactly there.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from myagent.voice.wake import PhraseWake

# Deliberately ordinary words with a real vowel after the carrier - the shape
# that measured well - and no two that sound alike. Users can pass their own.
DEFAULT_CANDIDATES: tuple[str, ...] = (
    "hey buddy",
    "hey nova",
    "hey eva",
    "okay computer",
    "hey friday",
    "hey sunny",
)

# Sentences the phrase must NOT match: everyday speech, plus near-misses that
# share the carrier word, which is where false triggers actually come from.
CONTROL_PHRASES: tuple[str, ...] = (
    "what is the weather today",
    "hey there how are you doing",
    "can you hear me now",
    "the meeting is at seven",
    "hey everyone lets get started",
)

GOOD_HIT_RATE = 0.8
GOOD_MARGIN = 0.25


@dataclass
class PhraseScore:
    """How one candidate performed against a real voice."""

    phrase: str
    heard: list[str] = field(default_factory=list)
    hits: int = 0
    attempts: int = 0
    best_control: float = 0.0

    @property
    def hit_rate(self) -> float:
        """Fraction of attempts that were recognised."""
        return self.hits / self.attempts if self.attempts else 0.0

    @property
    def mean_similarity(self) -> float:
        """Average score across attempts, recognised or not."""
        matcher = PhraseWake(self.phrase)
        if not self.heard:
            return 0.0
        return sum(matcher.best_similarity(text) for text in self.heard) / len(self.heard)

    @property
    def margin(self) -> float:
        """Gap between this phrase's score and ordinary speech's.

        The number that decides false triggers. A phrase can score 1.00
        against itself and still be unusable if conversation scores 0.95.
        """
        return self.mean_similarity - self.best_control

    @property
    def verdict(self) -> str:
        """One word for the results table."""
        if self.hit_rate >= GOOD_HIT_RATE and self.margin >= GOOD_MARGIN:
            return "good"
        if self.hit_rate >= 0.5:
            return "ok"
        return "poor"

    @property
    def rank_key(self) -> tuple[float, float]:
        """Sort order: reliability first, then resistance to false triggers."""
        return (self.hit_rate, self.margin)


def control_ceiling(phrase: str, controls: tuple[str, ...] = CONTROL_PHRASES) -> float:
    """The best score ordinary speech achieves against this phrase.

    Computed from written control sentences rather than recorded ones: the
    user should not have to read five extra sentences aloud, and the failure
    this catches - a phrase that looks like common speech - is present in the
    text already.
    """
    matcher = PhraseWake(phrase)
    return max((matcher.best_similarity(control) for control in controls), default=0.0)


def rank(scores: list[PhraseScore]) -> list[PhraseScore]:
    """Best candidate first."""
    return sorted(scores, key=lambda score: score.rank_key, reverse=True)


def recommend(scores: list[PhraseScore]) -> str:
    """Advice for the user, based on what actually happened."""
    ranked = rank(scores)
    if not ranked:
        return "No candidates were tested."
    best = ranked[0]
    if best.verdict == "poor":
        return (
            "None of these were recognised reliably. Check the microphone with\n"
            "  --mic-check, and try again somewhere quieter."
        )
    lines = [
        f'Best for your voice: "{best.phrase}"',
        f"  recognised {best.hits}/{best.attempts} times, margin {best.margin:.2f}",
        "",
        "Put this in config/voice.yaml:",
        "  wake:",
        f'    phrase: "{best.phrase}"',
    ]
    if best.margin < GOOD_MARGIN:
        lines.append(
            f"\nNote: the margin is thin ({best.margin:.2f}), so ordinary conversation\n"
            "may occasionally trigger it. Raise wake.phrase_similarity if that happens."
        )
    return "\n".join(lines)
