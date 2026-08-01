"""The agent loop (M1 form: conversation only).

This module owns the turn lifecycle: persist the user message, assemble the
model-facing transcript, stream the reply through the gateway, and persist
the outcome. Tools, the permission broker, and budgets attach here in M4;
cancellation attaches in M3 - per the playbook, neither exists yet.

The loop consumes the gateway's ``reset`` protocol: when a provider fails
mid-answer and the gateway restarts on the next candidate, accumulated text
is discarded so exactly the final, complete answer is persisted.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

from myagent.core import history
from myagent.db import connection
from myagent.events import EventType, append_event
from myagent.gateway.gateway import Gateway
from myagent.gateway.types import InferenceChunk, InferenceRequest, PrivacyClass, TaskClass
from myagent.logging import get_logger
from myagent.memory import context

log = get_logger(__name__)

SYSTEM_PROMPT = (
    "You are MyAgent, a personal assistant that talks like a thoughtful, direct "
    "friend. Be concise and natural; skip filler and disclaimers. If you do not "
    "know something, say so plainly. You currently have no tools and cannot take "
    "actions on the user's computer - if asked to act, say what you would do once "
    "your tools are enabled, briefly."
)


class AgentLoop:
    """Turn-by-turn conversation engine bound to one database and gateway."""

    def __init__(self, gateway: Gateway, db_path: Path) -> None:
        self._gateway = gateway
        self._db_path = db_path

    @property
    def db_path(self) -> Path:
        """Database this loop persists conversations into."""
        return self._db_path

    def ensure_session(self, session_id: str | None) -> str:
        """Return a valid session id, creating a session when needed."""
        if session_id and history.session_exists(self._db_path, session_id):
            return session_id
        return history.create_session(self._db_path)

    async def respond(
        self,
        session_id: str,
        user_text: str,
        cancel: asyncio.Event | None = None,
    ) -> AsyncIterator[InferenceChunk]:
        """Run one turn: persist, infer (with failover), stream, persist.

        Chunks are re-yielded to the caller exactly as the gateway emits them,
        including ``reset`` chunks, so UIs can mirror failover behavior.

        ``cancel`` (barge-in, M3): when set mid-stream, generation stops and
        the partial answer - what was actually delivered - is persisted.
        """
        history.append_message(self._db_path, session_id, "user", user_text)

        bundle = context.assemble(self._db_path, session_id, user_text, SYSTEM_PROMPT)
        request = InferenceRequest(
            messages=bundle.messages,
            task_class=TaskClass.CONVERSATION,
            privacy_class=(
                bundle.privacy_class if bundle.privacy_class is PrivacyClass.LOCAL_ONLY else None
            ),  # None lets the gateway's own secret scan classify the final prompt
            trace_id=session_id,
        )

        answer = ""
        model_key: str | None = None
        tokens: int | None = None
        interrupted = False
        stream = self._gateway.complete(request)
        try:
            async for chunk in stream:
                if cancel is not None and cancel.is_set():
                    interrupted = True
                    break
                if chunk.reset:
                    answer = ""
                answer += chunk.delta
                if chunk.done:
                    model_key = chunk.model_key
                    tokens = chunk.tokens
                yield chunk
        finally:
            await stream.aclose()  # releases the provider connection on early exit

        provider = model_key.split("/", 1)[0] if model_key else None
        history.append_message(
            self._db_path,
            session_id,
            "assistant",
            answer,
            provider=provider,
            model=model_key,
            tokens=tokens,
        )
        if interrupted:
            with connection(self._db_path) as conn:
                append_event(conn, EventType.TURN_INTERRUPTED, {"session": session_id}, session_id)
        log.info("turn_completed", session=session_id, model=model_key, interrupted=interrupted)
