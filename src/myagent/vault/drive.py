"""Google Drive adapter for the RemoteVault port.

Least privilege by construction: the ``drive.file`` scope lets this code see
and touch only files it created itself - never the rest of the user's Drive
(FR-SYNC-06). Blobs land inside one visible folder so the user can always
inspect (and even manually copy) their encrypted backups.

OAuth is a one-time interactive step (browser consent via loopback flow),
run from scripts/restore.py --auth or the first manual backup; the refresh
token then lives in the Windows Credential Manager.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import keyring
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload

from myagent.logging import get_logger
from myagent.vault.remote import BlobInfo, VaultUnavailableError

log = get_logger(__name__)

KEYRING_SERVICE = "myagent"
TOKEN_NAME = "vault_oauth_token"
SCOPES = ["https://www.googleapis.com/auth/drive.file"]
FOLDER_MIME = "application/vnd.google-apps.folder"


def _load_credentials() -> Credentials | None:
    """Cached OAuth credentials from the credential manager, refreshed if stale."""
    raw = keyring.get_password(KEYRING_SERVICE, TOKEN_NAME)
    if not raw:
        return None
    credentials = Credentials.from_authorized_user_info(json.loads(raw), SCOPES)
    if credentials.expired and credentials.refresh_token:
        credentials.refresh(Request())
        _store_credentials(credentials)
    return credentials


def _store_credentials(credentials: Credentials) -> None:
    keyring.set_password(KEYRING_SERVICE, TOKEN_NAME, credentials.to_json())


def authorize_interactively(client_secrets: Path) -> None:
    """One-time browser consent flow; stores the token in the credential manager."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    if not client_secrets or not client_secrets.exists():
        raise VaultUnavailableError(
            "Google OAuth client secrets file not found. Create an OAuth 'Desktop app' "
            "client in Google Cloud Console (APIs & Services -> Credentials), download "
            "its JSON, and point vault.client_secrets at it."
        )
    flow = InstalledAppFlow.from_client_secrets_file(str(client_secrets), SCOPES)
    credentials = flow.run_local_server(port=0)
    if not isinstance(credentials, Credentials):  # loopback flow always yields user creds
        raise VaultUnavailableError("unexpected credential type from the OAuth flow")
    _store_credentials(credentials)
    log.info("drive_authorized")


class DriveVault:
    """RemoteVault backed by one folder in the user's Google Drive."""

    def __init__(self, folder_name: str, client_secrets: Path | None = None) -> None:
        self._folder_name = folder_name
        self._client_secrets = client_secrets
        self._service: Any = None
        self._folder_id: str | None = None

    def _api(self) -> Any:
        """Lazily build the Drive service; fail with actionable guidance."""
        if self._service is None:
            credentials = _load_credentials()
            if credentials is None:
                raise VaultUnavailableError(
                    "Google Drive is not authorized yet; run: "
                    "uv run python scripts/restore.py --auth"
                )
            self._service = build("drive", "v3", credentials=credentials, cache_discovery=False)
        return self._service

    def _folder(self) -> str:
        """Find or create the vault folder; cache its id."""
        if self._folder_id is None:
            api = self._api()
            query = (
                f"name = '{self._folder_name}' and mimeType = '{FOLDER_MIME}' and trashed = false"
            )
            found = api.files().list(q=query, fields="files(id)").execute().get("files", [])
            if found:
                self._folder_id = found[0]["id"]
            else:
                created = (
                    api.files()
                    .create(body={"name": self._folder_name, "mimeType": FOLDER_MIME}, fields="id")
                    .execute()
                )
                self._folder_id = created["id"]
                log.info("drive_folder_created", folder=self._folder_name)
        assert self._folder_id is not None
        return self._folder_id

    def _find(self, name: str) -> dict[str, Any] | None:
        api = self._api()
        query = f"name = '{name}' and '{self._folder()}' in parents and trashed = false"
        found = api.files().list(q=query, fields="files(id, name, size)").execute()
        files = found.get("files", [])
        return files[0] if files else None

    def upload(self, name: str, data: bytes) -> None:
        try:
            api = self._api()
            media = MediaIoBaseUpload(
                io.BytesIO(data), mimetype="application/octet-stream", resumable=True
            )
            existing = self._find(name)
            if existing:
                api.files().update(fileId=existing["id"], media_body=media).execute()
            else:
                api.files().create(
                    body={"name": name, "parents": [self._folder()]},
                    media_body=media,
                    fields="id",
                ).execute()
        except HttpError as exc:
            raise VaultUnavailableError(f"Drive upload failed: {exc}") from exc

    def download(self, name: str) -> bytes:
        try:
            found = self._find(name)
            if not found:
                raise VaultUnavailableError(f"blob not found on Drive: {name}")
            request = self._api().files().get_media(fileId=found["id"])
            buffer = io.BytesIO()
            downloader = MediaIoBaseDownload(buffer, request)
            done = False
            while not done:
                _, done = downloader.next_chunk()
            return buffer.getvalue()
        except HttpError as exc:
            raise VaultUnavailableError(f"Drive download failed: {exc}") from exc

    def list_blobs(self, prefix: str = "") -> list[BlobInfo]:
        try:
            api = self._api()
            query = f"'{self._folder()}' in parents and trashed = false"
            blobs: list[BlobInfo] = []
            token: str | None = None
            while True:
                page = (
                    api.files()
                    .list(q=query, fields="nextPageToken, files(name, size)", pageToken=token)
                    .execute()
                )
                for entry in page.get("files", []):
                    if entry["name"].startswith(prefix):
                        blobs.append(BlobInfo(name=entry["name"], size=int(entry.get("size", 0))))
                token = page.get("nextPageToken")
                if not token:
                    break
            blobs.sort(key=lambda blob: blob.name)
            return blobs
        except HttpError as exc:
            raise VaultUnavailableError(f"Drive listing failed: {exc}") from exc

    def delete(self, name: str) -> None:
        try:
            found = self._find(name)
            if found:
                self._api().files().delete(fileId=found["id"]).execute()
        except HttpError as exc:
            raise VaultUnavailableError(f"Drive delete failed: {exc}") from exc
