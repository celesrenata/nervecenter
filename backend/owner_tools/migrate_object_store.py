"""One-off copy of NerveCenter objects into the encrypted durable store.

Owner tool (not wired into the app). Source: the current plain S3/MinIO
store from ``NERVECENTER_OBJECT_STORE_*`` (built *without* encryption, since
existing objects are plaintext). Destination: ``--dest`` (``file:///...``)
wrapped with the keyring from ``NERVECENTER_OBJECT_STORE_ENCRYPTION_KEYS``.

Keys come from the durable plane: every ``communications.content_ref`` under
``content://mail/`` (the only writer with data; Takeout raw capture is OFF).

Idempotent: a destination object whose plaintext digest already matches is
skipped. Every copy is read back and compared before it counts as done. The
source is never modified or deleted. Output is counts only — no keys,
metadata or content are printed.

Usage (inside a pod with the NFS volume mounted)::

    python -m backend.owner_tools.migrate_object_store \
        --source-url http://<minio>:9000 --dest file:///data/objects [--verify-only]
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from collections import Counter

from sqlalchemy import text

from backend.persistence.connection import config_from_env, create_persistence
from backend.storage.config import ENV_ENCRYPTION_KEYS
from backend.storage.config import config_from_env as store_config
from backend.storage.encrypted import EncryptedObjectStore, keyring_from_env
from backend.storage.filesystem import FileObjectStore
from backend.storage.s3 import S3ObjectStore

PREFIX = "content://"


async def _keys() -> list[str]:
    engine, factory = create_persistence(config_from_env())
    try:
        async with factory() as s:
            rows = await s.execute(
                text(
                    "select distinct content_ref from communications "
                    "where content_ref like 'content://mail/%'"
                )
            )
            return sorted(r[0][len(PREFIX) :] for r in rows.all())
    finally:
        await engine.dispose()


async def run(dest_url: str, *, source_url: str, verify_only: bool) -> dict[str, int]:
    if not dest_url.startswith("file:///"):
        raise SystemExit("--dest must be an absolute file:/// URL")
    src_env = {k: v for k, v in os.environ.items() if k != ENV_ENCRYPTION_KEYS}
    src_env["NERVECENTER_OBJECT_STORE_URL"] = source_url
    src_cfg = store_config(src_env)
    if src_cfg.scheme != "s3":
        raise SystemExit("source must be the current S3/MinIO store")
    src = S3ObjectStore(src_cfg)
    dest = EncryptedObjectStore(FileObjectStore(dest_url[len("file://") :]), keyring_from_env())
    counts: Counter[str] = Counter()
    for key in await _keys():
        counts["keys"] += 1
        meta = await src.head(key)
        if meta is None:
            counts["source_missing"] += 1
            continue
        data = await src.get(key)
        digest = hashlib.sha256(data).hexdigest()
        existing = await dest.head(key) if await dest.exists(key) else None
        if existing is not None and existing.etag == digest:
            counts["already_present"] += 1
            continue
        if verify_only:
            counts["missing_at_dest"] += 1
            continue
        await dest.put(key, data, content_type=meta.content_type, metadata=meta.metadata)
        back = await dest.get(key)
        head = await dest.head(key)
        if back != data or head.metadata != meta.metadata:
            counts["verify_failed"] += 1
            continue
        counts["copied"] += 1
    return dict(sorted(counts.items()))


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--dest", required=True)
    p.add_argument("--source-url", required=True, help="current S3/MinIO URL")
    p.add_argument("--verify-only", action="store_true")
    a = p.parse_args()
    print(json.dumps(asyncio.run(run(a.dest, source_url=a.source_url, verify_only=a.verify_only))))


if __name__ == "__main__":
    main()
