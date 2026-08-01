"""Finding installed Windows applications by the name a person would say.

``shutil.which`` only sees PATH, which almost no GUI app is on - "chrome",
"spotify", and "premiere" all fail there. Windows records installed programs
in two other places, and this module reads both:

1. **App Paths registry** - the mechanism behind Win+R: HKCU/HKLM
   ``SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\App Paths\\<name>.exe``
2. **Start Menu shortcuts** - every installed app puts a ``.lnk`` here, and
   the shortcut itself is launchable, so no COM parsing is needed.

Lookup is name-insensitive and tolerant of partial names ("visual studio
code" finds "Visual Studio Code.lnk"), because that is how people speak.
"""

from __future__ import annotations

import os
import shutil
import winreg
from functools import lru_cache
from pathlib import Path

APP_PATHS_KEY = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"

# Spoken name -> the executable Windows actually knows, for apps whose
# registry/shortcut name differs from what people call them.
ALIASES = {
    "explorer": "explorer.exe",
    "file explorer": "explorer.exe",
    "files": "explorer.exe",
    "chrome": "chrome.exe",
    "google chrome": "chrome.exe",
    "edge": "msedge.exe",
    "microsoft edge": "msedge.exe",
    "firefox": "firefox.exe",
    "brave": "brave.exe",
    "opera": "opera.exe",
    "notepad": "notepad.exe",
    "calculator": "calc.exe",
    "calc": "calc.exe",
    "paint": "mspaint.exe",
    "task manager": "taskmgr.exe",
    "cmd": "cmd.exe",
    "terminal": "wt.exe",
    "settings": "ms-settings:",
    "vs code": "code",
    "vscode": "code",
    "visual studio code": "code",
    "word": "winword.exe",
    "excel": "excel.exe",
    "powerpoint": "powerpnt.exe",
    "outlook": "outlook.exe",
    "camera": "microsoft.windows.camera:",
    "photos": "ms-photos:",
    "store": "ms-windows-store:",
    "microsoft store": "ms-windows-store:",
}

# Names that mean "whatever the user's default browser is" rather than a
# specific program. Hardcoding Edge here was wrong: it opened a browser the
# user does not use, which reads as the assistant ignoring the request.
BROWSER_WORDS = frozenset({"browser", "web browser", "the browser", "internet", "web"})

HTTP_USER_CHOICE = r"SOFTWARE\Microsoft\Windows\Shell\Associations\UrlAssociations\http\UserChoice"


def _registry_app_path(name: str) -> Path | None:
    """Look up an executable in the App Paths registry (Win+R's index)."""
    exe = name if name.lower().endswith(".exe") else f"{name}.exe"
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(hive, rf"{APP_PATHS_KEY}\{exe}") as key:
                value, _ = winreg.QueryValueEx(key, "")
        except OSError:
            continue
        path = Path(str(value).strip('"'))
        if path.exists():
            return path
    return None


@lru_cache(maxsize=1)
def _start_menu_shortcuts() -> dict[str, Path]:
    """Map lowercase shortcut names to .lnk paths from both Start Menus."""
    roots: list[Path] = []
    for variable in ("APPDATA", "PROGRAMDATA"):
        base = os.environ.get(variable)
        if base:
            roots.append(Path(base) / "Microsoft" / "Windows" / "Start Menu" / "Programs")
    shortcuts: dict[str, Path] = {}
    for root in roots:
        if not root.exists():
            continue
        for link in root.rglob("*.lnk"):
            shortcuts.setdefault(link.stem.lower(), link)
    return shortcuts


def _shortcut_for(name: str) -> Path | None:
    """Exact then partial match against Start Menu shortcut names."""
    shortcuts = _start_menu_shortcuts()
    wanted = name.lower().removesuffix(".exe")
    if wanted in shortcuts:
        return shortcuts[wanted]
    partial = [path for stem, path in shortcuts.items() if wanted in stem]
    if partial:
        # Shortest name wins: "code" should prefer "Visual Studio Code" over
        # "Visual Studio Code Insiders".
        return min(partial, key=lambda path: len(path.stem))
    return None


def default_browser() -> str | None:
    """The executable Windows uses for http links, or None if unreadable.

    Read from the UserChoice association rather than assumed, so "open my
    browser" opens the browser the user actually chose.
    """
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, HTTP_USER_CHOICE) as key:
            prog_id, _ = winreg.QueryValueEx(key, "ProgId")
    except OSError:
        return None
    try:
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, rf"{prog_id}\shell\open\command") as key:
            command, _ = winreg.QueryValueEx(key, "")
    except OSError:
        return None
    # The command is a template: "C:\...\chrome.exe" --single-argument %1
    text = str(command).strip()
    quoted = text.startswith('"')
    executable = text[1:].split('"', 1)[0] if quoted else text.split(" ", 1)[0]
    return executable if Path(executable).exists() else None


def find_application(name: str) -> tuple[str, str] | None:
    """Resolve a spoken app name to something launchable.

    Returns ``(kind, target)`` where kind is ``exe`` (run directly),
    ``shortcut`` (hand to the shell), or ``uri`` (protocol like ms-settings:),
    or None when nothing matches.
    """
    spoken = name.strip().lower()
    if spoken in BROWSER_WORDS:
        browser = default_browser()
        if browser is not None:
            return ("exe", browser)
    resolved = ALIASES.get(spoken, name.strip())
    if resolved.endswith(":"):  # protocol handler, e.g. ms-settings:
        return ("uri", resolved)

    on_path = shutil.which(resolved)
    if on_path:
        return ("exe", on_path)

    registry_hit = _registry_app_path(resolved)
    if registry_hit is not None:
        return ("exe", str(registry_hit))

    shortcut = _shortcut_for(resolved)
    if shortcut is not None:
        return ("shortcut", str(shortcut))

    if resolved != name.strip():  # an alias was used; try the spoken form too
        shortcut = _shortcut_for(name)
        if shortcut is not None:
            return ("shortcut", str(shortcut))
    return None


def list_known_applications(limit: int = 60) -> list[str]:
    """Installed app names (for "what can you open?" and error messages)."""
    return sorted(path.stem for path in _start_menu_shortcuts().values())[:limit]
