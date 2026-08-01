"""Tool registry: declare a capability with one decorator.

    @tool(
        name="files.list_dir",
        tier=Tier.READ,
        description="List entries in a directory.",
        params={"path": {"type": "string", "description": "Directory to list"}},
        required=["path"],
    )
    def list_dir(context: ToolContext, path: str) -> dict[str, Any]:
        ...

The registry exposes JSON-Schema specs for the model's tool-calling API and
looks tools up by name for execution. It holds no policy - the broker decides
what may run, and the executor enforces it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from myagent.security.taint import TurnContext
from myagent.security.tiers import Tier


class ToolError(Exception):
    """A tool failed in a way the model should see and may retry around.

    Tool errors are *observations*, not crashes: the loop feeds them back to
    the model so it can adapt (retry, pick another path, or report honestly).
    """


@dataclass
class ToolContext:
    """What a tool gets besides its own arguments."""

    turn: TurnContext
    db_path: Path
    settings: Any  # myagent.config.Settings (untyped here to avoid a cycle)


# A tool implementation takes a ToolContext plus its own declared keyword
# arguments, so the parameter list varies per tool; only the first parameter
# and the return type are fixed. Callable[..., ...] is the honest type here -
# a Protocol with **kwargs would reject every real (named-parameter) tool.
ToolFunction = Callable[..., dict[str, Any]]


@dataclass
class ToolSpec:
    """One registered tool: its metadata, schema, and implementation."""

    name: str
    tier: Tier
    description: str
    params: dict[str, Any]
    required: list[str]
    func: ToolFunction
    summarize: Callable[[dict[str, Any]], str] | None = None

    def schema(self) -> dict[str, Any]:
        """OpenAI-compatible function-tool schema for the model."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": self.params,
                    "required": self.required,
                },
            },
        }

    def summary(self, args: dict[str, Any]) -> str:
        """Concrete one-line description of what this call will do.

        Used in confirmation prompts, so it must name real paths/commands
        rather than paraphrasing (SEC-02).
        """
        if self.summarize is not None:
            return self.summarize(args)
        rendered = ", ".join(f"{key}={value!r}" for key, value in args.items())
        return f"{self.name}({rendered})"


_REGISTRY: dict[str, ToolSpec] = {}


def tool(
    name: str,
    tier: Tier,
    description: str,
    params: dict[str, Any] | None = None,
    required: list[str] | None = None,
    summarize: Callable[[dict[str, Any]], str] | None = None,
) -> Callable[[ToolFunction], ToolFunction]:
    """Register a function as a tool. Returns the function unchanged."""

    def decorator(func: ToolFunction) -> ToolFunction:
        if name in _REGISTRY:
            raise ValueError(f"tool already registered: {name}")
        _REGISTRY[name] = ToolSpec(
            name=name,
            tier=tier,
            description=description,
            params=params or {},
            required=required or [],
            func=func,
            summarize=summarize,
        )
        return func

    return decorator


def get_tool(name: str) -> ToolSpec:
    """Look up a tool; raises ToolError so a bad model call is recoverable."""
    try:
        return _REGISTRY[name]
    except KeyError as exc:
        raise ToolError(f"unknown tool: {name}") from exc


def all_tools() -> list[ToolSpec]:
    """Every registered tool, name-ordered."""
    return [_REGISTRY[name] for name in sorted(_REGISTRY)]


def schemas(names: list[str] | None = None) -> list[dict[str, Any]]:
    """Model-facing schemas, optionally restricted to a subset of tools."""
    specs = all_tools() if names is None else [get_tool(name) for name in names]
    return [spec.schema() for spec in specs]


def load_builtin_tools() -> None:
    """Import the built-in tool modules so their decorators run.

    Called once from the composition root. Importing is the registration
    mechanism, so this is the only place that needs updating when a new tool
    module is added.
    """
    from myagent.tools import apps, files, shell  # noqa: F401
