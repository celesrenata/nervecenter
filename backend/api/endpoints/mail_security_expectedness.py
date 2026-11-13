"""NerveShield expectedness feedback API (NS-ADR-078, 8.3 follow-on).

* ``POST /api/mail-security/assessments/{assessment_id}/expected`` — the owner
  says "this was expected" for a message queued as an UNEXPECTED high-impact
  ask. One feedback per such ask, against that message's keyed sender.
* ``DELETE /api/mail-security/expectedness-feedback/{feedback_id}`` — undo.
* ``GET /api/mail-security/expectedness-feedback`` — recent feedback (codes).

Contract (mirrors Task 7.2):

* **Scope.** The assessment and the feedback are resolved under the caller's
  own scopes only; anything else is the same safe ``404``. The body cannot
  name an account (``extra="forbid"``).
* **Human reviewers only** (device / non-access tokens: ``403``). The reviewer
  is stored as an opaque hash.
* **Bounded effect.** Each feedback adds a few pseudo-counts for that sender
  and request type, expires after 180 days, never counts as history, and is
  exactly undone by revoking it (the row is kept for audit). It never makes a
  message or sender "safe", and never changes a disposition.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select

from backend.api.auth import current_access
from backend.api.endpoints.mail_security import (
    _caller_scopes,
    _eligible_for_server_scopes,
    _repo,
    _safe_error,
)
from backend.mail_security.expectedness import (
    FEEDBACK_TTL_DAYS,
    FEEDBACK_WEIGHT,
    HIGH_IMPACT,
    Status,
)
from backend.mail_security.review_feedback import reviewer_ref, valid_idempotency_key

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/mail-security", tags=["mail-security"])


class ExpectedBody(BaseModel):
    """Unknown fields are rejected; there is nothing to choose but idempotency."""

    model_config = ConfigDict(extra="forbid")

    idempotency_key: str | None = None


def _expect_repo(request: Request) -> Any:
    from backend.persistence.repositories.mail_security_expectedness_repo import (
        MailExpectednessRepository,
    )

    registry = getattr(request.app.state, "repository_registry", None)
    factory = getattr(registry, "session_factory", None)
    return None if factory is None else MailExpectednessRepository(factory)


def _view(row: Any, me: str, now: datetime) -> dict[str, Any]:
    expires = row.expires_at if row.expires_at.tzinfo else row.expires_at.replace(tzinfo=UTC)
    return {
        "feedback_id": row.feedback_id,
        "assessment_id": row.assessment_id,
        "request_type": row.request_type,
        "weight": row.weight,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "expires_at": expires.isoformat(),
        "revoked": row.revoked_at is not None,
        "live": row.revoked_at is None and expires > now,
        "mine": row.reviewer_ref == me,
    }


async def _latest_expectedness(request: Request, scope: str, source_key: str) -> dict | None:
    from backend.persistence.models.mail_security import MailSemanticObservationRow

    factory = request.app.state.repository_registry.session_factory
    async with factory() as s:
        rows = (
            await s.execute(
                select(MailSemanticObservationRow.observation)
                .where(
                    MailSemanticObservationRow.account_scope == scope,
                    MailSemanticObservationRow.source_key == source_key,
                    MailSemanticObservationRow.status == "COMPLETED",
                )
                .order_by(
                    MailSemanticObservationRow.revision.desc(),
                    MailSemanticObservationRow.created_at.desc(),
                )
            )
        ).scalars().all()
    for obs in rows:
        if isinstance(obs, dict) and obs.get("expectedness") and obs.get("expectedness_sender_key"):
            return obs
    return None


@router.post("/assessments/{assessment_id}/expected")
async def post_expected(
    request: Request,
    response: Response,
    assessment_id: str,
    body: ExpectedBody,
    claims: Annotated[dict[str, Any], Depends(current_access)],
) -> dict[str, Any]:
    if not _eligible_for_server_scopes(claims):
        raise _safe_error(403, "reviewer_not_eligible")
    if not valid_idempotency_key(body.idempotency_key):
        raise _safe_error(422, "invalid_idempotency_key")
    repo, expect = _repo(request), _expect_repo(request)
    scopes = await _caller_scopes(request, claims)
    if repo is None or expect is None or not scopes:
        raise _safe_error(404, "not_found")
    row = scope = None
    for candidate in scopes:
        row = await repo.get_assessment(candidate, assessment_id)
        if row is not None:
            scope = candidate
            break
    if row is None or scope is None:
        raise _safe_error(404, "not_found")
    obs = await _latest_expectedness(request, scope, row.source_id)
    asks = sorted(
        {
            str(s.get("request_type"))
            for s in (obs or {}).get("expectedness") or []
            if s.get("status") == Status.UNEXPECTED.value
            and str(s.get("request_type")) in HIGH_IMPACT
        }
    )
    if obs is None or not asks:
        raise _safe_error(409, "nothing_unexpected")

    me = reviewer_ref(str(claims["sub"]))
    now = datetime.now(UTC)
    idem = body.idempotency_key or now.isoformat()
    created_any = False
    views = []
    for ask in asks:
        material = f"{scope}\x1f{row.assessment_id}\x1f{ask}\x1f{me}\x1f{idem}"
        fid = "efb_" + hashlib.sha256(material.encode()).hexdigest()[:32]
        fb, created = await expect.add_feedback(
            feedback_id=fid,
            account_scope=scope,
            assessment_id=row.assessment_id,
            source_key=row.source_id,
            sender_key=str(obs["expectedness_sender_key"]),
            request_type=ask,
            weight=FEEDBACK_WEIGHT,
            expires_at=now + timedelta(days=FEEDBACK_TTL_DAYS),
            reviewer_ref=me,
        )
        created_any = created_any or created
        views.append(_view(fb, me, now))
    response.status_code = 201 if created_any else 200
    log.info("mail.security.expected.recorded", count=len(views), created=created_any)
    return {"data": views}


@router.delete("/expectedness-feedback/{feedback_id}")
async def delete_expected(
    request: Request,
    feedback_id: str,
    claims: Annotated[dict[str, Any], Depends(current_access)],
) -> dict[str, Any]:
    if not _eligible_for_server_scopes(claims):
        raise _safe_error(403, "reviewer_not_eligible")
    expect = _expect_repo(request)
    scopes = await _caller_scopes(request, claims)
    if expect is None or not scopes:
        raise _safe_error(404, "not_found")
    now = datetime.now(UTC)
    row = await expect.revoke_feedback(scopes, feedback_id, now=now)
    if row is None:
        raise _safe_error(404, "not_found")
    log.info("mail.security.expected.revoked")
    return {"data": _view(row, reviewer_ref(str(claims["sub"])), now)}


@router.get("/expectedness-feedback")
async def list_expected(
    request: Request,
    claims: Annotated[dict[str, Any], Depends(current_access)],
) -> dict[str, Any]:
    expect = _expect_repo(request)
    scopes = await _caller_scopes(request, claims)
    if expect is None or not scopes:
        raise _safe_error(404, "not_found")
    now = datetime.now(UTC)
    me = reviewer_ref(str(claims.get("sub") or ""))
    return {"data": [_view(r, me, now) for r in await expect.list_feedback(scopes)]}


__all__ = ["ExpectedBody", "router"]
