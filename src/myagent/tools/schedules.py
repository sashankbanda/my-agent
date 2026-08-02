"""Scheduling tools: set up recurring work by asking for it.

M5 built the scheduler and a dashboard to drive it, but no tool - so the
assistant could *run* on a timer and could not *be asked* to. "Brief me every
morning at eight" is one of the most natural things to say to an assistant,
and it landed as conversation.

These wrap ``myagent.scheduler``, so the poller, the UI, and the assistant all
mean the same thing by a schedule.

Creating one is REVERSIBLE rather than CONFIRM_ALWAYS: it is undone by
deleting it, and demanding a confirmation for "remind me every morning" would
train the habit of clicking yes. The dangerous case - a web page talking the
assistant into scheduling something - is already covered, twice: the taint
rule forces confirmation for the rest of any turn that read untrusted content,
and a scheduled task cannot answer a confirmation prompt, so anything
destructive stalls and is recorded as failed when it runs.
"""

from __future__ import annotations

from typing import Any

from myagent.logging import get_logger
from myagent.scheduler import ScheduleError, add, list_all, remove, set_enabled
from myagent.security.tiers import Tier
from myagent.tools.registry import ToolContext, ToolError, tool

log = get_logger(__name__)

CRON_HELP = (
    "Five fields: minute hour day-of-month month day-of-week. "
    "'0 8 * * *' = 8am daily. '0 9 * * 1-5' = 9am weekdays. "
    "'30 18 * * 5' = 6:30pm Fridays. '0 * * * *' = hourly."
)


@tool(
    name="schedule.add",
    tier=Tier.REVERSIBLE,
    description=(
        "Set up a recurring task: something the assistant should do on its "
        "own, on a timer. The task is written as a plain request, exactly as "
        "the user would say it. Use this for 'every morning', 'every Monday', "
        "'each evening'. " + CRON_HELP + " Only for RECURRING work - there is "
        "no one-off timer yet, so say so rather than faking one."
    ),
    params={
        "name": {"type": "string", "description": "Short label, e.g. 'Morning briefing'"},
        "cron": {"type": "string", "description": "5-field cron expression, local time"},
        "task": {
            "type": "string",
            "description": "What to do, in plain language, e.g. 'Summarise today's AI news'",
        },
    },
    required=["name", "cron", "task"],
    summarize=lambda args: (
        f"schedule {args.get('name')!r} to run '{args.get('task')}' on '{args.get('cron')}'"
    ),
)
def add_schedule(context: ToolContext, name: str, cron: str, task: str) -> dict[str, Any]:
    """Create a recurring task."""
    try:
        created = add(context.db_path, name=name, cron=cron, task=task)
    except ScheduleError as exc:
        raise ToolError(str(exc)) from exc
    log.info("schedule_tool_added", id=created.id, cron=cron)
    return {
        "created": created.id,
        "name": created.name,
        "cron": created.cron,
        "next_run": created.next_run,
    }


@tool(
    name="schedule.list",
    tier=Tier.READ,
    description=(
        "List the recurring tasks that are set up, when each next runs, and how the last run went."
    ),
)
def list_schedules(context: ToolContext) -> dict[str, Any]:
    """Every schedule, soonest first."""
    items = [
        {
            "id": item.id,
            "name": item.name,
            "cron": item.cron,
            "task": item.task,
            "enabled": item.enabled,
            "next_run": item.next_run,
            "last_status": item.last_status,
        }
        for item in list_all(context.db_path)
    ]
    return {"count": len(items), "schedules": items}


@tool(
    name="schedule.remove",
    tier=Tier.REVERSIBLE,
    description="Delete a recurring task by its id (from schedule.list).",
    params={"id": {"type": "integer", "description": "Schedule id to delete"}},
    required=["id"],
    summarize=lambda args: f"delete scheduled task {args.get('id')}",
)
def remove_schedule(context: ToolContext, id: int) -> dict[str, Any]:
    """Delete a schedule."""
    try:
        remove(context.db_path, id)
    except ScheduleError as exc:
        raise ToolError(str(exc)) from exc
    return {"deleted": id}


@tool(
    name="schedule.set_enabled",
    tier=Tier.REVERSIBLE,
    description=(
        "Pause or resume a recurring task without deleting it. Resuming "
        "recomputes the next run from now, so a task paused for a week does "
        "not wake up believing it is overdue."
    ),
    params={
        "id": {"type": "integer", "description": "Schedule id (from schedule.list)"},
        "enabled": {"type": "boolean", "description": "true to resume, false to pause"},
    },
    required=["id", "enabled"],
    summarize=lambda args: (
        f"{'resume' if args.get('enabled') else 'pause'} scheduled task {args.get('id')}"
    ),
)
def set_schedule_enabled(context: ToolContext, id: int, enabled: bool) -> dict[str, Any]:
    """Pause or resume a schedule."""
    try:
        updated = set_enabled(context.db_path, id, enabled)
    except ScheduleError as exc:
        raise ToolError(str(exc)) from exc
    return {"id": updated.id, "enabled": updated.enabled, "next_run": updated.next_run}
