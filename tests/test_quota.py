"""Quota governor tests: buckets, persistence, windows, and headroom."""

from __future__ import annotations

import sqlite3

import pytest

from myagent.config import Settings
from myagent.gateway.quota import INTERACTIVE_HEADROOM, QuotaGovernor
from myagent.gateway.types import ModelSpec


@pytest.fixture
def spec() -> ModelSpec:
    return ModelSpec(provider="p1", id="m", rpm=4, rpd=10, tpm=100)


class Clock:
    def __init__(self, start: float = 1_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


def test_fresh_model_can_be_used(
    db: sqlite3.Connection, settings: Settings, spec: ModelSpec
) -> None:
    governor = QuotaGovernor(settings.db_path())
    assert governor.can_use(spec) is True


def test_rpm_exhaustion_blocks_and_window_reset_unblocks(
    db: sqlite3.Connection, settings: Settings, spec: ModelSpec
) -> None:
    clock = Clock()
    governor = QuotaGovernor(settings.db_path(), now=clock)
    for _ in range(spec.rpm):
        governor.record_request(spec)
    assert governor.can_use(spec) is False
    clock.now += 61  # past the rpm window
    assert governor.can_use(spec) is True


def test_rpd_counts_survive_restart(
    db: sqlite3.Connection, settings: Settings, spec: ModelSpec
) -> None:
    """A new governor over the same database sees the same daily count."""
    clock = Clock()
    first = QuotaGovernor(settings.db_path(), now=clock)
    for _ in range(spec.rpd):
        first.record_request(spec)
    clock.now += 120  # rpm window expired, rpd window has not
    second = QuotaGovernor(settings.db_path(), now=clock)
    assert second.can_use(spec) is False


def test_background_respects_interactive_headroom(
    db: sqlite3.Connection, settings: Settings, spec: ModelSpec
) -> None:
    governor = QuotaGovernor(settings.db_path())
    background_ceiling = int(spec.rpd * (1 - INTERACTIVE_HEADROOM))
    # Fill rpm slowly enough not to trip it: use rpd as the constraining window.
    clock = Clock()
    governor = QuotaGovernor(settings.db_path(), now=clock)
    for _ in range(background_ceiling):
        governor.record_request(spec)
        clock.now += 61  # keep rpm fresh; rpd accumulates
    assert governor.can_use(spec, interactive=False) is False
    assert governor.can_use(spec, interactive=True) is True


def test_token_accounting_blocks_tpm(
    db: sqlite3.Connection, settings: Settings, spec: ModelSpec
) -> None:
    governor = QuotaGovernor(settings.db_path())
    governor.record_tokens(spec, spec.tpm)
    assert governor.can_use(spec) is False


def test_usage_snapshot(db: sqlite3.Connection, settings: Settings, spec: ModelSpec) -> None:
    governor = QuotaGovernor(settings.db_path())
    governor.record_request(spec)
    governor.record_tokens(spec, 40)
    usage = governor.usage(spec)
    assert usage["rpm"] == (1, 4)
    assert usage["rpd"] == (1, 10)
    assert usage["tpm"] == (40, 100)
