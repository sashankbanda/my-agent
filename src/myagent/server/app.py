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
from myagent.bus import broadcaster
from myagent.config import Settings
from myagent.core.loop import AgentLoop
from myagent.db import connection, migrate
from myagent.events import EventType, append_event
from myagent.gateway.client import ProviderClientPool
from myagent.gateway.gateway import Gateway
from myagent.gateway.health import HealthTracker
from myagent.gateway.quota import QuotaGovernor
from myagent.gateway.registry import load_registry
from myagent.gateway.warmup import warm_local_models
from myagent.logging import get_logger
from myagent.scheduler_lite import nightly_snapshots
from myagent.security.broker import PermissionBroker
from myagent.security.confirm import ConfirmationService
from myagent.server import chat, control, events_ws, memory, security, voice_ws
from myagent.tools.executor import ToolExecutor
from myagent.tools.registry import load_builtin_tools

log = get_logger(__name__)

UI_DIST = Path(__file__).resolve().parents[3] / "ui" / "dist"


def build_kernel(settings: Settings) -> tuple[AgentLoop, PermissionBroker, ConfirmationService]:
    """Production wiring: gateway + security + tools -> loop, one database.

    Assembly order matters: tools must be registered before the loop is built
    (it advertises their schemas), and the executor must exist before the loop
    can act - a loop with tools but no broker is exactly what M4 forbids.
    """
    db_path = settings.db_path()
    provider_registry = load_registry()
    gateway = Gateway(
        registry=provider_registry,
        quota=QuotaGovernor(db_path),
        health=HealthTracker(db_path),
        client=ProviderClientPool(provider_registry),
        db_path=db_path,
    )
    load_builtin_tools()
    broker = PermissionBroker(db_path)
    confirmations = ConfirmationService()
    executor = ToolExecutor(db_path, settings, broker, confirmations)
    loop = AgentLoop(
        gateway,
        db_path,
        executor=executor,
        max_steps=settings.tools.max_steps_per_turn,
        max_seconds=settings.tools.max_turn_seconds,
        fast_path=settings.tools.fast_path,
        local_tier=settings.tools.local_tier,
    )
    return loop, broker, confirmations


def create_app(
    settings: Settings,
    loop: AgentLoop | None = None,
    broker: PermissionBroker | None = None,
    confirmations: ConfirmationService | None = None,
) -> FastAPI:
    """Build the FastAPI app for the given settings.

    Tests inject a pre-built loop (with a fake gateway) plus its broker and
    confirmation service; production builds them from settings.
    """

    @asynccontextmanager
    async def lifespan(app_: FastAPI) -> AsyncIterator[None]:
        broadcaster.bind_loop()  # live UI feed publishes onto this loop
        app_.state.voice_connected = False
        app_.state.voice_state = "offline"
        app_.state.voice_muted = False
        app_.state.voice_link = control.VoiceLink()
        app_.state.turns = control.TurnRegistry()
        db_path = settings.db_path()
        with connection(db_path) as conn:
            applied = migrate(conn)
            append_event(conn, EventType.APP_STARTED, {"version": myagent.__version__})
        if loop is not None:
            app_.state.loop = loop
            app_.state.broker = broker or PermissionBroker(db_path)
            app_.state.confirmations = confirmations or ConfirmationService()
        else:
            built_loop, built_broker, built_confirmations = build_kernel(settings)
            app_.state.loop = built_loop
            app_.state.broker = built_broker
            app_.state.confirmations = built_confirmations
        snapshot_task = None
        if settings.vault.enabled:
            snapshot_task = asyncio.create_task(nightly_snapshots(settings, db_path))
        warm_task = None
        if settings.tools.local_tier and loop is None:
            # Load the on-device model now so the first easy question is fast.
            warm_task = asyncio.create_task(warm_local_models(load_registry()))
        log.info("kernel_started", db=str(db_path), migrations_applied=applied)
        yield
        for task in (snapshot_task, warm_task):
            if task is not None:
                task.cancel()
        with connection(db_path) as conn:
            append_event(conn, EventType.APP_STOPPING)
        log.info("kernel_stopping")

    app = FastAPI(title=settings.app.name, version=myagent.__version__, lifespan=lifespan)
    app.state.settings = settings
    app.include_router(chat.router)
    app.include_router(control.router)
    app.include_router(memory.router)
    app.include_router(voice_ws.router)
    app.include_router(security.router)
    app.include_router(events_ws.router)

    @app.get("/health")
    async def health() -> dict[str, str]:
        """Liveness probe: the kernel is up and serving."""
        return {"status": "ok", "version": myagent.__version__}

    if UI_DIST.exists():
        app.mount("/", StaticFiles(directory=UI_DIST, html=True), name="ui")

    return app
