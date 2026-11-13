"""Owner tool: seed per-account content campaigns from stored mail (NS-ADR-069).

For every DONE security job of an enabled mail account, this reads the stored
(encrypted) object the security worker scanned, parses it with the same
offline parser, and records its keyed MinHash campaign membership, exactly as
the live worker does after a verdict. No Gmail access, no model, no new
verdicts. Idempotent (already-recorded sources are skipped). Prints counts
only. Run in the api pod::

    python -m backend.owner_tools.seed_campaigns
"""
from __future__ import annotations

import asyncio
from collections import Counter
from datetime import UTC, datetime


async def main() -> None:
    from sqlalchemy import select

    import backend.api.app  # noqa: F401 - import-order guard
    from backend.mail_security.acquisition import scan_ref_for
    from backend.mail_security.campaigns import (
        band_keys,
        campaign_secret_from_env,
        signature,
        text_of,
    )
    from backend.mail_security.parser import parse_mime_evidence
    from backend.mail_security.worker import ObjectStoreContentResolver
    from backend.persistence.connection import create_engine, make_session_factory
    from backend.persistence.models.mail_security import MailSecurityJobRow
    from backend.persistence.repositories.mail_security_campaign_repo import (
        MailCampaignRepository,
    )
    from backend.storage.config import create_object_store_from_env

    secret = campaign_secret_from_env()
    if secret is None:
        raise SystemExit("NERVE_MAIL_SECURITY_LEDGER_KEY is not set or invalid")
    engine = create_engine()
    try:
        sessions = make_session_factory(engine)
        repo = MailCampaignRepository(sessions)
        resolver = ObjectStoreContentResolver(object_store=create_object_store_from_env())
        async with sessions() as s:
            jobs = (
                await s.execute(
                    select(MailSecurityJobRow).where(MailSecurityJobRow.state == "DONE")
                )
            ).scalars().all()
        counts: dict[str, Counter] = {}
        now = datetime.now(UTC)
        for job in jobs:
            c = counts.setdefault(job.account_scope, Counter())
            ref = scan_ref_for(job.meta)
            content = await resolver.resolve(account_scope=job.account_scope, content_ref=ref)
            if not content:
                c["no_content"] += 1
                continue
            try:
                evidence = parse_mime_evidence(content)
            except Exception:  # noqa: BLE001 - skip unparseable, count it
                c["parse_failed"] += 1
                continue
            sig = signature(secret, job.account_scope, text_of(evidence))
            if not sig:
                c["too_short"] += 1
                continue
            result = await repo.record(
                job.account_scope,
                source_key=job.source_key,
                content_ref=(job.meta or {}).get("content_ref"),
                sig=sig,
                bands=band_keys(secret, job.account_scope, sig),
                now=now,
            )
            if result is None:
                c["skipped"] += 1
            else:
                c["recorded"] += 1
                c["joined_existing"] += 1 if result[1] else 0
        for scope, c in counts.items():
            members = await repo.live_members(scope, now)  # campaigns of 2+ only
            sizes = Counter(Counter(cid for _src, cid in members).values())
            print(
                "conn", scope[5:13], dict(c),
                "campaigns_2plus", len({cid for _s, cid in members}),
                "size_histogram", dict(sorted(sizes.items())),
            )
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
