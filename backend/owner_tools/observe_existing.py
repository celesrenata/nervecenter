"""Owner tool: run the SHADOW semantic observer over stored mail (NS-ADR-073).

For each opted-in account's DONE jobs, this resolves the scanned object, parses
it with the offline parser and runs the same route-guarded observer the live
worker uses, recording one enum-code observation per (message, revision).
Already-observed messages are skipped. ``--limit`` bounds model calls. Prints
status counts only. Run in the api pod::

    python -m backend.owner_tools.observe_existing --limit 20

Evaluation modes (NS-ADR-080), which store nothing:

* ``--eval-synthetic``: run the labeled SYNTHETIC set
  (``research/observer_eval``) and print its scores plus per-case codes
  (synthetic text only).
* ``--compare``: dry-run real stored mail and print AGGREGATE counts only
  (status, category, category by disposition, signals); nothing per message.

``--contract COMPACT|COMPACT_V2`` and ``--role Reader|Coder`` choose the
prompt contract and the NerveCenter combo role (always route-guarded:
secure, local-only).
"""
from __future__ import annotations

import argparse
import asyncio
import json
from collections import Counter
from typing import Any


def _runtime(
    engine: Any, *, contract: str, role: str, store: bool,
    extra_freemail_domains: frozenset[str] = frozenset(),
) -> Any:
    import os

    from backend.connectors.omniroute_observer import HttpObserverTransport, HttpRegistryTransport
    from backend.mail_security.observer_wiring import ObserverRuntime
    from backend.mail_security.route_guard import LocalOnlyRouteResolver, ResolverLimits
    from backend.mail_security.thread_ledger import ledger_key_from_env
    from backend.persistence.connection import make_session_factory
    from backend.persistence.repositories.mail_security_expectedness_repo import (
        MailExpectednessRepository,
    )
    from backend.persistence.repositories.mail_security_observation_repo import (
        MailObservationRepository,
    )
    from backend.services.inference_providers import InferenceComboService

    url = os.environ["NERVE_OMNIROUTE_URL"]
    key = os.environ["NERVE_OMNIROUTE_API_KEY"]
    sessions = make_session_factory(engine)
    return ObserverRuntime(
        combos=InferenceComboService(engine),
        registry=HttpRegistryTransport(url, key),
        resolver_factory=lambda reg: LocalOnlyRouteResolver(
            reg, omniroute_base_url=url, limits=ResolverLimits(timeout_seconds=15.0)
        ),
        transport=HttpObserverTransport(url, key),
        repository=MailObservationRepository(sessions) if store else None,
        expectedness_repository=MailExpectednessRepository(sessions) if store else None,
        expectedness_secret=ledger_key_from_env() if store else None,
        contract=contract,
        role=role,
        extra_freemail_domains=extra_freemail_domains,
    )


async def _latest_assessment(s: Any, job: Any) -> tuple[Any, list[str]] | None:
    from sqlalchemy import select

    from backend.persistence.models.mail_security import (
        MailSecurityAssessmentRow,
        MailSecurityFindingRow,
    )

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
        return None
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
    return asmt, codes


async def main(limit: int, *, contract: str, role: str) -> None:
    from sqlalchemy import select

    import backend.api.app  # noqa: F401 - import-order guard
    from backend.mail_security.acquisition import scan_ref_for
    from backend.mail_security.observer_wiring import observer_applies, sender_auth_of
    from backend.mail_security.parser import parse_mime_evidence
    from backend.mail_security.worker import ObjectStoreContentResolver
    from backend.persistence.connection import create_engine, make_session_factory
    from backend.persistence.models.mail_security import MailSecurityJobRow
    from backend.storage.config import create_object_store_from_env

    engine = create_engine()
    n: Counter = Counter()
    try:
        sessions = make_session_factory(engine)
        runtime = _runtime(engine, contract=contract, role=role, store=True)
        repo = runtime._repo  # noqa: SLF001 - same repository the runtime records into
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
            # The newest assessment revision for this source is the one to observe.
            async with sessions() as s:
                latest = await _latest_assessment(s, job)
            if latest is None:
                n["no_assessment"] += 1
                continue
            asmt, codes = latest
            rev = int(asmt.revision)
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
                sender_auth=sender_auth_of((asmt.assessment or {}).get("authentication") or {}),
            )
            n[status] += 1
        print(dict(n), "model_calls", calls)
    finally:
        await engine.dispose()


async def eval_synthetic(*, contract: str, role: str) -> dict[str, Any]:
    """Run the labeled synthetic set; nothing is stored."""
    import backend.api.app  # noqa: F401 - import-order guard
    from backend.mail_security.inspections import inspect_offline
    from backend.mail_security.observer_wiring import observer_scopes
    from backend.mail_security.parser import parse_mime_evidence
    from backend.mail_security.research.observer_eval import (
        CASES,
        EVAL_FREEMAIL_DOMAINS,
        score,
    )
    from backend.persistence.connection import create_engine

    scopes = observer_scopes()
    if scopes is not None and not scopes:
        raise SystemExit("observer not opted in for any account")
    # Any opted-in scope only satisfies the opt-in check; nothing is written.
    scope = "*" if scopes is None else sorted(scopes)[0]
    engine = create_engine()
    try:
        runtime = _runtime(engine, contract=contract, role=role, store=False,
                           extra_freemail_domains=EVAL_FREEMAIL_DOMAINS)
        results = []
        for case in CASES:
            evidence = parse_mime_evidence(case.eml())
            # Real offline rule codes, as live mail would carry.
            rule_codes = [str(f.explanation_code) for f in inspect_offline(evidence)]
            status, codes, latency = await runtime.dry_run(
                scope, source_key=f"eval:{case.case_id}",
                evidence=evidence, finding_codes=rule_codes,
                sender_auth=case.sender_auth,
            )
            results.append((case, status, codes, latency))
    finally:
        await engine.dispose()
    rep = score(results)
    out = {"contract": contract, "role": role, **rep.as_dict()}
    out["per_case"] = [" ".join(row) for row in rep.per_case]
    return out


