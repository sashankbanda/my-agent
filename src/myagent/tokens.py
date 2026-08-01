"""Token estimation.

One estimator for the whole kernel (quota accounting, context budgeting).
Chars/4 is deliberately rough: budgets that depend on it carry safety margins,
and a real tokenizer dependency is not worth its weight for personal-scale
budgeting.
"""

from __future__ import annotations


def estimate_tokens(text: str) -> int:
    """Rough token count for budgeting; always at least 1 for non-empty text."""
    if not text:
        return 0
    return max(1, len(text) // 4)
