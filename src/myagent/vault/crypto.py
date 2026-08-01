"""Vault cryptography: zstd + AES-256-GCM envelope, key custody, recovery.

The 256-bit vault key is generated locally, lives in the Windows Credential
Manager, and never travels to any remote. At creation the user receives a
recovery string (the key, dash-grouped base32) to store off-machine:
losing both the machine and the recovery string makes the vault permanently
unreadable - by design (FR-SYNC-02).

Envelope layout (versioned so the format can evolve):
    b"MAV1" | 12-byte nonce | AES-256-GCM(zstd(plaintext), aad=b"MAV1")

GCM's authentication tag doubles as integrity verification at restore time:
a flipped bit anywhere fails decryption outright.
"""

from __future__ import annotations

import base64
import os

import keyring
import zstandard
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

KEYRING_SERVICE = "myagent"
KEY_NAME = "vault_key"
MAGIC = b"MAV1"
NONCE_SIZE = 12
KEY_SIZE = 32
RECOVERY_GROUP = 8  # dash-grouped for human transcription


class VaultCryptoError(Exception):
    """Bad envelope, wrong key, or corrupted ciphertext."""


def _encode_recovery(key: bytes) -> str:
    # base32: case-insensitive, no characters that collide with the dash
    # separators - built for human transcription.
    raw = base64.b32encode(key).decode().rstrip("=")
    return "-".join(raw[i : i + RECOVERY_GROUP] for i in range(0, len(raw), RECOVERY_GROUP))


def _decode_recovery(recovery: str) -> bytes:
    compact = recovery.replace("-", "").replace(" ", "").strip().upper()
    padded = compact + "=" * (-len(compact) % 8)
    try:
        key = base64.b32decode(padded)
    except (ValueError, TypeError) as exc:
        raise VaultCryptoError("recovery string is not valid") from exc
    if len(key) != KEY_SIZE:
        raise VaultCryptoError("recovery string decodes to the wrong key length")
    return key


def load_key() -> bytes | None:
    """The stored vault key, or None if none exists yet."""
    stored = keyring.get_password(KEYRING_SERVICE, KEY_NAME)
    return base64.b64decode(stored) if stored else None


def create_key() -> tuple[bytes, str]:
    """Generate, store, and return a new key plus its recovery string.

    Refuses to overwrite an existing key: replacing it would silently orphan
    every previous snapshot.
    """
    if load_key() is not None:
        raise VaultCryptoError("a vault key already exists; refusing to overwrite it")
    key = os.urandom(KEY_SIZE)
    keyring.set_password(KEYRING_SERVICE, KEY_NAME, base64.b64encode(key).decode())
    return key, _encode_recovery(key)


def get_or_create_key() -> tuple[bytes, str | None]:
    """Load the key, creating one on first use.

    Returns ``(key, recovery_string)`` - the recovery string is non-None only
    at creation, exactly once; callers must surface it to the user.
    """
    existing = load_key()
    if existing is not None:
        return existing, None
    return create_key()


def install_key_from_recovery(recovery: str) -> bytes:
    """Rebuild the key from a recovery string (disaster recovery on a fresh machine)."""
    key = _decode_recovery(recovery)
    keyring.set_password(KEYRING_SERVICE, KEY_NAME, base64.b64encode(key).decode())
    return key


def encrypt_blob(data: bytes, key: bytes) -> bytes:
    """Compress then encrypt; the result is safe to hand to any remote."""
    nonce = os.urandom(NONCE_SIZE)
    compressed = zstandard.ZstdCompressor().compress(data)
    ciphertext = AESGCM(key).encrypt(nonce, compressed, MAGIC)
    return MAGIC + nonce + ciphertext


def decrypt_blob(blob: bytes, key: bytes) -> bytes:
    """Verify, decrypt, and decompress an envelope."""
    if len(blob) < len(MAGIC) + NONCE_SIZE + 16 or not blob.startswith(MAGIC):
        raise VaultCryptoError("not a MyAgent vault envelope")
    nonce = blob[len(MAGIC) : len(MAGIC) + NONCE_SIZE]
    ciphertext = blob[len(MAGIC) + NONCE_SIZE :]
    try:
        compressed = AESGCM(key).decrypt(nonce, ciphertext, MAGIC)
    except InvalidTag as exc:
        raise VaultCryptoError("decryption failed: wrong key or corrupted blob") from exc
    return zstandard.ZstdDecompressor().decompress(compressed)
