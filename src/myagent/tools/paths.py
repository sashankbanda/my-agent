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


def _candidates(candidate: str, roots: list[Path]) -> list[Path]:
    """Interpretations of a path argument, most literal first.

    A bare name like ``Downloads`` or ``Downloads/tax`` is what a person (and
    therefore the model) naturally says. Absolute paths are only ever taken
    literally; a relative name is tried:

    1. as given (relative to the process directory),
    2. under the home directory - this is what makes "Downloads" mean
       ``~/Downloads``, i.e. the permitted root itself rather than a
       nonexistent ``Desktop/Downloads``,
    3. under each permitted root, for names *inside* a root ("notes.txt").
    """
    expanded = os.path.expandvars(candidate)
    literal = Path(expanded).expanduser()
    if literal.is_absolute():
        return [literal]
    return [literal, Path.home() / expanded, *(root.expanduser() / expanded for root in roots)]


def resolve_allowed(candidate: str | Path, roots: list[Path], must_exist: bool = False) -> Path:
    """Resolve ``candidate`` and require it to live inside one of ``roots``.

    Resolution is non-strict so a not-yet-existing target (a move
    destination) is still fully normalized before the containment check - the
    check must never depend on whether an attacker's path exists.
    """
    inside: list[Path] = []
    for option in _candidates(str(candidate), roots):
        resolved = option.resolve()
        for root in roots:
            root_resolved = root.expanduser().resolve()
            if resolved == root_resolved or resolved.is_relative_to(root_resolved):
                if not must_exist or resolved.exists():
                    return resolved
                inside.append(resolved)  # permitted, but absent: keep looking
                break

    allowed = ", ".join(str(root) for root in roots)
    if inside:
        raise ToolError(f"path does not exist: {inside[0]}")
    raise ToolError(f"'{candidate}' is outside the permitted folders. Permitted roots: {allowed}")
