"""Owner tool: seed personal expectedness from stored observations (NS-ADR-075).

Replays every COMPLETED semantic observation of an opted-in account in the
order its mail was RECEIVED, scoring each message's request types against the
sender's history so far (never against later mail), counting them, and
recording the scores on that observation. No model calls, no Gmail access.
``--forget`` first erases the account's counts and stored scores, so a rerun
starts clean. Prints counts only. Run in the api pod::

    python -m backend.owner_tools.seed_expectedness --forget
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter


async def main(forget: bool) -> None:
    from sqlalchemy import select, update

    import backend.api.app  # noqa: F401 - import-order guard
    from backend.mail_security.acquisition import scan_ref_for
    from backend.mail_security.expectedness import score, sender_key, unexpected_high_impact
    from backend.mail_security.observer_wiring import (
        ENV_EXPECTEDNESS_SCOPES,
        scope_listed,
        sender_domain,
    )
    from backend.mail_security.parser import parse_mime_evidence
    from backend.mail_security.thread_ledger import ledger_key_from_env
    from backend.mail_security.worker import ObjectStoreContentResolver
    from backend.persistence.connection import create_engine, make_session_factory
    from backend.persistence.models.mail_security import (
        MailSecurityJobRow,
        MailSemanticObservationRow,
    )
    from backend.persistence.models.personal import CommunicationRow
    from backend.persistence.repositories.mail_security_expectedness_repo import (
        MailExpectednessRepository,
    )
    from backend.storage.config import create_object_store_from_env

    secret = ledger_key_from_env()
    if secret is None:
        raise SystemExit("NERVE_MAIL_SECURITY_LEDGER_KEY is not set or invalid")
    engine = create_engine()
    try:
        sessions = make_session_factory(engine)
        repo = MailExpectednessRepository(sessions)
        resolver = ObjectStoreContentResolver(object_store=create_object_store_from_env())
        async with sessions() as s:
            obs = (
                (
                    await s.execute(
                        select(MailSemanticObservationRow).where(
                            MailSemanticObservationRow.status == "COMPLETED"
                        )
                    )
                )
                .scalars()
                .all()
            )
            jobs = {
                (j.account_scope, j.source_key): j
                for j in (await s.execute(select(MailSecurityJobRow))).scalars()
            }
            received = {
                r[0]: r[1]
                for r in (
                    await s.execute(
                        select(CommunicationRow.content_ref, CommunicationRow.received_at)
                    )
                ).all()
            }
        # One message counts once: keep only its newest COMPLETED revision (a
        # re-assessed message is re-observed as revision N+1).
        newest: dict[tuple[str, str], MailSemanticObservationRow] = {}
        for o in obs:
            key = (o.account_scope, o.source_key)
            if key not in newest or o.revision > newest[key].revision:
                newest[key] = o
        obs = list(newest.values())
        scopes = sorted(
            {o.account_scope for o in obs if scope_listed(o.account_scope, ENV_EXPECTEDNESS_SCOPES)}
        )
        for scope in scopes:
            n: Counter = Counter()
            if forget:
                n["forgot_counts"] = await repo.forget(scope)
            rows = []
            for o in obs:
                if o.account_scope != scope:
                    continue
                job = jobs.get((scope, o.source_key))
                ref = (job.meta or {}).get("content_ref") if job else None
                rows.append((received.get(ref), o, job))
            rows.sort(key=lambda t: (t[0] is None, t[0] or 0, t[1].created_at))
            for _when, o, job in rows:
                types = sorted(
                    {r["request_type"] for r in (o.observation or {}).get("requests") or []}
                )
                if job is None or not types:
                    n["no_requests"] += 1
                    continue
                content = await resolver.resolve(
                    account_scope=scope, content_ref=scan_ref_for(job.meta)
                )
                domain = sender_domain(parse_mime_evidence(content)) if content else None
                if not domain:
                    n["no_sender_domain"] += 1
                    continue
                key = sender_key(secret, scope, domain)
                counts = await repo.counts(scope, key)
                scores = [score(t, counts).as_codes() for t in types]
                await repo.add(scope, key, types)
                obs_doc = dict(o.observation or {})
                obs_doc["expectedness"] = scores
                obs_doc["expectedness_sender_key"] = key
                async with sessions() as s:
                    await s.execute(
                        update(MailSemanticObservationRow)
                        .where(MailSemanticObservationRow.id == o.id)
                        .values(observation=obs_doc)
                    )
                    await s.commit()
                n["scored"] += 1
                for sc in scores:
                    n[f"{sc['status']}{'_HI' if sc['high_impact'] else ''}"] += 1
                n["unexpected_high_impact_msgs"] += 1 if unexpected_high_impact(scores) else 0
            print("scope", scope[5:13], dict(n))
    finally:
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--forget", action="store_true")
    asyncio.run(main(parser.parse_args().forget))
