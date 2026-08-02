"""Schedules API: create, list, pause, delete, run-now, notification setup."""

from __future__ import annotations

from myagent.config import Settings
from tests.test_chat_api import make_client


class TestScheduleCrud:
    def test_create_and_list(self, settings: Settings) -> None:
        with make_client(settings, {}) as client:
            created = client.post(
                "/schedules",
                json={"name": "Briefing", "cron": "0 8 * * *", "task": "brief me"},
            )
            assert created.status_code == 200
            body = created.json()
            assert body["name"] == "Briefing"
            assert body["next_run"]

            listed = client.get("/schedules").json()
            assert [item["id"] for item in listed] == [body["id"]]

    def test_an_invalid_cron_is_rejected_with_a_reason(self, settings: Settings) -> None:
        """Better a 400 now than a schedule that silently never fires."""
        with make_client(settings, {}) as client:
            response = client.post(
                "/schedules", json={"name": "Broken", "cron": "nonsense", "task": "x"}
            )
            assert response.status_code == 400
            assert "cron" in response.json()["detail"]
            assert client.get("/schedules").json() == []

    def test_an_empty_task_is_rejected(self, settings: Settings) -> None:
        with make_client(settings, {}) as client:
            response = client.post(
                "/schedules", json={"name": "Empty", "cron": "0 8 * * *", "task": ""}
            )
            assert response.status_code == 422

    def test_pause_and_resume(self, settings: Settings) -> None:
        with make_client(settings, {}) as client:
            created = client.post(
                "/schedules", json={"name": "T", "cron": "0 8 * * *", "task": "x"}
            ).json()

            paused = client.patch(f"/schedules/{created['id']}", json={"enabled": False})
            assert paused.json()["enabled"] is False

            resumed = client.patch(f"/schedules/{created['id']}", json={"enabled": True})
            assert resumed.json()["enabled"] is True

    def test_delete(self, settings: Settings) -> None:
        with make_client(settings, {}) as client:
            created = client.post(
                "/schedules", json={"name": "T", "cron": "0 8 * * *", "task": "x"}
            ).json()
            assert client.delete(f"/schedules/{created['id']}").status_code == 200
            assert client.get("/schedules").json() == []

    def test_unknown_ids_are_404_not_500(self, settings: Settings) -> None:
        with make_client(settings, {}) as client:
            assert client.delete("/schedules/999").status_code == 404
            assert client.patch("/schedules/999", json={"enabled": True}).status_code == 404


class TestRunNow:
    def test_running_a_task_on_demand(self, settings: Settings) -> None:
        """Otherwise the only way to test a morning briefing is to wait for morning."""
        with make_client(settings, {}) as client:
            created = client.post(
                "/schedules", json={"name": "T", "cron": "0 8 * * *", "task": "x"}
            ).json()
            response = client.post(f"/schedules/{created['id']}/run")
            assert response.status_code == 200
            assert response.json()["started"] is True

    def test_running_an_unknown_schedule_is_404(self, settings: Settings) -> None:
        with make_client(settings, {}) as client:
            assert client.post("/schedules/999/run").status_code == 404


class TestNotificationApi:
    def test_the_topic_is_never_returned(self, settings: Settings) -> None:
        """A topic name is the only credential ntfy has; do not echo it."""
        with make_client(settings, {}) as client:
            client.post("/notify/topic", json={"topic": "my-secret-topic"})
            body = client.get("/notify/topic").json()

            assert body == {"configured": True}
            assert "my-secret-topic" not in str(body)

    def test_push_is_off_until_configured(self, settings: Settings) -> None:
        with make_client(settings, {}) as client:
            assert client.get("/notify/topic").json() == {"configured": False}

    def test_sending_reports_what_was_delivered(self, settings: Settings) -> None:
        with make_client(settings, {}) as client:
            body = client.post(
                "/notify", json={"title": "Test", "body": "hello", "push": False}
            ).json()

            assert set(body) == {"toast", "push", "delivered", "errors"}
            assert body["push"] is False  # push explicitly disabled for this call
