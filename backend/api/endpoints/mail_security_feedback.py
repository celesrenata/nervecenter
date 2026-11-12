"""NerveShield reviewer feedback API (Task 7.2, R-013/R-016/R-020).

``POST /api/mail-security/assessments/{assessment_id}/feedback`` records one
typed reviewer verdict; ``GET`` on the same path lists the feedback for that
assessment. Kept out of the read-only router so that module stays GET-only.

Contract:

- **Scope (R-020.2, R-023).** The assessment is resolved under the caller's
  own scopes only; another account's id is the same safe ``404`` as an absent
  one. The stored ``account_scope`` is copied from the assessment row; the
  request body cannot name an account (``extra="forbid"``).
- **Human reviewers only.** Device credentials and non-access tokens are
  refused (``403 reviewer_not_eligible``). The reviewer is stored as an
  opaque hash, never the subject or an address.
- **Codes only.** The body carries a verdict enum and an optional idempotency
  key. No free text, no consent reference, no indicator values.
- **No implicit trust, no training (R-016.1).** Feedback is routed through the
  5.5 ledger rules and the resulting proposals are *recorded*, never applied:
  no intel override, trust-index entry, policy change or model update happens
  here. Training proposals stay ``BLOCKED_NO_CONSENT`` because this API never
  accepts a consent reference.
- **Audit.** Every record is append-only with time, verdict, mapped kind,
  source, reviewer ref and the disposition the reviewer saw. A replay with the
  same idempotency key returns the existing record (``200``) instead of a
  duplicate (``201``).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import uuid4

import structlog
from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict

from backend.api.auth import current_access
from backend.api.endpoints.mail_security import (
    _caller_scopes,
    _eligible_for_server_scopes,
    _repo,
    _safe_error,
)
from backend.mail_security.feedback import (
    FeedbackError,
    FeedbackLedger,
    FeedbackRecord,
    FeedbackSource,
)
from backend.mail_security.review_feedback import (
    ReviewerVerdict,
    feedback_id,
    kind_for_verdict,
    reviewer_ref,
    valid_idempotency_key,
)

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/mail-security", tags=["mail-security"])


class FeedbackBody(BaseModel):
    """Reviewer feedback request. Unknown fields are rejected."""

    model_config = ConfigDict(extra="forbid")

    verdict: ReviewerVerdict
    idempotency_key: str | None = None


def _view(row: Any, me: str) -> dict[str, Any]:
    return {
        "feedback_id": row.feedback_id,
        "assessment_id": row.assessment_id,
        "verdict": row.verdict,
        "kind": row.kind,
        "source": row.source,
        "disposition_seen": row.disposition_seen,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "mine": row.reviewer_ref == me,
        "proposals": [
            {
                "destination": p.get("destination"),
                "scope": p.get("scope"),
                "status": p.get("status"),
            }
            for p in (row.proposals or [])
        ],
        # Recorded, never applied: nothing here changes trust, intel or models.
        "applied": False,
    }


async def _resolve(
    request: Request, claims: dict[str, Any], assessment_id: str
) -> tuple[Any, Any, str]:
    repo = _repo(request)
    scopes = await _caller_scopes(request, claims)
    if repo is None or not scopes:
        raise _safe_error(404, "not_found")
    for scope in scopes:
        row = await repo.get_assessment(scope, assessment_id)
        if row is not None:
            return repo, row, scope
    raise _safe_error(404, "not_found")


@router.post("/assessments/{assessment_id}/feedback")
async def post_feedback(
    request: Request,
    response: Response,
    assessment_id: str,
    body: FeedbackBody,
    claims: Annotated[dict[str, Any], Depends(current_access)],
) -> dict[str, Any]:
    if not _eligible_for_server_scopes(claims):
        raise _safe_error(403, "reviewer_not_eligible")
    if not valid_idempotency_key(body.idempotency_key):
        raise _safe_error(422, "invalid_idempotency_key")
    repo, row, scope = await _resolve(request, claims, assessment_id)

    me = reviewer_ref(str(claims["sub"]))
    verdict = body.verdict
    kind = kind_for_verdict(verdict, row.disposition)
    fid = feedback_id(
        scope, row.assessment_id, me, verdict, body.idempotency_key or uuid4().hex
    )
    record = FeedbackRecord(
        feedback_id=fid,
        account_scope=scope,
        assessment_ref=row.assessment_id,
        kind=kind,
        source=FeedbackSource.ACCOUNT_OWNER,
        reviewer_ref=me,
        created_at=datetime.now(UTC),
    )
    try:
        proposals = FeedbackLedger().record(record)
    except FeedbackError as exc:
        raise _safe_error(422, exc.code.value.lower()) from None

    written = await repo.record_feedback(
        feedback_id=fid,
        account_scope=scope,
        assessment_id=row.assessment_id,
        verdict=verdict.value,
        kind=kind.value,
        source=FeedbackSource.ACCOUNT_OWNER.value,
        reviewer_ref=me,
        disposition_seen=row.disposition,
        proposals=[
            {
                "destination": p.destination.value,
                "scope": p.scope.value,
                "status": p.status.value,
            }
            for p in proposals
        ],
    )
    response.status_code = 201 if written.created else 200
    log.info(
        "mail.security.feedback.recorded",
        feedback_id=fid,
        verdict=verdict.value,
        kind=kind.value,
        created=written.created,
    )
    return {"data": _view(written.row, me)}


@router.get("/assessments/{assessment_id}/feedback")
async def list_feedback(
    request: Request,
    assessment_id: str,
    claims: Annotated[dict[str, Any], Depends(current_access)],
) -> dict[str, Any]:
    repo, row, scope = await _resolve(request, claims, assessment_id)
    me = reviewer_ref(str(claims.get("sub") or ""))
    rows = await repo.list_feedback(scope, row.assessment_id)
    return {"data": [_view(r, me) for r in rows]}


__all__ = ["FeedbackBody", "router"]
