"""Permission broker tests: tiers, grants, channels, taint, kill switch."""

from __future__ import annotations

import sqlite3

import pytest

from myagent.config import Settings
from myagent.security.broker import PermissionBroker, _redact
from myagent.security.taint import TurnContext
from myagent.security.tiers import Decision, Tier

TOOL = "files.delete"


@pytest.fixture
def broker(db: sqlite3.Connection, settings: Settings) -> PermissionBroker:
    return PermissionBroker(settings.db_path())


@pytest.fixture
def turn() -> TurnContext:
    return TurnContext(session_id="session-1")


def test_read_tier_always_allowed(broker: PermissionBroker, turn: TurnContext) -> None:
    decision, _ = broker.authorize("files.list_dir", Tier.READ, {}, turn)
    assert decision is Decision.ALLOW


def test_reversible_allowed_locally(broker: PermissionBroker, turn: TurnContext) -> None:
    decision, reason = broker.authorize("files.move", Tier.REVERSIBLE, {}, turn)
    assert decision is Decision.ALLOW
    assert "reversible" in reason


def test_confirm_always_tier_needs_confirmation(
    broker: PermissionBroker, turn: TurnContext
) -> None:
    decision, _ = broker.authorize(TOOL, Tier.CONFIRM_ALWAYS, {}, turn)
    assert decision is Decision.CONFIRM


def test_session_grant_allows_afterwards(broker: PermissionBroker, turn: TurnContext) -> None:
    broker.add_grant(TOOL, "session", turn.session_id)
    decision, reason = broker.authorize(TOOL, Tier.CONFIRM_ALWAYS, {}, turn)
    assert decision is Decision.ALLOW
    assert reason == "standing grant"


def test_session_grant_does_not_leak_to_other_sessions(broker: PermissionBroker) -> None:
    broker.add_grant(TOOL, "session", "session-1")
    other = TurnContext(session_id="session-2")
    decision, _ = broker.authorize(TOOL, Tier.CONFIRM_ALWAYS, {}, other)
    assert decision is Decision.CONFIRM


def test_always_grant_applies_across_sessions(broker: PermissionBroker) -> None:
    broker.add_grant(TOOL, "always", "session-1")
    other = TurnContext(session_id="session-2")
    decision, _ = broker.authorize(TOOL, Tier.CONFIRM_ALWAYS, {}, other)
    assert decision is Decision.ALLOW


def test_revoked_grant_stops_allowing(broker: PermissionBroker, turn: TurnContext) -> None:
    broker.add_grant(TOOL, "always", None)
    grant_id = broker.list_grants()[0]["id"]
    assert broker.revoke_grant(grant_id) is True
    decision, _ = broker.authorize(TOOL, Tier.CONFIRM_ALWAYS, {}, turn)
    assert decision is Decision.CONFIRM
    assert broker.revoke_grant(grant_id) is False


def test_kill_switch_denies_everything(broker: PermissionBroker, turn: TurnContext) -> None:
    broker.add_grant(TOOL, "always", None)
    broker.kill_switch.engage()
    decision, reason = broker.authorize(TOOL, Tier.CONFIRM_ALWAYS, {}, turn)
    assert decision is Decision.DENY
    assert "emergency stop" in reason
    # Even read-only work stops while the switch is engaged.
    assert broker.authorize("files.list_dir", Tier.READ, {}, turn)[0] is Decision.DENY
    broker.kill_switch.release()
    assert broker.authorize("files.list_dir", Tier.READ, {}, turn)[0] is Decision.ALLOW


def test_remote_channel_forces_confirmation_despite_grant(broker: PermissionBroker) -> None:
    """SEC-09: standing grants do not apply to remote sessions."""
    broker.add_grant("files.move", "always", None)
    remote = TurnContext(session_id="s", channel="remote")
    decision, reason = broker.authorize("files.move", Tier.REVERSIBLE, {}, remote)
    assert decision is Decision.CONFIRM
    assert "remote" in reason


def test_decisions_are_audited(
    broker: PermissionBroker, db: sqlite3.Connection, turn: TurnContext
) -> None:
    broker.authorize(TOOL, Tier.CONFIRM_ALWAYS, {"path": "x"}, turn)
    rows = [
        row["type"] for row in db.execute("SELECT type FROM events WHERE type='PermissionDecided'")
    ]
    assert rows == ["PermissionDecided"]


def test_secret_arguments_are_redacted_in_logs() -> None:
    redacted = _redact({"path": "C:/x", "password": "hunter2", "api_key": "sk-abc"})
    assert redacted["path"] == "C:/x"
    assert redacted["password"] == "***"
    assert redacted["api_key"] == "***"
