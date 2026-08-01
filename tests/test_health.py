"""Health tracker tests: failure counting, cooldowns, and recovery."""

from __future__ import annotations

import sqlite3

from myagent.config import Settings
from myagent.gateway.health import FAILURE_THRESHOLD, HealthTracker


class Clock:
    def __init__(self, start: float = 1_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


def test_available_by_default(db: sqlite3.Connection, settings: Settings) -> None:
    tracker = HealthTracker(settings.db_path())
    assert tracker.is_available("p1") is True


def test_below_threshold_stays_available(db: sqlite3.Connection, settings: Settings) -> None:
    tracker = HealthTracker(settings.db_path())
    for _ in range(FAILURE_THRESHOLD - 1):
        tracker.record_failure("p1")
    assert tracker.is_available("p1") is True


def test_threshold_triggers_cooldown_and_expiry_restores(
    db: sqlite3.Connection, settings: Settings
) -> None:
    clock = Clock()
    tracker = HealthTracker(settings.db_path(), now=clock)
    for _ in range(FAILURE_THRESHOLD):
        tracker.record_failure("p1")
    assert tracker.is_available("p1") is False
    clock.now += 31  # base cooldown is 30s
    assert tracker.is_available("p1") is True


def test_cooldown_grows_with_repeated_failures(db: sqlite3.Connection, settings: Settings) -> None:
    clock = Clock()
    tracker = HealthTracker(settings.db_path(), now=clock)
    for _ in range(FAILURE_THRESHOLD + 1):
        tracker.record_failure("p1")
    clock.now += 31  # would clear the base cooldown, but this one doubled
    assert tracker.is_available("p1") is False
    clock.now += 31
    assert tracker.is_available("p1") is True


def test_success_resets_failures(db: sqlite3.Connection, settings: Settings) -> None:
    clock = Clock()
    tracker = HealthTracker(settings.db_path(), now=clock)
    for _ in range(FAILURE_THRESHOLD):
        tracker.record_failure("p1")
    clock.now += 31
    tracker.record_success("p1")
    tracker.record_failure("p1")  # first failure of a fresh count
    assert tracker.is_available("p1") is True


def test_health_is_per_provider(db: sqlite3.Connection, settings: Settings) -> None:
    tracker = HealthTracker(settings.db_path())
    for _ in range(FAILURE_THRESHOLD):
        tracker.record_failure("p1")
    assert tracker.is_available("p1") is False
    assert tracker.is_available("p2") is True
