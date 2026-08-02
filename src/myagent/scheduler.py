"""The scheduler: a poller you own, not a framework.

Every ~30 seconds it asks one indexed question - "what is due?" - and runs
what comes back through the ordinary agent loop. A scheduled task is a normal
turn with a clock for a trigger, so it inherits the permission broker, the
audit log, and the event feed rather than becoming a second execution path
with its own bugs.

Three decisions worth knowing:

**Misfires are skipped, not replayed.** A laptop asleep from Friday to Monday
wakes with three missed 8am briefings. Running all three is noise; running
none is a silent failure. It runs the job once and advances to the next slot,
recording that a misfire happened.

**Overlap is impossible.** A job still running when its next slot arrives is
not started again - a task that takes longer than its interval would otherwise
pile up copies of itself until the machine dies.

**next_run is persisted.** Restarting mid-day does not lose or re-fire the
day's schedule; the poller resumes from the stored value.

The nightly vault snapshot moved here from ``scheduler_lite``: one timing
mechanism in the system, not two.
"""

from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from croniter import CroniterBadCronError, croniter

from myagent.config import Settings
from myagent.db import connection
from myagent.events import EventType, append_event, prune_events
from myagent.logging import get_logger

log = get_logger(__name__)

POLL_SECONDS = 30.0
# A run that starts this far past its slot is a misfire: the machine was
# asleep or off, and the moment has passed.
MISFIRE_GRACE = timedelta(minutes=10)
MAX_TASK_SECONDS = 600.0
# Event housekeeping runs on the scheduler's clock; hourly is plenty for
# something that deletes rows older than a week.
PRUNE_EVERY_TICKS = 120
TIMESTAMP = "%Y-%m-%dT%H:%M:%S"

# Recognized by the runner and executed directly rather than sent to the
# model: the backup is a fixed operation, not something to reason about.
SNAPSHOT_TASK = "__vault_snapshot__"


class ScheduleError(ValueError):
    """A schedule was rejected: bad cron, empty task, unknown id."""


@dataclass
class Schedule:
    """One standing instruction."""

    id: int
    name: str
    cron: str
    task: str
    enabled: bool
    next_run: str
    last_run: str | None = None
    last_status: str | None = None
    last_error: str | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Schedule:
        """Build from a database row."""
        return cls(
            id=row["id"],
            name=row["name"],
            cron=row["cron"],
            task=row["task"],
            enabled=bool(row["enabled"]),
            next_run=row["next_run"],
            last_run=row["last_run"],
            last_status=row["last_status"],
            last_error=row["last_error"],
        )

    def as_dict(self) -> dict[str, Any]:
        """Serializable form for the API and UI."""
        return {
            "id": self.id,
            "name": self.name,
            "cron": self.cron,
            "task": self.task,
            "enabled": self.enabled,
            "next_run": self.next_run,
            "last_run": self.last_run,
            "last_status": self.last_status,
            "last_error": self.last_error,
        }


def next_fire(cron: str, after: datetime | None = None) -> datetime:
    """When this cron expression next fires, in local time.

    Raises ScheduleError on an invalid expression - a schedule that silently
    never fires is worse than one that refuses to be created.
    """
    start = after or datetime.now()
    try:
        return croniter(cron, start).get_next(datetime)
    except (CroniterBadCronError, KeyError, ValueError) as exc:
        raise ScheduleError(f"{cron!r} is not a valid cron expression: {exc}") from exc


