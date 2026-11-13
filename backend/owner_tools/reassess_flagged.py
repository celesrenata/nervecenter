"""Owner tool: re-assess flagged mail after a policy change (NS-ADR-072).

Re-queues every DONE job whose NEWEST assessment is REVIEW or RESTRICTED as
the next source revision, so the security runner writes a fresh verdict under
the current policy beside the old one (verdicts are immutable; every reader
takes the newest revision). Job meta, refs and acquisition mode are otherwise
unchanged. No provider access. Dry run by default; ``--apply`` writes::

    python -m backend.owner_tools.reassess_flagged
    python -m backend.owner_tools.reassess_flagged --apply
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter

FLAGGED = ("REVIEW", "RESTRICTED")


async def main(apply: bool) -> None:
    from sqlalchemy import select, update

    import backend.api.app  # noqa: F401 - import-order guard
    from backend.mail_security.worker import SOURCE_REVISION_META_KEY
    from backend.persistence.connection import create_engine, make_session_factory
    from backend.persistence.models.mail_security import (
        MailSecurityAssessmentRow,
        MailSecurityJobRow,
    )

    engine = create_engine()
    n: Counter = Counter()
    try:
        sessions = make_session_factory(engine)
        async with sessions() as s:
            latest: dict[tuple[str, str], MailSecurityAssessmentRow] = {}
            for a in (await s.execute(select(MailSecurityAssessmentRow))).scalars():
                key = (a.account_scope, a.source_id)
                if key not in latest or a.revision > latest[key].revision:
                    latest[key] = a
            done = select(MailSecurityJobRow).where(MailSecurityJobRow.state == "DONE")
            jobs = (await s.execute(done)).scalars().all()
        for job in jobs:
            a = latest.get((job.account_scope, job.source_key))
            if a is None or a.disposition not in FLAGGED:
                continue
            n["eligible"] += 1
            if not apply:
                continue
            meta = dict(job.meta or {})
            meta[SOURCE_REVISION_META_KEY] = int(a.revision) + 1
            async with sessions() as s:
                res = await s.execute(
                    update(MailSecurityJobRow)
                    .where(MailSecurityJobRow.id == job.id, MailSecurityJobRow.state == "DONE")
                    .values(meta=meta, state="PENDING", lease_owner=None,
                            lease_expires_at=None, last_error_code=None)
                )
                await s.commit()
            n["requeued" if res.rowcount else "raced"] += 1
        print(dict(n), "" if apply else "dry run: nothing written")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true")
    asyncio.run(main(parser.parse_args().apply))
