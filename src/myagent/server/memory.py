"""Memory and vault API: the memory viewer's backend plus manual backups.

The right-to-forget (FR from Phase 2) lives here: every fact is listable and
deletable by the user, no exceptions. Vault endpoints run snapshot work in a
thread so the event loop never blocks on network I/O.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from myagent.config import Settings
from myagent.core.loop import AgentLoop
from myagent.memory import store, tools_builtin
from myagent.scheduler_lite import run_snapshot_now
from myagent.vault.remote import VaultUnavailableError
from myagent.vault.snapshot import last_snapshot, verify_manifest_chain

router = APIRouter()


class RememberBody(BaseModel):
    """POST /memory request body."""

    content: str = Field(min_length=1)
    type: str = "fact"


class ForgetBody(BaseModel):
    """POST /memory/forget request body."""

    id: int


@router.get("/memory")
async def list_memory(request: Request) -> list[dict[str, Any]]:
    """All standing facts, newest first."""
    loop: AgentLoop = request.app.state.loop
    return store.list_facts(loop.db_path)


@router.post("/memory")
async def remember(body: RememberBody, request: Request) -> dict[str, Any]:
    """Store one fact about the user."""
    loop: AgentLoop = request.app.state.loop
    try:
        return dict(tools_builtin.remember(loop.db_path, body.content, type_=body.type))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/memory/forget")
async def forget(body: ForgetBody, request: Request) -> dict[str, Any]:
    """Delete one fact permanently."""
    loop: AgentLoop = request.app.state.loop
    try:
        return dict(tools_builtin.forget(loop.db_path, body.id))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc.args[0])) from exc


@router.post("/vault/backup")
async def backup_now(request: Request) -> dict[str, Any]:
    """Run a snapshot immediately; first use returns the recovery string once."""
    settings: Settings = request.app.state.settings
    loop: AgentLoop = request.app.state.loop
    try:
        return await asyncio.to_thread(run_snapshot_now, settings, loop.db_path)
    except VaultUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/vault/status")
async def vault_status(request: Request) -> dict[str, Any]:
    """Vault configuration state and the most recent snapshot."""
    settings: Settings = request.app.state.settings
    loop: AgentLoop = request.app.state.loop
    return {
        "enabled": settings.vault.enabled,
        "backend": settings.vault.backend,
        "last_snapshot": last_snapshot(loop.db_path),
        "manifest_chain_ok": verify_manifest_chain(loop.db_path),
    }
