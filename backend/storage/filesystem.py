"""Filesystem object store for a mounted durable volume (e.g. NFS).

Implements :class:`~backend.storage.contracts.ObjectStore` over a directory:

* ``<root>/<key>.obj`` holds the payload and ``<root>/<key>.meta`` a small
  JSON record (size, etag, content type, timestamps, caller metadata). The
  meta file is written last, so its presence means the object exists.
* Writes go to a unique temp file in the same directory, are ``fsync``-ed and
  then atomically renamed, so a reader never sees a partial object and a
  crash leaves at most an orphaned temp file.
* Keys are strict: ``/``-separated segments of ``[A-Za-z0-9._-]``, no empty,
  ``.`` or ``..`` segment, no leading ``/``, bounded length. A key can never
  escape ``root``.
* Directories are created ``0700`` and files ``0600``.

This backend stores bytes as given. Pair it with
:class:`~backend.storage.encrypted.EncryptedObjectStore` (the factory always
does for ``file://``) so the volume only ever holds ciphertext.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path

from .contracts import MetadataMap, ObjectMetadata, validate_key, validate_metadata
from .errors import ObjectNotFoundError

_SEGMENT = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
MAX_KEY_LENGTH = 512


def _check_key(key: str) -> list[str]:
    key = validate_key(key)
    if len(key) > MAX_KEY_LENGTH or key.startswith("/"):
        raise ValueError("invalid object key")
    parts = key.split("/")
    if any(p in ("", ".", "..") or not _SEGMENT.fullmatch(p) for p in parts):
        raise ValueError("invalid object key")
    return parts


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


class FileObjectStore:
    def __init__(self, root: str | os.PathLike[str]) -> None:
        self._root = Path(root).resolve()
        if not self._root.is_dir():
            raise ValueError("object store root is not a directory")

    def _paths(self, key: str) -> tuple[Path, Path]:
        parts = _check_key(key)
        base = self._root.joinpath(*parts)
        if self._root not in base.parents:  # defence in depth
            raise ValueError("invalid object key")
        return base.with_name(base.name + ".obj"), base.with_name(base.name + ".meta")

    def _ensure_dirs(self, directory: Path) -> None:
        """Create every missing directory under root with mode 0700.

        ``Path.mkdir(parents=True)`` applies ``mode`` only to the last
        component, so intermediate parents would get the default (0755).
        """
        missing: list[Path] = []
        current = directory
        while current != self._root and not current.exists():
            missing.append(current)
            current = current.parent
        for d in reversed(missing):
            try:
                d.mkdir(mode=0o700)
            except FileExistsError:
                pass
            os.chmod(d, 0o700)

    def _atomic_write(self, path: Path, data: bytes) -> None:
        self._ensure_dirs(path.parent)
        tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

    def _read_meta(self, meta_path: Path, key: str) -> ObjectMetadata | None:
        try:
            raw = json.loads(meta_path.read_bytes())
        except FileNotFoundError:
            return None
        return ObjectMetadata(
            key=key,
            size=int(raw["size"]),
            etag=str(raw["etag"]),
            content_type=raw.get("content_type"),
            created_at=_dt(raw["created_at"]),
            updated_at=_dt(raw["updated_at"]),
            metadata=dict(raw.get("metadata") or {}),
        )

    def _put_sync(
        self, key: str, payload: bytes, content_type: str | None, metadata: dict[str, str]
    ) -> ObjectMetadata:
        obj_path, meta_path = self._paths(key)
        now = datetime.now(UTC)
        existing = self._read_meta(meta_path, key)
        created = existing.created_at if existing else now
        meta = ObjectMetadata(
            key=key,
            size=len(payload),
            etag=hashlib.sha256(payload).hexdigest(),
            content_type=content_type,
            created_at=created,
            updated_at=now,
            metadata=metadata,
        )
        self._atomic_write(obj_path, payload)
        record = {
            "size": meta.size,
            "etag": meta.etag,
            "content_type": content_type,
            "created_at": _iso(created),
            "updated_at": _iso(now),
            "metadata": metadata,
        }
        self._atomic_write(meta_path, json.dumps(record, sort_keys=True).encode())
        return meta

    async def put(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str | None = None,
        metadata: MetadataMap | None = None,
    ) -> ObjectMetadata:
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise TypeError("object payload must be bytes, bytearray, or memoryview")
        stored = validate_metadata(metadata)
        return await asyncio.to_thread(self._put_sync, key, bytes(data), content_type, stored)

    def _get_sync(self, key: str) -> bytes:
        obj_path, meta_path = self._paths(key)
        if not meta_path.exists():
            raise ObjectNotFoundError(key)
        try:
            return obj_path.read_bytes()
        except FileNotFoundError:
            raise ObjectNotFoundError(key) from None

    async def get(self, key: str) -> bytes:
        return await asyncio.to_thread(self._get_sync, key)

    def _delete_sync(self, key: str) -> bool:
        obj_path, meta_path = self._paths(key)
        existed = meta_path.exists()
        meta_path.unlink(missing_ok=True)
        obj_path.unlink(missing_ok=True)
        return existed

    async def delete(self, key: str) -> bool:
        return await asyncio.to_thread(self._delete_sync, key)

    async def exists(self, key: str) -> bool:
        _obj, meta_path = self._paths(key)
        return await asyncio.to_thread(meta_path.exists)

    async def head(self, key: str) -> ObjectMetadata | None:
        _obj, meta_path = self._paths(key)
        return await asyncio.to_thread(self._read_meta, meta_path, key)


__all__ = ["MAX_KEY_LENGTH", "FileObjectStore"]
