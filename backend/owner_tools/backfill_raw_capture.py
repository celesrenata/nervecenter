"""Owner tool: full-capture backfill for stored Gmail mail (NS-ADR-071).

Owner decision (2026-11-13, "do em"). For every DONE security job of an
enabled Gmail account whose job is still ``LEGACY_BODY_ONLY`` and whose
communication is INCOMING, this fetches the unmodified message from Gmail
(``format=raw``, the same 32 MiB cap as live capture), stores it as the
separate encrypted ``raw/<conn>/<digest>`` object, and re-queues the SAME job
with ``acquisition_mode=GMAIL_RAW``, the ``raw_ref`` and ``source_revision=2``.
The security runner then writes a NEW revision-2 assessment beside the
revision-1 body-only one (verdicts are immutable); every reader takes the
newest revision. ``content_ref`` (the 7.1 / 7.4 join key) is unchanged.

Dry run by default (counts only). ``--apply`` writes. ``--limit`` bounds the
number of messages. Idempotent: re-running skips jobs already moved. Run in
the api pod right after a successful poll (it reuses the stored token)::

    python -m backend.owner_tools.backfill_raw_capture
    python -m backend.owner_tools.backfill_raw_capture --apply
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import binascii
from collections import Counter
from typing import Any

import httpx

_API = "https://gmail.googleapis.com/gmail/v1/users/me"
REVISION = 2


async def _fetch_raw(client: httpx.AsyncClient, auth: dict[str, str], mid: str,
                     cap: int) -> tuple[bytes | None, str]:
    r = None
    for attempt in range(6):
        r = await client.get(f"{_API}/messages/{mid}", headers=auth, params={"format": "raw"})
        if r.status_code == 429 or r.status_code >= 500 or (
            r.status_code == 403 and "rateLimit" in r.text
        ):
            await asyncio.sleep(min(30.0, 2.0**attempt))
            continue
        break
    if r.status_code == 401:
        raise SystemExit("access token expired: run after a successful poll")
    if r.status_code != 200:
        return None, f"http_{r.status_code}"
    encoded = str(r.json().get("raw") or "")
    if not encoded or len(encoded) > (cap * 4) // 3 + 4:
        return None, "empty_or_oversized"
    try:
        raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
    except (binascii.Error, ValueError):
        return None, "bad_base64"
    if not raw or len(raw) > cap:
        return None, "empty_or_oversized"
    return raw, "ok"


async def _connection(conn: Any, reg: Any, store: Any, *, apply: bool, limit: int) -> Counter:
    from sqlalchemy import select, update

    from backend.ingest.mail_worker import _raw_content_key, security_account_scope
    from backend.mail_security.acquisition import ACQUISITION_META_KEY, RAW_REF_META_KEY
    from backend.mail_security.ingestion_seam import RAW_CAPTURE_MAX_BYTES
    from backend.mail_security.worker import SOURCE_REVISION_META_KEY
    from backend.persistence.models.mail_security import MailSecurityJobRow
    from backend.persistence.models.personal import CommunicationRow

    n: Counter = Counter()
    scope = security_account_scope(conn.id)
    account = str((conn.config or {}).get("account") or "").strip().lower()
    token = await reg.connections.get_secret(
        kind="access_token", owner_kind="connection", owner_id=conn.id
    )
    auth = {"Authorization": f"Bearer {token}"}
    async with reg.session_factory() as s:
        jobs = (
            await s.execute(
                select(MailSecurityJobRow).where(
                    MailSecurityJobRow.account_scope == scope,
                    MailSecurityJobRow.state == "DONE",
                )
            )
        ).scalars().all()
        incoming = {
            r[0]
            for r in (
                await s.execute(
                    select(CommunicationRow.content_ref).where(
                        CommunicationRow.content_ref.startswith(
                            f"content://mail/{conn.id}/", autoescape=True
                        ),
                        CommunicationRow.direction == "incoming",
                    )
                )
            ).all()
        }
    todo = [
        j
        for j in jobs
        if (j.meta or {}).get(ACQUISITION_META_KEY) == "LEGACY_BODY_ONLY"
        and (j.meta or {}).get("content_ref") in incoming
    ]
    n["eligible"] = len(todo)
    if not apply:
        return n
    async with httpx.AsyncClient(timeout=30) as client:
        for job in todo[:limit]:
            meta = dict(job.meta or {})
            body_key = str(meta["content_ref"]).removeprefix("content://")
            head = await store.head(body_key)
            mail_id = str(((head and head.metadata) or {}).get("message_id", ""))
            if not mail_id.startswith("gmail:"):
                n["no_message_id"] += 1
                continue
            raw, why = await _fetch_raw(client, auth, mail_id.removeprefix("gmail:"),
                                        RAW_CAPTURE_MAX_BYTES)
            if raw is None:
                n[f"skip_{why}"] += 1
                continue
            key = _raw_content_key(conn.id, mail_id)
            stored = await store.put(
                key, raw, content_type="message/rfc822",
                metadata={"account": account, "message_id": mail_id},
            )
            meta[ACQUISITION_META_KEY] = "GMAIL_RAW"
            meta[RAW_REF_META_KEY] = f"content://{getattr(stored, 'key', key)}"
            meta[SOURCE_REVISION_META_KEY] = REVISION
            async with reg.session_factory() as s:
                res = await s.execute(
                    update(MailSecurityJobRow)
                    .where(
                        MailSecurityJobRow.id == job.id,
                        MailSecurityJobRow.account_scope == scope,
                        MailSecurityJobRow.state == "DONE",
                    )
                    .values(meta=meta, state="PENDING", lease_owner=None,
                            lease_expires_at=None, last_error_code=None)
                )
                await s.commit()
            n["requeued" if res.rowcount else "raced"] += 1
    return n


async def main(apply: bool, limit: int) -> None:
    import backend.api.app  # noqa: F401 - import-order guard
    from backend.api.wiring import _load_secret_codec
    from backend.persistence.connection import create_engine, make_session_factory
    from backend.persistence.repositories import RepositoryRegistry
    from backend.storage.config import create_object_store_from_env

    engine = create_engine()
    try:
        reg = RepositoryRegistry(make_session_factory(engine), secret_codec=_load_secret_codec())
        store = create_object_store_from_env()
        for conn in await reg.connections.list_connections():
            if conn.kind != "gmail" or not conn.enabled:
                continue
            counts = await _connection(conn, reg, store, apply=apply, limit=limit)
            print("conn", conn.id[:8], dict(counts))
        if not apply:
            print("dry run: nothing written")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--limit", type=int, default=5000)
    args = parser.parse_args()
    asyncio.run(main(args.apply, max(1, args.limit)))