def add(db_path: Path, name: str, cron: str, task: str, enabled: bool = True) -> Schedule:
    """Create a schedule; returns it with ``next_run`` already computed."""
    if not name.strip():
        raise ScheduleError("name is empty")
    if not task.strip():
        raise ScheduleError("task is empty")
    upcoming = next_fire(cron)
    with connection(db_path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO schedules (name, cron, task, enabled, next_run)
            VALUES (?, ?, ?, ?, ?)
            """,
            (name.strip(), cron.strip(), task.strip(), int(enabled), upcoming.strftime(TIMESTAMP)),
        )
        row_id = cursor.lastrowid
        assert row_id is not None
        append_event(
            conn,
            EventType.SCHEDULE_ADDED,
            {"id": row_id, "name": name.strip(), "cron": cron.strip()},
        )
    log.info("schedule_added", id=row_id, cron=cron, next_run=upcoming.strftime(TIMESTAMP))
    return get(db_path, row_id)


def get(db_path: Path, schedule_id: int) -> Schedule:
    """One schedule by id."""
    with connection(db_path) as conn:
        row = conn.execute("SELECT * FROM schedules WHERE id = ?", (schedule_id,)).fetchone()
    if row is None:
        raise ScheduleError(f"no schedule with id {schedule_id}")
    return Schedule.from_row(row)


def list_all(db_path: Path) -> list[Schedule]:
    """Every schedule, soonest first."""
    with connection(db_path) as conn:
        rows = conn.execute("SELECT * FROM schedules ORDER BY next_run").fetchall()
    return [Schedule.from_row(row) for row in rows]


def set_enabled(db_path: Path, schedule_id: int, enabled: bool) -> Schedule:
    """Pause or resume a schedule.

    Resuming recomputes ``next_run`` from now, so a schedule paused for a week
    does not wake up believing it is a week overdue.
    """
    existing = get(db_path, schedule_id)
    upcoming = next_fire(existing.cron) if enabled else datetime.now()
    with connection(db_path) as conn:
        conn.execute(
            "UPDATE schedules SET enabled = ?, next_run = ? WHERE id = ?",
            (int(enabled), upcoming.strftime(TIMESTAMP), schedule_id),
        )
    log.info("schedule_enabled" if enabled else "schedule_disabled", id=schedule_id)
    return get(db_path, schedule_id)


def remove(db_path: Path, schedule_id: int) -> None:
    """Delete a schedule."""
    with connection(db_path) as conn:
        cursor = conn.execute("DELETE FROM schedules WHERE id = ?", (schedule_id,))
        if cursor.rowcount == 0:
            raise ScheduleError(f"no schedule with id {schedule_id}")
        append_event(conn, EventType.SCHEDULE_REMOVED, {"id": schedule_id})
    log.info("schedule_removed", id=schedule_id)


def due(db_path: Path, now: datetime | None = None) -> list[Schedule]:
    """Enabled schedules whose moment has arrived. The poller's only query."""
    moment = (now or datetime.now()).strftime(TIMESTAMP)
    with connection(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM schedules WHERE enabled = 1 AND next_run <= ? ORDER BY next_run",
            (moment,),
        ).fetchall()
    return [Schedule.from_row(row) for row in rows]


def record_run(
    db_path: Path,
    schedule_id: int,
    status: str,
    error: str | None = None,
    now: datetime | None = None,
) -> None:
    """Mark a run finished and advance to the next slot.

    Advancing is computed from *now*, not from the old ``next_run``: that is
    what makes a misfire fire once instead of once per interval missed.
    """
    moment = now or datetime.now()
    schedule = get(db_path, schedule_id)
    upcoming = next_fire(schedule.cron, moment)
    with connection(db_path) as conn:
        conn.execute(
            """
            UPDATE schedules
               SET last_run = ?, last_status = ?, last_error = ?, next_run = ?
             WHERE id = ?
            """,
            (
                moment.strftime(TIMESTAMP),
                status,
                (error or "")[:500] or None,
                upcoming.strftime(TIMESTAMP),
                schedule_id,
            ),
        )


def is_misfire(schedule: Schedule, now: datetime | None = None) -> bool:
    """True when this run is late enough that the moment has passed."""
    moment = now or datetime.now()
    try:
        planned = datetime.strptime(schedule.next_run, TIMESTAMP)
    except ValueError:
        return False
    return moment - planned > MISFIRE_GRACE


class Scheduler:
    """Owns the poll loop and the set of currently running jobs."""

    def __init__(self, db_path: Path, runner: Any, settings: Settings | None = None) -> None:
        """``runner`` is an async callable taking the task text and returning a summary."""
        self._db_path = db_path
        self._runner = runner
        self._settings = settings
        self._running: set[int] = set()
        # Held so the event loop cannot garbage-collect a job mid-execution:
        # asyncio keeps only weak references to tasks.
        self._jobs: set[asyncio.Task[None]] = set()

    @property
    def running_ids(self) -> set[int]:
        """Schedule ids executing right now (used to prevent overlap)."""
        return set(self._running)

    async def run_forever(self, poll_seconds: float = POLL_SECONDS) -> None:
        """Poll until cancelled. One tick is one ``tick()``."""
        log.info("scheduler_started", poll_seconds=poll_seconds)
        ticks = 0
        try:
            while True:
                try:
                    await self.tick()
                    if ticks % PRUNE_EVERY_TICKS == 0:
                        await asyncio.to_thread(self._prune)
                    ticks += 1
                except Exception:
                    log.exception("scheduler_tick_failed")
                await asyncio.sleep(poll_seconds)
        except asyncio.CancelledError:
            log.info("scheduler_stopped")
            raise

    def _prune(self) -> None:
        """Drop operational events that have aged out - never audit rows.

        Housekeeping lives on the scheduler because it is the one thing in the
        system that already owns a clock.
        """
        keep_days = self._settings.app.keep_event_days if self._settings else 0
        if keep_days <= 0:
            return
        with connection(self._db_path) as conn:
            removed = prune_events(conn, keep_days)
        if removed:
            log.info("events_pruned", removed=removed, keep_days=keep_days)

    async def tick(self, now: datetime | None = None) -> list[int]:
        """Run everything due; returns the ids started this tick."""
        moment = now or datetime.now()
        started: list[int] = []
        for schedule in due(self._db_path, moment):
            if schedule.id in self._running:
                # Still going from last time. Skip the slot rather than
                # stacking a second copy on top of the first.
                log.warning("schedule_overlap_skipped", id=schedule.id, name=schedule.name)
                self._note(schedule, "skipped", "previous run was still going", moment)
                continue
            self._running.add(schedule.id)
            started.append(schedule.id)
            self._spawn(schedule, moment)
        return started

    async def run_now(self, schedule_id: int) -> bool:
        """Start a schedule immediately, ignoring its slot.

        Returns False if it is already running - "test my briefing" must not
        become a way to start three of them.
        """
        schedule = get(self._db_path, schedule_id)
        if schedule.id in self._running:
            return False
        self._running.add(schedule.id)
        # Pass the planned time, not now, so an on-demand run is never
        # mistaken for a misfire.
        self._spawn(schedule, datetime.now())
        return True

    def _spawn(self, schedule: Schedule, moment: datetime) -> None:
        """Start a job and keep a reference until it finishes."""
        job = asyncio.create_task(self._execute(schedule, moment))
        self._jobs.add(job)
        job.add_done_callback(self._jobs.discard)

    def _note(self, schedule: Schedule, status: str, error: str | None, moment: datetime) -> None:
        record_run(self._db_path, schedule.id, status, error, moment)
        with connection(self._db_path) as conn:
            append_event(
                conn,
                EventType.SCHEDULE_FIRED,
                {"id": schedule.id, "name": schedule.name, "status": status, "error": error},
            )

    async def _execute(self, schedule: Schedule, moment: datetime) -> None:
        """Run one scheduled task to completion, recording the outcome."""
        misfired = is_misfire(schedule, moment)
        if misfired:
            log.warning("schedule_misfire", id=schedule.id, planned=schedule.next_run)
        try:
            summary = await asyncio.wait_for(self._runner(schedule.task), timeout=MAX_TASK_SECONDS)
            self._note(schedule, "ok", None, moment)
            self._announce(schedule, str(summary))
        except TimeoutError:
            log.warning("schedule_timeout", id=schedule.id)
            self._note(
                schedule, "failed", f"did not finish within {int(MAX_TASK_SECONDS)}s", moment
            )
        except Exception as exc:
            log.exception("schedule_failed", id=schedule.id)
            self._note(schedule, "failed", str(exc), moment)
        finally:
            self._running.discard(schedule.id)

    def _announce(self, schedule: Schedule, summary: str) -> None:
        """Notify the user that an unattended task produced something.

        Unattended work is pointless if you never learn it happened, so this
        is part of running a schedule rather than an optional extra.
        """
        from myagent import notify

        body = summary.strip() or "Done."
        notify.send(schedule.name, body)


async def run_snapshot_task(settings: Settings, db_path: Path) -> str:
    """The nightly vault backup, as a scheduler job.

    Lives here rather than in its own loop so there is one timing mechanism.
    """
    from myagent.vault.remote import VaultUnavailableError

    try:
        entry = await asyncio.to_thread(run_snapshot_now, settings, db_path)
    except VaultUnavailableError as exc:
        raise RuntimeError(f"backup skipped: {exc}") from exc
    return f"Backup uploaded ({entry.get('size', 0)} bytes)."


def run_snapshot_now(settings: Settings, db_path: Path) -> dict[str, object]:
    """Synchronous snapshot entry used by the API endpoint and the nightly job.

    First-ever use creates the vault key; the recovery string is returned so
    the caller (API/UI) can show it to the user exactly once.
    """
    from myagent.vault import crypto, snapshot
    from myagent.vault.remote import make_vault

    vault = make_vault(settings)  # raises VaultUnavailableError if unconfigured
    key, recovery = crypto.get_or_create_key()
    entry = snapshot.run_snapshot(db_path, vault, settings.vault, key)
    if recovery is not None:
        entry["recovery_string"] = recovery
    return entry


def ensure_snapshot_schedule(db_path: Path, hour: int) -> None:
    """Make sure the nightly backup exists as a normal schedule row.

    Idempotent: it is created once and then visible, editable, and pausable in
    the task dashboard like anything else the user set up.
    """
    cron = f"0 {hour} * * *"
    for schedule in list_all(db_path):
        if schedule.task == SNAPSHOT_TASK:
            if schedule.cron != cron:
                with connection(db_path) as conn:
                    conn.execute(
                        "UPDATE schedules SET cron = ?, next_run = ? WHERE id = ?",
                        (cron, next_fire(cron).strftime(TIMESTAMP), schedule.id),
                    )
            return
    add(db_path, name="Nightly backup", cron=cron, task=SNAPSHOT_TASK)


def make_runner(loop: Any, settings: Settings, db_path: Path) -> Any:
    """Build the callable the scheduler executes for each due task.

    A scheduled task runs through the ordinary agent loop, in its own session,
    so it gets the same tools, permission checks, and audit trail as anything
    typed. The one exception is the backup, which is a fixed operation.

    The channel is ``local``: the user wrote this task themselves when creating
    the schedule, so it carries the same rights as typing it. What it does not
    get is a way around confirmation - nobody is there to answer one, so a
    destructive step (T2) or a turn that read a web page (tainted) will stall
    and be recorded as failed. That is the right failure: a schedule must not
    be a way to acquire permissions nobody approved.
    """

    async def run(task: str) -> str:
        if task == SNAPSHOT_TASK:
            return await run_snapshot_task(settings, db_path)
        session_id = loop.ensure_session(None)
        answer = ""
        async for chunk in loop.respond(session_id, task, channel="local"):
            if chunk.reset:
                answer = ""
            answer += chunk.delta
        return answer.strip()

    return run
