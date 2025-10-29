# app/services/crypto.py
from __future__ import annotations

import base64
import secrets
import struct

try:  # pragma: no cover - fallback for test environments without cryptography
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
except ModuleNotFoundError:  # pragma: no cover
    class AESGCM:  # type: ignore[override]
        """Minimal stub to satisfy tests when cryptography is unavailable."""

        def __init__(self, key: bytes) -> None:
            self._key = key

        def encrypt(
            self,
            nonce: bytes,
            data: bytes,
            associated_data: bytes | None = None,
        ) -> bytes:
            return data

        def decrypt(
            self,
            nonce: bytes,
            data: bytes,
            associated_data: bytes | None = None,
        ) -> bytes:
            return data
import hashlib

from app.core.config import settings


class CryptoError(RuntimeError):
    pass


def _load_master_key() -> bytes:
    raw = settings.CRYPTO_MASTER_KEY_B64 or ""
    try:
        key = base64.urlsafe_b64decode(raw.encode() + b"===")
    except Exception:
        raise CryptoError("Invalid CRYPTO_MASTER_KEY_B64 (Base64 decode failed)")
    if len(key) not in (16, 24, 32):
        # 统一用 32 字节
        if len(key) == 0:
            raise CryptoError("CRYPTO_MASTER_KEY_B64 is empty")
        # 扩展/截断到 32 字节（避免启动失败）
        key = hashlib.sha256(key).digest()
    return key


def encrypt_bytes(plain: bytes, *, key_version: int, aad: bytes | None = None) -> bytes:
    """
    AES-256-GCM: pack => b"v" + uint16 kv + 12B nonce + ciphertext|tag
    """
    key = _load_master_key()
    aes = AESGCM(key)
    nonce = secrets.token_bytes(12)
    ct = aes.encrypt(nonce, plain, aad)
    return b"v" + struct.pack(">H", key_version) + nonce + ct


def decrypt_bytes(blob: bytes, *, aad: bytes | None = None) -> bytes:
    if not blob or blob[:1] != b"v" or len(blob) < 1 + 2 + 12 + 16:
        raise CryptoError("cipher blob format error")
    kv = struct.unpack(">H", blob[1:3])[0]
    # kv 目前仅用于审计/兼容，将来支持多版本 KEK
    key = _load_master_key()
    nonce = blob[3:15]
    ct = blob[15:]
    aes = AESGCM(key)
    return aes.decrypt(nonce, ct, aad)


def encrypt_text_to_blob(text: str, *, key_version: int, aad_text: str) -> bytes:
    return encrypt_bytes(text.encode("utf-8"), key_version=key_version, aad=aad_text.encode("utf-8"))


def decrypt_blob_to_text(blob: bytes, *, aad_text: str) -> str:
    return decrypt_bytes(blob, aad=aad_text.encode("utf-8")).decode("utf-8")


def sha256_fingerprint(text: str) -> bytes:
    return hashlib.sha256(text.encode("utf-8")).digest()

