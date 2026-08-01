"""Path allowlisting: the filesystem's outer wall.

Every path a tool touches passes ``resolve_allowed``, which fully resolves it
(following ``..``, symlinks, and Windows short names) and then requires the
result to sit inside a configured root. Traversal attempts fail closed with a
message that names the roots, so the model can correct itself.

Roots default to the user's own document folders - never the whole drive.
"""

from __future__ import annotations

import os
from pathlib import Path

from myagent.tools.registry import ToolError

DEFAULT_ROOT_NAMES = ("Desktop", "Documents", "Downloads", "Pictures", "Music", "Videos")


def default_roots() -> list[Path]:
    """The user folders permitted out of the box."""
    home = Path.home()
    roots = [home / name for name in DEFAULT_ROOT_NAMES if (home / name).exists()]
    return roots or [home]


def configured_roots(settings: object) -> list[Path]:
    """Roots from settings, falling back to the defaults."""
    tools = getattr(settings, "tools", None)
    configured = getattr(tools, "roots", None) if tools is not None else None
    if not configured:
        return default_roots()
    return [Path(os.path.expandvars(str(root))).expanduser() for root in configured]


def resolve_allowed(candidate: str | Path, roots: list[Path], must_exist: bool = False) -> Path:
    """Resolve ``candidate`` and require it to live inside one of ``roots``.

    ``strict=False`` resolution means a not-yet-existing target (a move
    destination) still gets fully normalized before the containment check -
    the check must never depend on whether the attacker's path exists.
    """
    resolved = Path(os.path.expandvars(str(candidate))).expanduser().resolve()
    for root in roots:
        root_resolved = root.expanduser().resolve()
        if resolved == root_resolved or resolved.is_relative_to(root_resolved):
            if must_exist and not resolved.exists():
                raise ToolError(f"path does not exist: {resolved}")
            return resolved
    allowed = ", ".join(str(root) for root in roots)
    raise ToolError(
        f"path is outside the permitted folders: {resolved}. Permitted roots: {allowed}"
    )
