"""Owner tool: run the SHADOW semantic observer over stored mail (NS-ADR-073).

For each opted-in account's DONE jobs, this resolves the scanned object, parses
it with the offline parser and runs the same route-guarded observer the live
worker uses, recording one enum-code observation per (message, revision).
Already-observed messages are skipped. ``--limit`` bounds model calls. Prints
status counts only. Run in the api pod::

    python -m backend.owner_tools.observe_existing --limit 20
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter


async def main(limit: int) -> None:
    import os

    from sqlalchemy import select

    import backend.api.app  # noqa: F401 - import-order guard
    from backend.connectors.omniroute_observer import HttpObserverTransport, HttpRegistryTransport
    from backend.mail_security.acquisition import scan_ref_for
    from backend.mail_security.observer_wiring import ObserverRuntime, observer_applies
    from backend.mail_security.parser import parse_mime_evidence
    from backend.mail_security.route_guard import LocalOnlyRouteResolver, ResolverLimits
    from backend.mail_security.worker import ObjectStoreContentResolver, source_revision_of
    from backend.persistence.connection import create_engine, make_session_factory
    from backend.persistence.models.mail_security import (
        MailSecurityAssessmentRow,
        MailSecurityFindingRow,
        MailSecurityJobRow,
    )
    from backend.persistence.repositories.mail_security_observation_repo import (
        MailObservationRepository,
    )
    from backend.services.inference_providers import InferenceComboService
    from backend.storage.config import create_object_store_from_env

    url = os.environ["NERVE_OMNIROUTE_URL"]
    key = os.environ["NERVE_OMNIROUTE_API_KEY"]
    engine = create_engine()
    n: Counter = Counter()
    try:
        sessions = make_session_factory(engine)
        repo = MailObservationRepository(sessions)
        runtime = ObserverRuntime(
            combos=InferenceComboService(engine),
            registry=HttpRegistryTransport(url, key),
            resolver_factory=lambda reg: LocalOnlyRouteResolver(
                reg, omniroute_base_url=url, limits=ResolverLimits(timeout_seconds=15.0)
            ),
            transport=HttpObserverTransport(url, key),
            repository=repo,
        )
        resolver = ObjectStoreContentResolver(object_store=create_object_store_from_env())
        async with sessions() as s:
            done = select(MailSecurityJobRow).where(MailSecurityJobRow.state == "DONE")
            jobs = (await s.execute(done)).scalars().all()
        calls = 0
        for job in jobs:
            if calls >= limit:
                break
            if not observer_applies(job.account_scope):
                n["not_opted_in"] += 1
                continue
            rev = source_revision_of(job.meta)
            # The newest assessment revision for this source is the one to observe.
            async with sessions() as s:
                asmt = (
                    await s.execute(
                        select(MailSecurityAssessmentRow)
                        .where(
                            MailSecurityAssessmentRow.account_scope == job.account_scope,
                            MailSecurityAssessmentRow.source_id == job.source_key,
                        )
                        .order_by(MailSecurityAssessmentRow.revision.desc())
                        .limit(1)
                    )
                ).scalar_one_or_none()
                if asmt is None:
                    n["no_assessment"] += 1
                    continue
                rev = int(asmt.revision)
                codes = [
                    r[0]
                    for r in (
                        await s.execute(
                            select(MailSecurityFindingRow.explanation_code).where(
                                MailSecurityFindingRow.assessment_id == asmt.assessment_id
                            )
                        )
                    ).all()
                ]
            if await repo.exists(job.account_scope, job.source_key, rev):
                n["already"] += 1
                continue
            content = await resolver.resolve(
                account_scope=job.account_scope, content_ref=scan_ref_for(job.meta)
            )
            if not content:
                n["no_content"] += 1
                continue
            calls += 1
            status = await runtime.observe(
                job.account_scope, source_key=job.source_key, revision=rev,
                evidence=parse_mime_evidence(content), finding_codes=codes,
            )
            n[status] += 1
        print(dict(n), "model_calls", calls)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--limit", type=int, default=20)
    asyncio.run(main(max(1, parser.parse_args().limit)))
