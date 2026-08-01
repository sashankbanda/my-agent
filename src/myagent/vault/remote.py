"""The RemoteVault port and its simplest adapter.

``RemoteVault`` is the seam that keeps blob storage vendor-portable (TC-03):
snapshot/restore code depends on this protocol only. ``FolderVault`` backs it
with a local directory - used by tests, and a legitimate production choice
for backing up to a NAS or synced folder without any cloud account.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from myagent.config import Settings


class VaultUnavailableError(Exception):
    """The vault backend is disabled, unconfigured, or unreachable."""


@dataclass
class BlobInfo:
    """One stored blob, as listed by a vault backend."""

    name: str
    size: int


class RemoteVault(Protocol):
    """What snapshot/restore need from any blob storage backend."""

    def upload(self, name: str, data: bytes) -> None: ...

    def download(self, name: str) -> bytes: ...

    def list_blobs(self, prefix: str = "") -> list[BlobInfo]: ...

    def delete(self, name: str) -> None: ...


class FolderVault:
    """RemoteVault backed by a plain directory."""

    def __init__(self, root: Path) -> None:
        self._root = root
        self._root.mkdir(parents=True, exist_ok=True)

    def _path(self, name: str) -> Path:
        # Blob names may contain '/' separators; keep them inside the root.
        path = (self._root / name).resolve()
        if not path.is_relative_to(self._root.resolve()):
            raise ValueError(f"blob name escapes the vault root: {name}")
        return path

    def upload(self, name: str, data: bytes) -> None:
        path = self._path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def download(self, name: str) -> bytes:
        path = self._path(name)
        if not path.exists():
            raise VaultUnavailableError(f"blob not found: {name}")
        return path.read_bytes()

    def list_blobs(self, prefix: str = "") -> list[BlobInfo]:
        blobs: list[BlobInfo] = []
        for path in self._root.rglob("*"):
            if not path.is_file():
                continue
            name = path.relative_to(self._root).as_posix()
            if name.startswith(prefix):
                blobs.append(BlobInfo(name=name, size=path.stat().st_size))
        blobs.sort(key=lambda blob: blob.name)
        return blobs

    def delete(self, name: str) -> None:
        path = self._path(name)
        if path.exists():
            path.unlink()


def make_vault(settings: Settings) -> RemoteVault:
    """Build the configured vault backend, or raise VaultUnavailableError.

    The Drive adapter is imported lazily: its google dependencies are heavy
    and only needed when the drive backend is actually selected.
    """
    vault = settings.vault
    if not vault.enabled:
        raise VaultUnavailableError("vault is disabled (vault.enabled: false)")
    if vault.backend == "folder":
        if vault.local_path is None:
            raise VaultUnavailableError("vault.backend is 'folder' but vault.local_path is unset")
        return FolderVault(vault.local_path)
    from myagent.vault.drive import DriveVault

    return DriveVault(folder_name=vault.folder_name, client_secrets=vault.client_secrets)
