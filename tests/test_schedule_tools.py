"""Scheduling by asking, not only by clicking.

M5 built the scheduler and a dashboard, but no tool - so "brief me every
morning at eight", one of the most natural things to say to an assistant,
landed as conversation and did nothing.
"""

from __future__ import annotations

import sqlite3

import pytest

from myagent.config import Settings
from myagent.core import complexity
from myagent.security.taint import TurnContext
from myagent.security.tiers import Tier
from myagent.tools import registry, schedules
from myagent.tools.registry import ToolContext, ToolError

registry.load_builtin_tools()


@pytest.fixture
def context(settings: Settings, db: sqlite3.Connection) -> ToolContext:
    return ToolContext(
        turn=TurnContext(session_id="s"), db_path=settings.db_path(), settings=settings
    )


class TestSchedulingTools:
    def test_a_recurring_task_can_be_created(self, context: ToolContext) -> None:
        result = schedules.add_schedule(
            context, name="Morning briefing", cron="0 8 * * *", task="summarise the news"
        )
        assert result["created"] > 0
        assert result["next_run"]

    def test_it_shows_up_in_the_listing(self, context: ToolContext) -> None:
        schedules.add_schedule(context, name="Briefing", cron="0 8 * * *", task="news")
        listed = schedules.list_schedules(context)

        assert listed["count"] == 1
        assert listed["schedules"][0]["name"] == "Briefing"

    def test_an_invalid_cron_is_an_honest_error(self, context: ToolContext) -> None:
        """The model gets a message it can act on, not a crash."""
        with pytest.raises(ToolError, match="cron"):
            schedules.add_schedule(context, name="Broken", cron="every morning", task="news")

    def test_pause_and_resume(self, context: ToolContext) -> None:
        created = schedules.add_schedule(context, name="T", cron="0 8 * * *", task="x")
        paused = schedules.set_schedule_enabled(context, id=created["created"], enabled=False)
        assert paused["enabled"] is False

        resumed = schedules.set_schedule_enabled(context, id=created["created"], enabled=True)
        assert resumed["enabled"] is True

    def test_delete(self, context: ToolContext) -> None:
        created = schedules.add_schedule(context, name="T", cron="0 8 * * *", task="x")
        schedules.remove_schedule(context, id=created["created"])
        assert schedules.list_schedules(context)["count"] == 0

    def test_deleting_something_absent_is_reported(self, context: ToolContext) -> None:
        with pytest.raises(ToolError, match="no schedule"):
            schedules.remove_schedule(context, id=999)


class TestTiers:
    def test_listing_is_read_only(self) -> None:
        assert registry.get_tool("schedule.list").tier is Tier.READ

    def test_creating_is_reversible_not_a_confirmation_prompt(self) -> None:
        """Undone by deleting it, and confirming "remind me daily" trains
        people to click yes. The injection case is covered by taint instead."""
        assert registry.get_tool("schedule.add").tier is Tier.REVERSIBLE

    def test_the_confirmation_text_names_what_will_run(self) -> None:
        """When taint or a remote session does force a prompt, it must be concrete."""
        summary = registry.get_tool("schedule.add").summary(
            {"name": "Briefing", "cron": "0 8 * * *", "task": "summarise the news"}
        )
        assert "0 8 * * *" in summary
        assert "summarise the news" in summary


class TestRouting:
    @pytest.mark.parametrize(
        "text",
        [
            "schedule a briefing every morning",
            "remind me every day at 8 to stretch",
            "set up a recurring backup",
            "what tasks are scheduled",
            "list my scheduled tasks",
            "what reminders do i have",
        ],
    )
    def test_scheduling_requests_reach_a_tool_capable_model(self, text: str) -> None:
        """A 3B would discuss the schedule rather than create it."""
        routing = complexity.classify(text, context_chars=1500)
        assert routing.use_local is False
        assert routing.needs_tool is True
