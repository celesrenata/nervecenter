"""Owner tool: seed the SHADOW conversation ledger from recent Gmail headers (NS-ADR-067).

Owner approval: "your job is to read my email, you can read sent emails as
well". For each enabled Gmail connection this lists recent INBOX and SENT
messages (never drafts) and fetches HEADERS ONLY (``format=metadata``:
Message-ID, From, To, Cc, Reply-To, References, In-Reply-To). Each message is
recorded oldest-first with ``assess_inbound=False``: it becomes conversation
memory, it is never judged, and nothing is queued. Only fingerprints and
registrable domains are stored; no subject, body, local part or address.

Run inside the api pod (it reuses the stored access token; run it right after
a successful poll so the token is fresh)::

    python -m backend.owner_tools.seed_thread_ledger --days 180 --max 2000

Idempotent: already-recorded messages are skipped. Prints counts only.
"""
from __future__ import annotations

import argparse
import asyncio
from typing import Any

import httpx

_HEADERS = ("Message-ID", "From", "To", "Cc", "Reply-To", "References", "In-Reply-To")
_API = "https://gmail.googleapis.com/gmail/v1/users/me"


async def _list_ids(client: httpx.AsyncClient, auth: dict[str, str], query: str,
                    limit: int) -> list[str]:
    ids: list[str] = []
    token: str | None = None
    while len(ids) < limit:
        params: dict[str, Any] = {"q": query, "maxResults": min(500, limit - len(ids))}
        if token:
            params["pageToken"] = token
        r = await client.get(f"{_API}/messages", headers=auth, params=params)
        r.raise_for_status()
        body = r.json()
        ids.extend(str(m["id"]) for m in body.get("messages") or [] if m.get("id"))
        token = body.get("nextPageToken")
        if not token:
            break
    return ids


async def _seed_connection(conn: Any, reg: Any, repo: Any, key: bytes, *, days: int,
                           limit: int) -> dict[str, int]:
    from backend.connectors.mail.mail_transport import is_outgoing_labels
    from backend.ingest.mail_worker import security_account_scope
    from backend.mail_security.thread_ledger import account_salt, observe

    token = await reg.connections.get_secret(
        kind="access_token", owner_kind="connection", owner_id=conn.id
    )
    auth = {"Authorization": f"Bearer {token}"}
    scope = security_account_scope(conn.id)
    salt = account_salt(key, scope)
    counts = {"listed": 0, "recorded": 0, "skipped": 0, "errors": 0}
    async with httpx.AsyncClient(timeout=15) as client:
        ids = await _list_ids(
            client, auth, f"(in:inbox OR in:sent) -in:drafts newer_than:{days}d", limit
        )
        counts["listed"] = len(ids)
        for mid in reversed(ids):  # oldest first so replies find their parents
            r = await client.get(
                f"{_API}/messages/{mid}",
                headers=auth,
                params=[("format", "metadata"), *(("metadataHeaders", h) for h in _HEADERS)],
            )
            if r.status_code == 401:
                raise SystemExit("access token expired: run after a successful poll")
            if r.status_code != 200:
                counts["errors"] += 1
                continue
            body = r.json()
            labels = tuple(body.get("labelIds") or ())
            if "DRAFT" in labels:
                counts["skipped"] += 1
                continue
            headers = {
                str(h.get("name")): str(h.get("value", ""))
                for h in (body.get("payload") or {}).get("headers") or []
            }
            observed = observe(headers, subject="", outgoing=is_outgoing_labels(labels))
            if observed is None:
                counts["skipped"] += 1
                continue
            before = await repo.status_counts(scope)
            await repo.record(scope, salt, observed, assess_inbound=False)
            after = await repo.status_counts(scope)
            counts["recorded" if sum(after.values()) > sum(before.values()) else "skipped"] += 1
    return counts


async def main(days: int, limit: int) -> None:
    import backend.api.app  # noqa: F401 - import-order guard
    from backend.api.wiring import _load_secret_codec
    from backend.mail_security.thread_ledger import ledger_key_from_env
    from backend.persistence.connection import create_engine, make_session_factory
    from backend.persistence.repositories import RepositoryRegistry
    from backend.persistence.repositories.mail_security_thread_repo import (
        MailThreadLedgerRepository,
    )

    key = ledger_key_from_env()
    if key is None:
        raise SystemExit("NERVE_MAIL_SECURITY_LEDGER_KEY is not set or invalid")
    engine = create_engine()
    try:
        reg = RepositoryRegistry(make_session_factory(engine), secret_codec=_load_secret_codec())
        repo = MailThreadLedgerRepository(reg.session_factory)
        for conn in await reg.connections.list_connections():
            if conn.kind != "gmail" or not conn.enabled:
                continue
            counts = await _seed_connection(conn, reg, repo, key, days=days, limit=limit)
            print("conn", conn.id[:8], counts, await repo.status_counts(f"conn:{conn.id}"))
    finally:
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--days", type=int, default=180)
    parser.add_argument("--max", type=int, default=2000, dest="limit")
    args = parser.parse_args()
    asyncio.run(main(max(1, args.days), max(1, min(args.limit, 20000))))
