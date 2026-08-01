"""Taint tracking: the prompt-injection defense.

The threat: the assistant reads untrusted content (file contents, web pages,
screen text, tool output) *and* holds real capabilities. A malicious document
saying "delete C:\\Users" must never be able to act.

The rule (SEC-07): once a turn has consumed untrusted content, standing
permission grants are suspended for that turn - any T1+ action requires fresh
human confirmation. Injected text can therefore *talk*, but never *act*.

This is deliberately ~40 lines. It is enforced in the broker, below
cognition, so no prompt wording can disable it.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class TurnContext:
    """Per-turn security state, carried from the loop into the broker.

    ``channel`` is where the request came from ("local" console/desktop vs
    "remote" phone/PWA); remote sessions get stricter treatment (SEC-09).
    """

    session_id: str
    channel: str = "local"
    tainted: bool = False
    taint_sources: list[str] = field(default_factory=list)

    @property
    def is_remote(self) -> bool:
        return self.channel != "local"

    def taint(self, source: str) -> None:
        """Mark this turn as having consumed untrusted content.

        Called by every tool that returns content the assistant did not
        author: file reads, command output, and (from M5/M7) web pages,
        clipboard, and screen text. Tainting is one-way for the turn.
        """
        self.tainted = True
        if source not in self.taint_sources:
            self.taint_sources.append(source)

    def describe_taint(self) -> str:
        """Human-readable reason for the escalation, shown in confirmations."""
        return ", ".join(self.taint_sources) if self.taint_sources else "untrusted content"
