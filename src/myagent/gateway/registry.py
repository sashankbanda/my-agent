"""Model registry: loads and validates ``config/providers.yaml``.

The registry is pure configuration data. Changing a provider, model, quota, or
routing order is a YAML edit - never a code change (FR-LLM-01). Validation is
eager: a routing entry pointing at an undeclared model fails at load time, not
mid-conversation.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from myagent.gateway.types import ModelSpec, ProviderSpec, TaskClass


class RegistryError(Exception):
    """providers.yaml is missing, malformed, or internally inconsistent."""


class Registry:
    """Validated view of providers, models, and routing tables."""

    def __init__(
        self,
        providers: dict[str, ProviderSpec],
        models: dict[str, ModelSpec],
        routing: dict[TaskClass, list[str]],
    ) -> None:
        self._providers = providers
        self._models = models
        self._routing = routing

    def provider(self, name: str) -> ProviderSpec:
        """Return a provider by name."""
        try:
            return self._providers[name]
        except KeyError as exc:
            raise RegistryError(f"unknown provider: {name}") from exc

    def model(self, key: str) -> ModelSpec:
        """Return a model by its ``provider/id`` key."""
        try:
            return self._models[key]
        except KeyError as exc:
            raise RegistryError(f"unknown model key: {key}") from exc

    def candidates(self, task_class: TaskClass) -> list[ModelSpec]:
        """Ranked candidate models for a task class (best first)."""
        keys = self._routing.get(task_class)
        if not keys:
            raise RegistryError(f"no routing configured for task class: {task_class}")
        return [self._models[key] for key in keys]

    @property
    def all_models(self) -> list[ModelSpec]:
        """Every declared model (used by doctor and the quota dashboard)."""
        return list(self._models.values())


def default_registry_path() -> Path:
    """Path of the checked-in provider registry."""
    return Path(__file__).resolve().parents[3] / "config" / "providers.yaml"


def load_registry(path: Path | None = None) -> Registry:
    """Parse and validate the registry file."""
    registry_path = path or default_registry_path()
    if not registry_path.exists():
        raise RegistryError(f"registry file not found: {registry_path}")
    raw: dict[str, Any] = yaml.safe_load(registry_path.read_text(encoding="utf-8")) or {}

    providers: dict[str, ProviderSpec] = {}
    for name, body in (raw.get("providers") or {}).items():
        providers[name] = ProviderSpec(name=name, **body)
    if not providers:
        raise RegistryError("registry declares no providers")

    models: dict[str, ModelSpec] = {}
    for body in raw.get("models") or []:
        quota = body.pop("quota", {})
        spec = ModelSpec(**body, **quota)
        if spec.provider not in providers:
            raise RegistryError(f"model {spec.key} references unknown provider {spec.provider}")
        spec = spec.model_copy(update={"local": providers[spec.provider].local})
        models[spec.key] = spec
    if not models:
        raise RegistryError("registry declares no models")

    routing: dict[TaskClass, list[str]] = {}
    for class_name, keys in (raw.get("routing") or {}).items():
        task_class = TaskClass(class_name)
        if not keys:
            raise RegistryError(f"routing list for {class_name} is empty")
        for key in keys:
            if key not in models:
                raise RegistryError(f"routing for {class_name} references unknown model {key}")
        routing[task_class] = list(keys)
    if not routing:
        raise RegistryError("registry declares no routing tables")

    return Registry(providers, models, routing)
