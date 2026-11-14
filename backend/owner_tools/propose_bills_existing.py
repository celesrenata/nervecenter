"""Owner tool: run the mail -> bill-candidate proposer over stored mail (NS-ADR-095).

Uses the live proposer (opt-in, LOW_PRIORITY only, bill-signal gate,
idempotent per email) against every DONE security job's newest assessment.
**Dry run by default**: counts per status, nothing written. ``--apply``
creates the proposals (they still need the owner's "Add bill"). Prints
counts only. Run in the api pod::

    python -m backend.owner_tools.propose_bills_existing
    python -m backend.owner_tools.propose_bills_existing --apply
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from typing import Any


class _DryRunRepo:
    """Read-through wrapper that records instead of writing."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.would_create = 0

    async def candidate_exists_for_source(self, kind: str, source_id: str) -> bool:
        return await self._inner.candidate_exists_for_source(kind, source_id)

    async def find_duplicate_candidate(self, **kw: Any) -> Any:
        return await self._inner.find_duplicate_candidate(**kw)

    async def create_candidate(self, **kw: Any) -> Any:
        self.would_create += 1

        class _Row:
            id = f"dry-run-{self.would_create}"

        return _Row()

    async def set_candidate_duplicate_of(self, *_a: Any) -> None:
        return None


async def main(apply: bool) -> None:
    from sqlalchemy import select

    import backend.api.app  # noqa: F401 - import-order guard
    from backend.bills.mail_candidates import MailBillProposer
    from backend.mail_security.acquisition import scan_ref_for
    from backend.mail_security.parser import parse_mime_evidence
    from backend.mail_security.worker import ObjectStoreContentResolver
    from backend.persistence.connection import create_engine, make_session_factory
    from backend.persistence.models.mail_security import (
        MailSecurityAssessmentRow,
        MailSecurityJobRow,
    )
    from backend.persistence.repositories.bills_repo import BillsRepository
    from backend.storage.config import create_object_store_from_env

    engine = create_engine()
    n: Counter = Counter()
    try:
        sessions = make_session_factory(engine)
        real = BillsRepository(sessions)
        repo = real if apply else _DryRunRepo(real)
        proposer = MailBillProposer(repo)
        resolver = ObjectStoreContentResolver(object_store=create_object_store_from_env())
        async with sessions() as s:
            jobs = (await s.execute(
                select(MailSecurityJobRow).where(MailSecurityJobRow.state == "DONE")
            )).scalars().all()
            latest: dict[tuple[str, str], Any] = {}
            for a in (await s.execute(select(MailSecurityAssessmentRow))).scalars():
                key = (a.account_scope, a.source_id)
                if key not in latest or a.revision > latest[key].revision:
                    latest[key] = a
        for job in jobs:
            asmt = latest.get((job.account_scope, job.source_key))
            if asmt is None:
                n["NO_ASSESSMENT"] += 1
                continue
            raw = await resolver.resolve(account_scope=job.account_scope,
                                         content_ref=scan_ref_for(job.meta))
            if not raw:
                n["NO_CONTENT"] += 1
                continue
            status = await proposer.propose(
                job.account_scope,
                content_ref=str((job.meta or {}).get("content_ref") or "") or None,
                evidence=parse_mime_evidence(raw),
                disposition=asmt.disposition,
            )
            n[status] += 1
    finally:
        await engine.dispose()
    print(dict(n), "applied" if apply else "dry run: nothing written")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true")
    asyncio.run(main(parser.parse_args().apply))
