"""Client-side encryption for any object store backend.

:class:`EncryptedObjectStore` wraps an :class:`ObjectStore` so the backend
(NFS volume, MinIO, S3) only ever holds ciphertext:

* **AES-256-GCM** per object with a fresh 96-bit nonce. The object key is
  the associated data, so a ciphertext copied or renamed to another key fails
  to decrypt.
* **Metadata is encrypted too.** Caller metadata (e.g. the mailbox account
  and message id the mail worker records) and the content type travel inside
  one encrypted envelope stored under a single backend metadata entry; the
  backend sees only ``nse-v``, ``nse-kid`` and ``nse-meta``.
* **Keyring with rotation.** ``NERVECENTER_OBJECT_STORE_ENCRYPTION_KEYS`` is
  ``kid:base64key[,kid:base64key...]``; the first key encrypts, every listed
  key can decrypt. Keys must be exactly 32 bytes.
* **Fail closed.** Reading an object without the envelope header, with an
  unknown key id, or that fails authentication raises
  :class:`ObjectDecryptionError`; there is no plaintext fallback.

``size`` and ``etag`` reported to callers describe the plaintext.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .config import ENV_ENCRYPTION_KEYS  # noqa: E402  (single source of truth)
from .contracts import MetadataMap, ObjectMetadata, ObjectStore, validate_key, validate_metadata
from .errors import ObjectStoreConfigError, ObjectStoreError

FORMAT = b"NSE1"
_KID_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
_META_FIELD = "nse-meta"
_KID_FIELD = "nse-kid"
_VER_FIELD = "nse-v"


class ObjectDecryptionError(ObjectStoreError):
    """Stored object could not be authenticated/decrypted (never plaintext)."""


@dataclass(frozen=True)
class Keyring:
    active: str
    keys: Mapping[str, bytes]

    def aead(self, kid: str) -> AESGCM:
        key = self.keys.get(kid)
        if key is None:
            raise ObjectDecryptionError("unknown key id")
        return AESGCM(key)


def keyring_from_value(raw: str) -> Keyring:
    entries = [e.strip() for e in (raw or "").split(",") if e.strip()]
    if not entries:
        raise ObjectStoreConfigError(
            f"{ENV_ENCRYPTION_KEYS} is empty", missing=(ENV_ENCRYPTION_KEYS,)
        )
    keys: dict[str, bytes] = {}
    order: list[str] = []
    for entry in entries:
        kid, sep, b64 = entry.partition(":")
        if not sep or not _KID_RE.fullmatch(kid) or kid in keys:
            raise ObjectStoreConfigError(f"{ENV_ENCRYPTION_KEYS} has a malformed entry")
        try:
            key = base64.b64decode(b64, validate=True)
        except (ValueError, base64.binascii.Error):
            raise ObjectStoreConfigError(f"{ENV_ENCRYPTION_KEYS} has a malformed key") from None
        if len(key) != 32:
            raise ObjectStoreConfigError(f"{ENV_ENCRYPTION_KEYS} keys must be 32 bytes")
        keys[kid] = key
        order.append(kid)
    return Keyring(active=order[0], keys=keys)


def keyring_from_env(env: Mapping[str, str] | None = None) -> Keyring:
    source = env if env is not None else os.environ
    return keyring_from_value(source.get(ENV_ENCRYPTION_KEYS, ""))


def _seal(aead: AESGCM, data: bytes, aad: bytes) -> bytes:
    nonce = os.urandom(12)
    return nonce + aead.encrypt(nonce, data, aad)


def _open(aead: AESGCM, blob: bytes, aad: bytes) -> bytes:
    if len(blob) < 12 + 16:
        raise ObjectDecryptionError("ciphertext too short")
    try:
        return aead.decrypt(blob[:12], blob[12:], aad)
    except Exception:  # noqa: BLE001 - InvalidTag and friends: never leak detail
        raise ObjectDecryptionError("authentication failed") from None


class EncryptedObjectStore:
    def __init__(self, inner: ObjectStore, keyring: Keyring) -> None:
        self._inner = inner
        self._keys = keyring

    @staticmethod
    def _aad(key: str, part: bytes) -> bytes:
        return FORMAT + b"\x00" + part + b"\x00" + key.encode()

    def _unwrap_meta(self, key: str, inner_meta: ObjectMetadata) -> tuple[str, dict]:
        md = inner_meta.metadata or {}
        if md.get(_VER_FIELD) != FORMAT.decode() or _META_FIELD not in md:
            raise ObjectDecryptionError("object is not encrypted")
        kid = md.get(_KID_FIELD, "")
        try:
            blob = base64.b64decode(md[_META_FIELD], validate=True)
        except (ValueError, base64.binascii.Error):
            raise ObjectDecryptionError("malformed metadata envelope") from None
        record = json.loads(_open(self._keys.aead(kid), blob, self._aad(key, b"meta")))
        return kid, record

    async def put(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str | None = None,
        metadata: MetadataMap | None = None,
    ) -> ObjectMetadata:
        key = validate_key(key)
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise TypeError("object payload must be bytes, bytearray, or memoryview")
        plain = bytes(data)
        user_meta = validate_metadata(metadata)
        kid = self._keys.active
        aead = self._keys.aead(kid)
        record = {
            "content_type": content_type,
            "metadata": user_meta,
            "size": len(plain),
            "etag": hashlib.sha256(plain).hexdigest(),
        }
        sealed_meta = _seal(
            aead, json.dumps(record, sort_keys=True).encode(), self._aad(key, b"meta")
        )
        body = FORMAT + _seal(aead, plain, self._aad(key, b"body"))
        inner = await self._inner.put(
            key,
            body,
            content_type="application/octet-stream",
            metadata={
                _VER_FIELD: FORMAT.decode(),
                _KID_FIELD: kid,
                _META_FIELD: base64.b64encode(sealed_meta).decode(),
            },
        )
        return ObjectMetadata(
            key=key,
            size=record["size"],
            etag=record["etag"],
            content_type=content_type,
            created_at=inner.created_at,
            updated_at=inner.updated_at,
            metadata=dict(user_meta),
        )

    async def get(self, key: str) -> bytes:
        key = validate_key(key)
        inner_meta = await self._inner.head(key)
        blob = await self._inner.get(key)
        if inner_meta is None or not blob.startswith(FORMAT):
            raise ObjectDecryptionError("object is not encrypted")
        kid, _record = self._unwrap_meta(key, inner_meta)
        return _open(self._keys.aead(kid), blob[len(FORMAT) :], self._aad(key, b"body"))

    async def head(self, key: str) -> ObjectMetadata | None:
        key = validate_key(key)
        inner_meta = await self._inner.head(key)
        if inner_meta is None:
            return None
        _kid, record = self._unwrap_meta(key, inner_meta)
        return ObjectMetadata(
            key=key,
            size=int(record["size"]),
            etag=str(record["etag"]),
            content_type=record.get("content_type"),
            created_at=inner_meta.created_at,
            updated_at=inner_meta.updated_at,
            metadata=dict(record.get("metadata") or {}),
        )

    async def exists(self, key: str) -> bool:
        return await self._inner.exists(key)

    async def delete(self, key: str) -> bool:
        return await self._inner.delete(key)


__all__ = [
    "ENV_ENCRYPTION_KEYS",
    "EncryptedObjectStore",
    "Keyring",
    "ObjectDecryptionError",
    "keyring_from_env",
    "keyring_from_value",
]
