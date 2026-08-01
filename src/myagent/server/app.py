"""FastAPI application factory.

The app's lifespan owns kernel startup and shutdown: it migrates the database
and writes the lifecycle events, so any way of running the app (uvicorn,
tests) boots the same way. Route modules from later milestones are mounted
here and nowhere else.

Tests may inject a pre-built ``AgentLoop`` (with a fake gateway) via the
``loop`` parameter; production wiring builds the real gateway stack.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

import myagent
from myagent.config import Settings
from myagent.core.loop import AgentLoop
from myagent.db import connection, migrate
from myagent.events import EventType, append_event
from myagent.gateway.client import ProviderClientPool
from myagent.gateway.gateway import Gateway
from myagent.gateway.health import HealthTracker
from myagent.gateway.quota import QuotaGovernor
from myagent.gateway.registry import load_registry
from myagent.logging import get_logger
from myagent.scheduler_lite import nightly_snapshots
from myagent.server import chat, memory, voice_ws

log = get_logger(__name__)

UI_DIST = Path(__file__).resolve().parents[3] / "ui" / "dist"


def build_loop(settings: Settings) -> AgentLoop:
    """Production wiring: registry -> gateway -> loop, all on one database."""
    db_path = settings.db_path()
    registry = load_registry()
    gateway = Gateway(
        registry=registry,
        quota=QuotaGovernor(db_path),
        health=HealthTracker(db_path),
        client=ProviderClientPool(registry),
        db_path=db_path,
    )
    return AgentLoop(gateway, db_path)


def create_app(settings: Settings, loop: AgentLoop | None = None) -> FastAPI:
    """Build the FastAPI app for the given settings."""

    @asynccontextmanager
    async def lifespan(app_: FastAPI) -> AsyncIterator[None]:
        db_path = settings.db_path()
        with connection(db_path) as conn:
            applied = migrate(conn)
            append_event(conn, EventType.APP_STARTED, {"version": myagent.__version__})
        app_.state.loop = loop if loop is not None else build_loop(settings)
        snapshot_task = None
        if settings.vault.enabled:
            snapshot_task = asyncio.create_task(nightly_snapshots(settings, db_path))
        log.info("kernel_started", db=str(db_path), migrations_applied=applied)
        yield
        if snapshot_task is not None:
            snapshot_task.cancel()
        with connection(db_path) as conn:
            append_event(conn, EventType.APP_STOPPING)
        log.info("kernel_stopping")

    app = FastAPI(title=settings.app.name, version=myagent.__version__, lifespan=lifespan)
    app.state.settings = settings
    app.include_router(chat.router)
    app.include_router(memory.router)
    app.include_router(voice_ws.router)

    @app.get("/health")
    async def health() -> dict[str, str]:
        """Liveness probe: the kernel is up and serving."""
        return {"status": "ok", "version": myagent.__version__}

    if UI_DIST.exists():
        app.mount("/", StaticFiles(directory=UI_DIST, html=True), name="ui")

    return app
