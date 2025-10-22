from __future__ import annotations

import os


class AESGCM:
    """Minimal stub for cryptography.hazmat.primitives.ciphers.aead.AESGCM.

    This implementation does *not* provide real cryptographic guarantees. It is
    only present to satisfy module imports in offline CI/test environments.
    The behaviour mirrors a deterministic XOR cipher so that encrypt-decrypt
    cycles remain reversible for tests that rely on the helper utilities.
    """

    def __init__(self, key: bytes):
        if not isinstance(key, (bytes, bytearray)):
            raise TypeError("key must be bytes-like")
        if len(key) == 0:
            raise ValueError("key must not be empty")
        self._key = bytes(key)

    def _keystream(self, nonce: bytes, length: int) -> bytes:
        seed = nonce + self._key
        data = bytearray()
        while len(data) < length:
            seed = seed[1:] + seed[:1]
            data.extend(seed)
        return bytes(data[:length])

    def encrypt(self, nonce: bytes, data: bytes, associated_data: bytes | None = None) -> bytes:
        if not isinstance(data, (bytes, bytearray)):
            raise TypeError("data must be bytes-like")
        if not isinstance(nonce, (bytes, bytearray)):
            raise TypeError("nonce must be bytes-like")
        keystream = self._keystream(bytes(nonce), len(data))
        ciphertext = bytes(a ^ b for a, b in zip(data, keystream))
        tag = b"stub-tag"
        if associated_data:
            tag += b"-" + bytes(associated_data)
        return ciphertext + tag

    def decrypt(self, nonce: bytes, data: bytes, associated_data: bytes | None = None) -> bytes:
        if not isinstance(data, (bytes, bytearray)):
            raise TypeError("data must be bytes-like")
        if len(data) < len(b"stub-tag"):
            raise ValueError("ciphertext truncated")
        payload = data[:-len(b"stub-tag")]
        keystream = self._keystream(bytes(nonce), len(payload))
        plaintext = bytes(a ^ b for a, b in zip(payload, keystream))
        return plaintext


__all__ = ["AESGCM"]
