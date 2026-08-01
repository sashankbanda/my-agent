"""Vault crypto tests: envelope round-trips, tamper detection, recovery."""

from __future__ import annotations

import os

import pytest

from myagent.vault import crypto


def test_encrypt_decrypt_round_trip() -> None:
    key = os.urandom(32)
    data = b"hello vault" * 1000
    blob = crypto.encrypt_blob(data, key)
    assert blob.startswith(crypto.MAGIC)
    assert crypto.decrypt_blob(blob, key) == data


def test_compression_is_effective() -> None:
    key = os.urandom(32)
    data = b"A" * 100_000
    blob = crypto.encrypt_blob(data, key)
    assert len(blob) < len(data) / 10


def test_wrong_key_fails_cleanly() -> None:
    blob = crypto.encrypt_blob(b"secret", os.urandom(32))
    with pytest.raises(crypto.VaultCryptoError, match="wrong key or corrupted"):
        crypto.decrypt_blob(blob, os.urandom(32))


def test_single_flipped_bit_is_detected() -> None:
    key = os.urandom(32)
    blob = bytearray(crypto.encrypt_blob(b"integrity matters", key))
    blob[len(blob) // 2] ^= 0x01
    with pytest.raises(crypto.VaultCryptoError):
        crypto.decrypt_blob(bytes(blob), key)


def test_garbage_is_rejected_as_non_envelope() -> None:
    with pytest.raises(crypto.VaultCryptoError, match="not a MyAgent vault envelope"):
        crypto.decrypt_blob(b"definitely not an envelope", os.urandom(32))


def test_recovery_string_round_trip() -> None:
    key = os.urandom(32)
    recovery = crypto._encode_recovery(key)
    assert "-" in recovery  # human-transcribable grouping
    assert crypto._decode_recovery(recovery) == key


def test_recovery_string_rejects_garbage() -> None:
    with pytest.raises(crypto.VaultCryptoError):
        crypto._decode_recovery("not-a-real-recovery-string!!!")
    with pytest.raises(crypto.VaultCryptoError, match="wrong key length"):
        crypto._decode_recovery("YWJj")  # decodes fine, but 3 bytes, not 32
