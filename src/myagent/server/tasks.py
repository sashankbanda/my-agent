"""Schedules API: create, list, pause, delete, and run-now.

Deliberately thin - all the behaviour lives in ``myagent.scheduler``, so the
poller and the UI cannot disagree about what a schedule means.

``POST /schedules/{id}/run`` exists because the alternative is waiting until
tomorrow morning to find out whether your morning briefing works.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from myagent import notify, scheduler
from myagent.logging import get_logger

log = get_logger(__name__)

router = APIRouter()


class ScheduleBody(BaseModel):
    """POST /schedules request body."""

    name: str = Field(min_length=1, max_length=120)
    cron: str = Field(min_length=1, max_length=120)
    task: str = Field(min_length=1, max_length=2_000)
    enabled: bool = True


class EnabledBody(BaseModel):
    """PATCH /schedules/{id} request body."""

    enabled: bool


class NotifyBody(BaseModel):
    """POST /notify request body."""

    title: str = Field(min_length=1, max_length=120)
    body: str = Field(default="", max_length=2_000)
    push: bool = True


class TopicBody(BaseModel):
    """POST /notify/topic request body."""

    topic: str = Field(min_length=1, max_length=120)


@router.get("/schedules")
async def list_schedules(request: Request) -> list[dict[str, Any]]:
    """Every schedule, soonest first."""
    db_path = request.app.state.loop.db_path
    running = _running_ids(request)
    return [
        {**item.as_dict(), "running": item.id in running} for item in scheduler.list_all(db_path)
    ]


@router.post("/schedules")
async def create_schedule(body: ScheduleBody, request: Request) -> dict[str, Any]:
    """Create a schedule. Rejects an invalid cron rather than never firing."""
    db_path = request.app.state.loop.db_path
    try:
        created = scheduler.add(db_path, body.name, body.cron, body.task, body.enabled)
    except scheduler.ScheduleError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return created.as_dict()


@router.patch("/schedules/{schedule_id}")
async def update_schedule(schedule_id: int, body: EnabledBody, request: Request) -> dict[str, Any]:
    """Pause or resume a schedule."""
    db_path = request.app.state.loop.db_path
    try:
        return scheduler.set_enabled(db_path, schedule_id, body.enabled).as_dict()
    except scheduler.ScheduleError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.delete("/schedules/{schedule_id}")
async def delete_schedule(schedule_id: int, request: Request) -> dict[str, Any]:
    """Delete a schedule."""
    db_path = request.app.state.loop.db_path
    try:
        scheduler.remove(db_path, schedule_id)
    except scheduler.ScheduleError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"deleted": schedule_id}


@router.post("/schedules/{schedule_id}/run")
async def run_schedule(schedule_id: int, request: Request) -> dict[str, Any]:
    """Run a schedule immediately, without waiting for its slot.

    Returns as soon as the task is started: a briefing can take a minute, and
    an HTTP request should not hold a connection open for it. Watch the
    activity feed, or re-read the schedule for its outcome.
    """
    engine: scheduler.Scheduler | None = getattr(request.app.state, "scheduler", None)
    if engine is None:
        raise HTTPException(status_code=503, detail="the scheduler is not running")
    db_path = request.app.state.loop.db_path
    try:
        item = scheduler.get(db_path, schedule_id)
    except scheduler.ScheduleError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if item.id in engine.running_ids:
        raise HTTPException(status_code=409, detail="that task is already running")
    started = await engine.run_now(schedule_id)
    return {"started": started, "id": schedule_id}


@router.post("/notify")
async def send_notification(body: NotifyBody, request: Request) -> dict[str, Any]:
    """Send a notification now (used to verify toast and phone push work)."""
    delivery = await asyncio.to_thread(notify.send, body.title, body.body, "default", body.push)
    return delivery.as_dict()


@router.get("/notify/topic")
async def get_topic() -> dict[str, Any]:
    """Whether phone push is configured. The topic itself is never returned.

    A topic name is the only credential ntfy has - anyone holding it can read
    your notifications - so it goes into the credential store and stays there.
    """
    return {"configured": notify.ntfy_topic() is not None}


@router.post("/notify/topic")
async def set_topic(body: TopicBody) -> dict[str, Any]:
    """Store the ntfy topic for phone push."""
    notify.set_ntfy_topic(body.topic)
    return {"configured": True}


def _running_ids(request: Request) -> set[int]:
    engine: scheduler.Scheduler | None = getattr(request.app.state, "scheduler", None)
    return engine.running_ids if engine is not None else set()
