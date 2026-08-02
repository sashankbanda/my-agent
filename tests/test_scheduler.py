"""Scheduler tests: fires once, respects enabled, survives restart, no dupes.

The interesting cases are all about *not* running things: a paused schedule,
an overlapping run, and a misfire after the laptop was asleep. A scheduler
that runs too much is worse than one that runs too little - a cron that moves
files every minute is the classic footgun.
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from myagent import scheduler
from myagent.config import Settings


@pytest.fixture
def db_path(settings: Settings, db: sqlite3.Connection) -> Path:
    """A migrated database (the ``db`` fixture applies migrations)."""
    return settings.db_path()


class TestCron:
    def test_next_fire_is_in_the_future(self) -> None:
        base = datetime(2026, 8, 2, 7, 30)
        assert scheduler.next_fire("0 8 * * *", base) == datetime(2026, 8, 2, 8, 0)

    def test_next_fire_rolls_over_midnight(self) -> None:
        base = datetime(2026, 8, 2, 9, 0)
        assert scheduler.next_fire("0 8 * * *", base) == datetime(2026, 8, 3, 8, 0)

    @pytest.mark.parametrize("bad", ["", "not a cron", "99 99 * * *", "* *"])
    def test_invalid_cron_is_refused_at_creation(self, bad: str) -> None:
        """A schedule that silently never fires is worse than one that errors."""
        with pytest.raises(scheduler.ScheduleError):
            scheduler.next_fire(bad)


class TestCrud:
    def test_add_computes_the_next_run(self, db_path: Path) -> None:
        created = scheduler.add(db_path, "Briefing", "0 8 * * *", "brief me")
        assert created.enabled is True
        assert created.next_run > datetime.now().strftime(scheduler.TIMESTAMP)

    def test_add_rejects_an_empty_task(self, db_path: Path) -> None:
        with pytest.raises(scheduler.ScheduleError, match="task is empty"):
            scheduler.add(db_path, "Nameless", "0 8 * * *", "   ")

    def test_add_rejects_a_bad_cron_before_storing(self, db_path: Path) -> None:
        with pytest.raises(scheduler.ScheduleError):
            scheduler.add(db_path, "Broken", "not a cron", "do something")
        assert scheduler.list_all(db_path) == []

    def test_remove_reports_a_missing_id(self, db_path: Path) -> None:
        with pytest.raises(scheduler.ScheduleError, match="no schedule"):
            scheduler.remove(db_path, 999)

    def test_pausing_keeps_it_out_of_due(self, db_path: Path) -> None:
        created = scheduler.add(db_path, "Hourly", "* * * * *", "tick")
        scheduler.set_enabled(db_path, created.id, False)
        later = datetime.now() + timedelta(hours=2)
        assert scheduler.due(db_path, later) == []

    def test_resuming_recomputes_from_now(self, db_path: Path) -> None:
        """A schedule paused for a week must not wake up a week overdue."""
        created = scheduler.add(db_path, "Daily", "0 8 * * *", "tick")
        scheduler.set_enabled(db_path, created.id, False)
        resumed = scheduler.set_enabled(db_path, created.id, True)
        assert resumed.next_run >= datetime.now().strftime(scheduler.TIMESTAMP)


class TestDueAndMisfire:
    def test_due_finds_only_what_has_arrived(self, db_path: Path) -> None:
        soon = scheduler.add(db_path, "Soon", "* * * * *", "tick")
        scheduler.add(db_path, "Later", "0 4 * * *", "much later")
        found = scheduler.due(db_path, datetime.now() + timedelta(minutes=2))
        assert [item.id for item in found] == [soon.id]

    def test_a_long_outage_fires_once_not_once_per_slot(self, db_path: Path) -> None:
        """The laptop was asleep for three days; that is one briefing, not three."""
        created = scheduler.add(db_path, "Daily", "0 8 * * *", "brief me")
        woke = datetime.now() + timedelta(days=3)

        assert len(scheduler.due(db_path, woke)) == 1
        scheduler.record_run(db_path, created.id, "ok", None, woke)
        # After running, the next slot is ahead of the wake-up moment.
        assert scheduler.due(db_path, woke) == []
        assert scheduler.get(db_path, created.id).next_run > woke.strftime(scheduler.TIMESTAMP)

    def test_a_late_run_is_recognised_as_a_misfire(self, db_path: Path) -> None:
        created = scheduler.add(db_path, "Daily", "0 8 * * *", "brief me")
        stale = scheduler.get(db_path, created.id)
        late = datetime.strptime(stale.next_run, scheduler.TIMESTAMP) + timedelta(hours=5)
        assert scheduler.is_misfire(stale, late) is True

    def test_an_on_time_run_is_not_a_misfire(self, db_path: Path) -> None:
        created = scheduler.add(db_path, "Daily", "0 8 * * *", "brief me")
        stale = scheduler.get(db_path, created.id)
        on_time = datetime.strptime(stale.next_run, scheduler.TIMESTAMP) + timedelta(seconds=20)
        assert scheduler.is_misfire(stale, on_time) is False

    def test_next_run_survives_a_restart(self, db_path: Path) -> None:
        """Nothing is held in memory: a fresh read sees the same schedule."""
        created = scheduler.add(db_path, "Daily", "0 8 * * *", "brief me")
        reloaded = scheduler.get(db_path, created.id)
        assert reloaded.next_run == created.next_run
        assert reloaded.task == "brief me"


class TestExecution:
    async def test_a_due_task_runs_once(self, db_path: Path, db: sqlite3.Connection) -> None:
        ran: list[str] = []

        async def runner(task: str) -> str:
            ran.append(task)
            return "done"

        created = scheduler.add(db_path, "Tick", "* * * * *", "do the thing")
        engine = scheduler.Scheduler(db_path, runner)
        await engine.tick(datetime.now() + timedelta(minutes=2))
        await asyncio.sleep(0.05)

        assert ran == ["do the thing"]
        assert scheduler.get(db_path, created.id).last_status == "ok"
        types = [row["type"] for row in db.execute("SELECT type FROM events ORDER BY id")]
        assert "ScheduleFired" in types

    async def test_a_second_tick_does_not_double_run(self, db_path: Path) -> None:
        """Two polls inside one slot must not produce two executions."""
        ran: list[str] = []

        async def runner(task: str) -> str:
            ran.append(task)
            return "done"

        scheduler.add(db_path, "Tick", "0 8 * * *", "do the thing")
        engine = scheduler.Scheduler(db_path, runner)
        moment = datetime.now() + timedelta(days=1)
        await engine.tick(moment)
        await asyncio.sleep(0.05)
        await engine.tick(moment)
        await asyncio.sleep(0.05)

        assert ran == ["do the thing"]

    async def test_an_overlapping_run_is_skipped_not_stacked(self, db_path: Path) -> None:
        release = asyncio.Event()
        started = 0

        async def slow(_task: str) -> str:
            nonlocal started
            started += 1
            await release.wait()
            return "eventually"

        created = scheduler.add(db_path, "Slow", "* * * * *", "long job")
        engine = scheduler.Scheduler(db_path, slow)
        await engine.tick(datetime.now() + timedelta(minutes=2))
        await asyncio.sleep(0.05)
        await engine.tick(datetime.now() + timedelta(minutes=4))
        await asyncio.sleep(0.05)

        assert started == 1, "a slow task must not pile up copies of itself"
        assert scheduler.get(db_path, created.id).last_status == "skipped"
        release.set()
        await asyncio.sleep(0.05)

    async def test_a_failing_task_is_recorded_not_swallowed(self, db_path: Path) -> None:
        async def broken(_task: str) -> str:
            raise RuntimeError("the provider was down")

        created = scheduler.add(db_path, "Broken", "* * * * *", "fail please")
        engine = scheduler.Scheduler(db_path, broken)
        await engine.tick(datetime.now() + timedelta(minutes=2))
        await asyncio.sleep(0.05)

        stored = scheduler.get(db_path, created.id)
        assert stored.last_status == "failed"
        assert "provider was down" in (stored.last_error or "")

    async def test_a_failure_still_advances_the_schedule(self, db_path: Path) -> None:
        """A broken task must not become a hot loop retrying every poll."""

        async def broken(_task: str) -> str:
            raise RuntimeError("nope")

        created = scheduler.add(db_path, "Broken", "0 8 * * *", "fail")
        engine = scheduler.Scheduler(db_path, broken)
        moment = datetime.now() + timedelta(days=1)
        await engine.tick(moment)
        await asyncio.sleep(0.05)

        assert scheduler.get(db_path, created.id).next_run > moment.strftime(scheduler.TIMESTAMP)

    async def test_a_disabled_task_never_runs(self, db_path: Path) -> None:
        ran: list[str] = []

        async def runner(task: str) -> str:
            ran.append(task)
            return "done"

        created = scheduler.add(db_path, "Off", "* * * * *", "should not run")
        scheduler.set_enabled(db_path, created.id, False)
        engine = scheduler.Scheduler(db_path, runner)
        await engine.tick(datetime.now() + timedelta(hours=1))
        await asyncio.sleep(0.05)

        assert ran == []

    async def test_run_now_ignores_the_schedule(self, db_path: Path) -> None:
        ran: list[str] = []

        async def runner(task: str) -> str:
            ran.append(task)
            return "done"

        created = scheduler.add(db_path, "Daily", "0 8 * * *", "brief me")
        engine = scheduler.Scheduler(db_path, runner)
        assert await engine.run_now(created.id) is True
        await asyncio.sleep(0.05)

        assert ran == ["brief me"]

    async def test_run_now_refuses_a_second_copy(self, db_path: Path) -> None:
        release = asyncio.Event()

        async def slow(_task: str) -> str:
            await release.wait()
            return "done"

        created = scheduler.add(db_path, "Slow", "0 8 * * *", "long job")
        engine = scheduler.Scheduler(db_path, slow)
        assert await engine.run_now(created.id) is True
        await asyncio.sleep(0.05)
        assert await engine.run_now(created.id) is False
        release.set()
        await asyncio.sleep(0.05)

    async def test_the_result_is_announced(self, db_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        """Unattended work you never hear about may as well not have run."""
        sent: list[tuple[str, str]] = []
        monkeypatch.setattr(
            "myagent.notify.send",
            lambda title, body, *a, **k: sent.append((title, body)),
        )

        async def runner(_task: str) -> str:
            return "Three headlines today."

        scheduler.add(db_path, "Briefing", "* * * * *", "brief me")
        engine = scheduler.Scheduler(db_path, runner)
        await engine.tick(datetime.now() + timedelta(minutes=2))
        await asyncio.sleep(0.05)

        assert sent == [("Briefing", "Three headlines today.")]


class TestSnapshotSchedule:
    def test_the_nightly_backup_becomes_a_normal_row(self, db_path: Path) -> None:
        """One clock in the system: the backup is editable like anything else."""
        scheduler.ensure_snapshot_schedule(db_path, 3)
        rows = scheduler.list_all(db_path)
        assert [item.cron for item in rows] == ["0 3 * * *"]
        assert rows[0].task == scheduler.SNAPSHOT_TASK

    def test_it_is_created_once_not_per_start(self, db_path: Path) -> None:
        scheduler.ensure_snapshot_schedule(db_path, 3)
        scheduler.ensure_snapshot_schedule(db_path, 3)
        assert len(scheduler.list_all(db_path)) == 1

    def test_changing_the_hour_updates_the_existing_row(self, db_path: Path) -> None:
        scheduler.ensure_snapshot_schedule(db_path, 3)
        scheduler.ensure_snapshot_schedule(db_path, 5)
        rows = scheduler.list_all(db_path)
        assert len(rows) == 1
        assert rows[0].cron == "0 5 * * *"
