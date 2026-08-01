"""Permission tiers: three levels of risk (v3 review F12).

T0 READ            - observe only, no effects. Auto-allowed.
T1 REVERSIBLE      - writes that can be undone or that stay inside permitted
                     roots (move a file, launch an app). Auto-allowed locally,
                     confirmed for remote sessions and tainted turns.
T2 CONFIRM_ALWAYS  - destructive, outward-visible, or arbitrary execution
                     (delete, run a shell command). Always confirmed unless a
                     standing grant exists - and never on a tainted turn.

The v1 design had four tiers; T2 and T3 produced no different enforcement
path, so they are one tier. Add a tier only when it changes behavior.
"""

from __future__ import annotations

from enum import IntEnum


class Tier(IntEnum):
    """Risk tier of a tool. Higher means more dangerous."""

    READ = 0
    REVERSIBLE = 1
    CONFIRM_ALWAYS = 2

    @property
    def label(self) -> str:
        """Short human-readable name for UI and audit output."""
        return {Tier.READ: "read", Tier.REVERSIBLE: "write", Tier.CONFIRM_ALWAYS: "danger"}[self]


class Decision(IntEnum):
    """Outcome of an authorization request."""

    ALLOW = 0
    CONFIRM = 1
    DENY = 2
