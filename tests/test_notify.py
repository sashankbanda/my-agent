"""Notification tests: both transports, and never taking the task down.

The property that matters most is negative: ``send`` must not raise. A
notification is the *report* on a task, and a report that crashes the thing it
reports on is worse than no report.
"""

from __future__ import annotations

from typing import Any

import pytest

from myagent import notify
from myagent.server.tasks import NotifyBody


class FakeResponse:
    def __init__(self, status: int = 200) -> None:
        self.status = status

    def raise_for_status(self) -> None:
        if self.status >= 400:
            raise RuntimeError(f"HTTP {self.status}")


class TestDelivery:
    def test_both_transports_are_attempted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        posted: dict[str, Any] = {}
        toasted: list[tuple[str, str]] = []

        monkeypatch.setattr(notify, "ntfy_topic", lambda: "secret-topic")
        monkeypatch.setattr(notify, "send_toast", lambda title, body: toasted.append((title, body)))
        monkeypatch.setattr(
            notify.httpx,
            "post",
            lambda url, **kwargs: posted.update({"url": url, **kwargs}) or FakeResponse(),
        )

        result = notify.send("Briefing", "Three headlines today.")

        assert result.toast is True and result.push is True
        assert toasted == [("Briefing", "Three headlines today.")]
        assert posted["url"] == "https://ntfy.sh/secret-topic"
        assert posted["content"] == b"Three headlines today."

    def test_push_is_skipped_when_no_topic_is_configured(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Phone push is off until the user opts in - it is a public server."""
        called = False

        def should_not_run(*_args: Any, **_kwargs: Any) -> None:
            nonlocal called
            called = True

        monkeypatch.setattr(notify, "ntfy_topic", lambda: None)
        monkeypatch.setattr(notify, "send_toast", lambda title, body: None)
        monkeypatch.setattr(notify.httpx, "post", should_not_run)

        result = notify.send("Briefing", "text")

        assert result.push is False
        assert called is False

    def test_a_failing_toast_does_not_stop_the_push(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(notify, "ntfy_topic", lambda: "topic")
        monkeypatch.setattr(
            notify, "send_toast", lambda title, body: (_ for _ in ()).throw(OSError("no COM"))
        )
        monkeypatch.setattr(notify.httpx, "post", lambda url, **kwargs: FakeResponse())

        result = notify.send("Briefing", "text")

        assert result.toast is False
        assert result.push is True
        assert result.delivered is True
        assert any("toast" in error for error in result.errors or [])

    def test_a_total_failure_is_reported_not_raised(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The task that produced the notification must still be recorded as ok."""
        monkeypatch.setattr(notify, "ntfy_topic", lambda: "topic")
        monkeypatch.setattr(
            notify, "send_toast", lambda title, body: (_ for _ in ()).throw(OSError("no COM"))
        )
        monkeypatch.setattr(
            notify.httpx,
            "post",
            lambda url, **kwargs: (_ for _ in ()).throw(OSError("network down")),
        )

        result = notify.send("Briefing", "text")

        assert result.delivered is False
        assert len(result.errors or []) == 2

    def test_a_server_error_counts_as_a_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(notify, "ntfy_topic", lambda: "topic")
        monkeypatch.setattr(notify, "send_toast", lambda title, body: None)
        monkeypatch.setattr(notify.httpx, "post", lambda url, **kwargs: FakeResponse(503))

        assert notify.send("Briefing", "text").push is False

    def test_long_bodies_are_truncated_before_sending(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Both transports truncate anyway; do it ourselves so it is predictable."""
        captured: dict[str, Any] = {}
        monkeypatch.setattr(notify, "ntfy_topic", lambda: "topic")
        monkeypatch.setattr(notify, "send_toast", lambda title, body: None)
        monkeypatch.setattr(
            notify.httpx, "post", lambda url, **kwargs: captured.update(kwargs) or FakeResponse()
        )

        notify.send("Briefing", "x" * 5_000)

        assert len(captured["content"]) == notify.MAX_BODY_CHARS

    def test_a_non_ascii_title_does_not_break_the_header(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """HTTP headers are latin-1; an emoji title must not raise mid-send."""
        captured: dict[str, Any] = {}
        monkeypatch.setattr(notify, "ntfy_topic", lambda: "topic")
        monkeypatch.setattr(notify, "send_toast", lambda title, body: None)
        monkeypatch.setattr(
            notify.httpx, "post", lambda url, **kwargs: captured.update(kwargs) or FakeResponse()
        )

        result = notify.send("Briefing ✅", "text")

        assert result.push is True
        captured["headers"]["Title"].encode("ascii")  # would raise if unescaped


class TestTopicStorage:
    def test_the_topic_round_trips_through_the_credential_store(self) -> None:
        notify.set_ntfy_topic("  my-private-topic  ")
        assert notify.ntfy_topic() == "my-private-topic"

    def test_an_empty_topic_is_refused(self) -> None:
        with pytest.raises(ValueError, match="empty"):
            notify.set_ntfy_topic("   ")

    def test_unset_means_off(self) -> None:
        assert notify.ntfy_topic() is None


class TestApiShape:
    def test_a_notification_needs_a_title(self) -> None:
        with pytest.raises(ValueError):
            NotifyBody(title="", body="text")
