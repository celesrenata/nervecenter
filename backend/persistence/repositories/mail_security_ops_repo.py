"""Read-only aggregate counts for the NerveShield operations dashboard (Task 7.7).

Every value is a count, a version label or a timing. Labels (versions,
model routes/ids, runtimes) are configuration identifiers and pass a strict
safe-label check; anything else is bucketed as ``OTHER``. No assessment,
message, communication, person or account identifier is returned.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable
from typing import Any

from sqlalchemy import func, select

from backend.persistence.models.mail_security import (
    MailSecurityAssessmentRow,
    MailSecurityConfirmationRow,
    MailSecurityFeedbackRow,
    MailSecurityHeldPromotionRow,
)

MAX_ASSESSMENTS_SCANNED = 5000
_SAFE = re.compile(r"^[A-Za-z0-9_.:/@+-]{1,100}$")


def safe_label(value: object) -> str:
    return value if isinstance(value, str) and _SAFE.fullmatch(value) else "OTHER"


def _counts(rows: Iterable[tuple[Any, ...]]) -> dict[str, int]:
    out: Counter[str] = Counter()
    for *labels, n in rows:
        out["|".join(safe_label(x) if x is not None else "NONE" for x in labels)] += int(n)
    return dict(sorted(out.items()))


async def load_ops_counts(session_factory: Any, scopes: list[str]) -> dict[str, Any]:
    if not scopes:
        return {}
    A, F = MailSecurityAssessmentRow, MailSecurityFeedbackRow
    H, C = MailSecurityHeldPromotionRow, MailSecurityConfirmationRow
    async with session_factory() as s:
        versions = (
            await s.execute(
                select(A.analysis_profile_version, A.policy_version, A.model_version, func.count())
                .where(A.account_scope.in_(scopes))
                .group_by(A.analysis_profile_version, A.policy_version, A.model_version)
            )
        ).all()
        dispositions = (
            await s.execute(
                select(A.disposition, func.count())
                .where(A.account_scope.in_(scopes))
                .group_by(A.disposition)
            )
        ).all()
        payloads = (
            (
                await s.execute(
                    select(A.assessment)
                    .where(A.account_scope.in_(scopes))
                    .order_by(A.created_at.desc())
                    .limit(MAX_ASSESSMENTS_SCANNED)
                )
            )
            .scalars()
            .all()
        )
        feedback = (
            await s.execute(
                select(F.kind, func.count()).where(F.account_scope.in_(scopes)).group_by(F.kind)
            )
        ).all()
        holds = (
            await s.execute(
                select(H.status, func.count()).where(H.account_scope.in_(scopes)).group_by(H.status)
            )
        ).all()
        confirmations = (
            await s.execute(
                select(C.status, C.risk, func.count())
                .where(C.account_scope.in_(scopes))
                .group_by(C.status, C.risk)
            )
        ).all()

    runs: Counter[str] = Counter()
    latency: dict[str, list[float]] = {}
    for payload in payloads:
        for run in (payload or {}).get("model_runs") or []:
            if not isinstance(run, dict):
                continue
            key = "|".join(
                safe_label(run.get(k)) for k in ("route", "model_id", "runtime", "status")
            )
            runs[key] += 1
            ms = run.get("latency_ms")
            if isinstance(ms, (int, float)) and not isinstance(ms, bool) and ms >= 0:
                latency.setdefault(key, []).append(float(ms))

    fb = _counts(feedback)
    fp, ct = fb.get("FALSE_POSITIVE", 0), fb.get("CONFIRMED_THREAT", 0)
    fn = fb.get("FALSE_NEGATIVE", 0)
    return {
        "versions": _counts(versions),
        "dispositions": _counts(dispositions),
        "assessments_scanned": len(payloads),
        "model_runs": {
            key: {
                "count": n,
                "latency_ms_mean": (sum(latency[key]) / len(latency[key]))
                if latency.get(key)
                else None,
                "latency_ms_max": max(latency[key]) if latency.get(key) else None,
            }
            for key, n in sorted(runs.items())
        },
        "feedback": {
            "by_kind": fb,
            # Share of reviewed *flagged* verdicts the reviewer called safe.
            "flagged_false_positive_rate": (fp / (fp + ct)) if (fp + ct) else None,
            "missed_threat_reports": fn,
        },
        "gate_holds": _counts(holds),
        "confirmations": _counts(confirmations),
    }


__all__ = ["MAX_ASSESSMENTS_SCANNED", "load_ops_counts", "safe_label"]
