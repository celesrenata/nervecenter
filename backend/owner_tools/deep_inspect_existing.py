"""Owner tool: SHADOW dry run of static deep inspection over stored mail (NS-ADR-086).

For every DONE job, parses the stored message, sends each PDF/OOXML/ZIP
attachment to the inspector sidecar and prints AGGREGATE counts only: kinds,
codes, incomplete reasons, sandbox flags, document-link deception hits and
how many messages would gain a MEDIUM+ deep finding (a REVIEW reason).
Nothing is stored and no assessment changes. Run in the api pod::

    python -m backend.owner_tools.deep_inspect_existing
"""
from __future__ import annotations

import asyncio
import json
from collections import Counter


async def main() -> None:
    from sqlalchemy import select

    import backend.api.app  # noqa: F401 - import-order guard
    from backend.mail_security.acquisition import scan_ref_for
    from backend.mail_security.models import FindingSeverity
    from backend.mail_security.parser import parse_mime_evidence
    from backend.mail_security.static_inspection import (
        StaticInspectorUnavailable,
        findings_from_report,
        inspector_from_env,
        wants_deep_inspection,
    )
    from backend.mail_security.worker import ObjectStoreContentResolver
    from backend.persistence.connection import create_engine, make_session_factory
    from backend.persistence.models.mail_security import MailSecurityJobRow
    from backend.storage.config import create_object_store_from_env

    inspector = inspector_from_env()
    if inspector is None:
        raise SystemExit("static inspection is not configured")
    engine = create_engine()
    n: Counter = Counter()
    kinds: Counter = Counter()
    codes: Counter = Counter()
    reasons: Counter = Counter()
    sandbox: Counter = Counter()
    deception: Counter = Counter()
    try:
        sessions = make_session_factory(engine)
        resolver = ObjectStoreContentResolver(object_store=create_object_store_from_env())
        async with sessions() as s:
            jobs = (await s.execute(
                select(MailSecurityJobRow).where(MailSecurityJobRow.state == "DONE")
            )).scalars().all()
        for job in jobs:
            raw = await resolver.resolve(account_scope=job.account_scope,
                                         content_ref=scan_ref_for(job.meta))
            if not raw:
                n["no_content"] += 1
                continue
            ev = parse_mime_evidence(raw, retain_attachment_payloads=True)
            sent = review = False
            for att in ev.attachments:
                payload, att.payload = att.payload, None
                if not wants_deep_inspection(payload):
                    continue
                sent = True
                n["attachments_inspected"] += 1
                try:
                    report = await inspector.inspect(payload)
                except StaticInspectorUnavailable as exc:
                    reasons[f"UNAVAILABLE:{exc}"] += 1
                    continue
                kinds[report.kind] += 1
                codes.update(report.codes)
                reasons.update(report.incomplete_reasons)
                if report.sandbox:
                    sandbox.update(f"{k}={v}" for k, v in report.sandbox.items())
                result = findings_from_report(att.evidence_ref, report)
                for f in result.findings:
                    if ":doclink:" in f.finding_key:
                        deception[f.explanation_code] += 1
                    if f.severity in (FindingSeverity.MEDIUM, FindingSeverity.HIGH):
                        review = True
                n["link_hosts"] += len(report.link_hosts)
                n["incomplete"] += not report.complete
            n["messages_with_documents"] += sent
            n["messages_gaining_review_reason"] += review
    finally:
        await engine.dispose()
    print(json.dumps({"totals": dict(n), "kinds": dict(kinds), "codes": dict(codes),
                      "incomplete_reasons": dict(reasons), "sandbox": dict(sandbox),
                      "doc_link_deception": dict(deception)}, indent=1))


if __name__ == "__main__":
    asyncio.run(main())
