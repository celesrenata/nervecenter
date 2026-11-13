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


async def _bare_domain_sources(jobs: list) -> set[tuple[str, str]]:
    """NS-ADR-077: messages with a link whose visible text is a BARE domain
    that differs from its target (newly detected; may raise a verdict)."""
    from backend.mail_security.acquisition import scan_ref_for
    from backend.mail_security.inspections import _display_host, _registrable, _url_host
    from backend.mail_security.parser import parse_mime_evidence
    from backend.mail_security.worker import ObjectStoreContentResolver
    from backend.storage.config import create_object_store_from_env

    resolver = ObjectStoreContentResolver(object_store=create_object_store_from_env())
    out: set[tuple[str, str]] = set()
    for job in jobs:
        content = await resolver.resolve(
            account_scope=job.account_scope, content_ref=scan_ref_for(job.meta)
        )
        if not content:
            continue
        for link in parse_mime_evidence(content).links:
            shown, target = _display_host(link.display_text), _url_host(link.target)
            if (
                shown and target and not _url_host(link.display_text)
                and _registrable(shown) != _registrable(target)
            ):
                out.add((job.account_scope, job.source_key))
                break
    return out


async def _document_sources(jobs: list) -> set[tuple[str, str]]:
    """NS-ADR-086: messages carrying a PDF/OOXML/ZIP payload (newly inspected
    by the static deep-inspection sidecar; may raise a verdict)."""
    from backend.mail_security.acquisition import scan_ref_for
    from backend.mail_security.parser import parse_mime_evidence
    from backend.mail_security.static_inspection import wants_deep_inspection
    from backend.mail_security.worker import ObjectStoreContentResolver
    from backend.storage.config import create_object_store_from_env

    resolver = ObjectStoreContentResolver(object_store=create_object_store_from_env())
    out: set[tuple[str, str]] = set()
    for job in jobs:
        content = await resolver.resolve(
            account_scope=job.account_scope, content_ref=scan_ref_for(job.meta)
        )
        if not content:
            continue
        ev = parse_mime_evidence(content, retain_attachment_payloads=True)
        if any(wants_deep_inspection(a.payload) for a in ev.attachments):
            out.add((job.account_scope, job.source_key))
        for a in ev.attachments:
            a.payload = None
    return out


async def main(apply: bool, bare_domain_links: bool = False, with_documents: bool = False,
               ) -> None:
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
        bare = await _bare_domain_sources(jobs) if bare_domain_links else set()
        n["bare_domain_link_sources"] = len(bare)
        docs = await _document_sources(jobs) if with_documents else set()
        n["document_sources"] = len(docs)
        bare |= docs
        for job in jobs:
            a = latest.get((job.account_scope, job.source_key))
            if a is None:
                continue
            if a.disposition not in FLAGGED and (job.account_scope, job.source_key) not in bare:
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
    parser.add_argument(
        "--bare-domain-links",
        action="store_true",
        help="also re-queue messages with a newly detected bare-domain link mismatch",
    )
    parser.add_argument(
        "--with-documents",
        action="store_true",
        help="also re-queue messages with PDF/OOXML/ZIP attachments (static inspection)",
    )
    args = parser.parse_args()
    asyncio.run(main(args.apply, args.bare_domain_links, args.with_documents))
