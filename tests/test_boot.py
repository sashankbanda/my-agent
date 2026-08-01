"""Boot tests: the app starts, serves /health, and logs lifecycle events."""

from __future__ import annotations

from fastapi.testclient import TestClient

import myagent
from myagent.config import Settings
from myagent.db import connection
from myagent.server.app import create_app


def test_health_returns_ok(settings: Settings) -> None:
    app = create_app(settings)
    with TestClient(app) as client:
        response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["version"] == myagent.__version__


def test_boot_writes_lifecycle_events(settings: Settings) -> None:
    app = create_app(settings)
    with TestClient(app) as client:
        client.get("/health")
    with connection(settings.db_path()) as conn:
        types = [row["type"] for row in conn.execute("SELECT type FROM events ORDER BY id")]
    assert types == ["AppStarted", "AppStopping"]


def test_boot_creates_database_in_data_dir(settings: Settings) -> None:
    app = create_app(settings)
    with TestClient(app) as client:
        client.get("/health")
    assert settings.db_path().exists()