async def compare(limit: int, *, contract: str, role: str) -> dict[str, Any]:
    """Dry-run real stored mail; returns aggregate counts only."""
    from sqlalchemy import select

    import backend.api.app  # noqa: F401 - import-order guard
    from backend.mail_security.acquisition import scan_ref_for
    from backend.mail_security.observer_wiring import observer_applies, sender_auth_of
    from backend.mail_security.parser import parse_mime_evidence
    from backend.mail_security.worker import ObjectStoreContentResolver
    from backend.persistence.connection import create_engine, make_session_factory
    from backend.persistence.models.mail_security import MailSecurityJobRow
    from backend.storage.config import create_object_store_from_env

    engine = create_engine()
    status_n: Counter = Counter()
    category_n: Counter = Counter()
    by_disp: Counter = Counter()
    signals_n: Counter = Counter()
    phish_on_aligned_low = 0
    aligned_low = 0
    latencies: list[int] = []
    try:
        sessions = make_session_factory(engine)
        runtime = _runtime(engine, contract=contract, role=role, store=False)
        resolver = ObjectStoreContentResolver(object_store=create_object_store_from_env())
        async with sessions() as s:
            done = select(MailSecurityJobRow).where(MailSecurityJobRow.state == "DONE")
            jobs = (await s.execute(done)).scalars().all()
        calls = 0
        for job in jobs:
            if calls >= limit:
                break
            if not observer_applies(job.account_scope):
                continue
            async with sessions() as s:
                latest = await _latest_assessment(s, job)
            if latest is None:
                continue
            asmt, codes = latest
            content = await resolver.resolve(
                account_scope=job.account_scope, content_ref=scan_ref_for(job.meta)
            )
            if not content:
                continue
            calls += 1
            auth = sender_auth_of((asmt.assessment or {}).get("authentication") or {})
            status, obs, latency = await runtime.dry_run(
                job.account_scope, source_key=job.source_key,
                evidence=parse_mime_evidence(content), finding_codes=codes, sender_auth=auth,
            )
            status_n[status] += 1
            if obs is None:
                continue
            if latency is not None:
                latencies.append(int(latency))
            cat = str(obs.get("message_category"))
            category_n[cat] += 1
            by_disp[f"{asmt.disposition}:{cat}"] += 1
            signals_n.update(set(obs.get("risk_indicators") or []))
            if asmt.disposition == "LOW_PRIORITY" and auth == "DMARC_PASS":
                aligned_low += 1
                phish_on_aligned_low += cat == "PHISHING_SUSPECTED"
    finally:
        await engine.dispose()
    completed = status_n.get("COMPLETED", 0)
    lat = sorted(latencies)
    return {
        "contract": contract,
        "role": role,
        "model_calls": calls,
        "statuses": dict(status_n),
        "format_ok_rate": round(completed / calls, 3) if calls else None,
        "categories": dict(category_n.most_common()),
        "phishing_rate": round(category_n.get("PHISHING_SUSPECTED", 0) / completed, 3)
        if completed else None,
        "phishing_on_dmarc_pass_low_priority": f"{phish_on_aligned_low}/{aligned_low}",
        "by_disposition": dict(sorted(by_disp.items())),
        "signals": dict(signals_n.most_common()),
        "latency_ms_p50": lat[len(lat) // 2] if lat else None,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--contract", choices=("COMPACT", "COMPACT_V2"), default=None)
    parser.add_argument("--role", choices=("Reader", "Coder"), default="Reader")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--eval-synthetic", action="store_true")
    mode.add_argument("--compare", action="store_true")
    args = parser.parse_args()
    if args.compare and args.role != "Reader":
        # Real mail goes only to the route whose no-memory/no-logging was
        # proven (secure/reader, NS-ADR-073). Other roles: synthetic only.
        raise SystemExit("--compare uses real mail: Reader role only")
    if args.eval_synthetic or args.compare:
        chosen = args.contract or "COMPACT_V2"
        run = (
            eval_synthetic(contract=chosen, role=args.role)
            if args.eval_synthetic
            else compare(max(1, args.limit), contract=chosen, role=args.role)
        )
        print(json.dumps(asyncio.run(run), indent=1))
    else:
        from backend.mail_security.observer_wiring import observer_contract_from_env

        if args.role != "Reader":
            raise SystemExit("stored observations use the Reader role only")
        asyncio.run(
            main(max(1, args.limit), contract=args.contract or observer_contract_from_env(),
                 role="Reader")
        )
