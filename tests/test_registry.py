"""Registry tests: real config validation and loader error paths."""

from __future__ import annotations

from pathlib import Path

import pytest

from myagent.gateway.registry import RegistryError, default_registry_path, load_registry
from myagent.gateway.types import TaskClass


def test_checked_in_registry_is_valid() -> None:
    """The repo's own providers.yaml must always load and route every task class."""
    registry = load_registry(default_registry_path())
    for task_class in TaskClass:
        candidates = registry.candidates(task_class)
        assert candidates, f"no candidates for {task_class}"


def test_checked_in_registry_ranks_groq_first_for_conversation() -> None:
    registry = load_registry(default_registry_path())
    first = registry.candidates(TaskClass.CONVERSATION)[0]
    assert first.provider == "groq"


def test_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(RegistryError, match="not found"):
        load_registry(tmp_path / "absent.yaml")


def test_routing_to_unknown_model_fails_eagerly(tmp_path: Path) -> None:
    bad = tmp_path / "providers.yaml"
    bad.write_text(
        """
providers:
  p1: {base_url: "https://x/v1", api_key_ref: k}
models:
  - {provider: p1, id: m}
routing:
  conversation: [p1/ghost]
""",
        encoding="utf-8",
    )
    with pytest.raises(RegistryError, match="unknown model"):
        load_registry(bad)


def test_model_with_unknown_provider_fails(tmp_path: Path) -> None:
    bad = tmp_path / "providers.yaml"
    bad.write_text(
        """
providers:
  p1: {base_url: "https://x/v1", api_key_ref: k}
models:
  - {provider: p2, id: m}
routing:
  conversation: [p2/m]
""",
        encoding="utf-8",
    )
    with pytest.raises(RegistryError, match="unknown provider"):
        load_registry(bad)


def test_empty_routing_list_fails(tmp_path: Path) -> None:
    bad = tmp_path / "providers.yaml"
    bad.write_text(
        """
providers:
  p1: {base_url: "https://x/v1", api_key_ref: k}
models:
  - {provider: p1, id: m}
routing:
  conversation: []
""",
        encoding="utf-8",
    )
    with pytest.raises(RegistryError, match="empty"):
        load_registry(bad)
