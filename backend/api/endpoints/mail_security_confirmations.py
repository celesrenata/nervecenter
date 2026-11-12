"""NerveShield explicit confirmations API (Task 7.6, R-011/R-019).

* ``POST /api/mail-security/confirmations`` — request a confirmation for one
  action on one proposal. The proposal payload is re-validated, and the
  server re-reads the assessment *in the caller's own scope* and requires it
  to still be ``SAFE_TO_PROCESS`` with no verification advice; the client's
  copy is never trusted for that.
* ``POST /api/mail-security/confirmations/{id}/approve`` — approve. High-risk
  actions need a verified recent sign-in (``step_up_id_token`` for SSO; the
  current local token for break-glass) *and*
  ``sender_verified_out_of_band: true``. A step-up proof is single-use.
* ``POST /api/mail-security/confirmations/{id}/reject`` — coded reason only.
* ``GET  /api/mail-security/confirmations`` — the caller's confirmations.

Humans only (device credentials 403). Every body forbids extra fields, so a
message cannot smuggle an approval, a capability or free text. Nothing here
executes an action; execution stays default-deny without an exact approval.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from backend.api.auth import current_access
from backend.api.endpoints.mail_security import (
    _caller_scopes,
    _eligible_for_server_scopes,
    _repo,
    _safe_error,
)
from backend.api.step_up import verify_step_up
from backend.mail_security import confirmations as cf
from backend.mail_security.review_feedback import reviewer_ref, verification_advice
from backend.mail_security.task_proposals import TaskProposal
from backend.persistence.repositories.mail_security_confirmation_repo import (
    ConfirmationRepository,
    StepUpReused,
    to_confirmation,
)
from schemas.nc_platform import NCPrincipal

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/mail-security", tags=["mail-security"])


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RequestBody(_Body):
    proposal: dict[str, Any]
    action: str = Field(max_length=80)
    ttl_minutes: int = Field(default=10, ge=1, le=60)


class ApproveBody(_Body):
    sender_verified_out_of_band: bool = False
    step_up_id_token: str | None = Field(default=None, max_length=8192)


class RejectBody(_Body):
    reason: cf.RejectReason


def _principal(claims: dict[str, Any]) -> NCPrincipal:
    sub = str(claims.get("sub") or claims.get("sid") or "")
    body = cf._ulid_like(f"principal:{sub}")
    return NCPrincipal(
        id=f"row-{body}",
        principal_id=f"prn_{body}",
        kind="human",
        display_name="NerveCenter user",
        namespace="personal",
    )


def _confirmations(request: Request) -> ConfirmationRepository:
    registry = getattr(request.app.state, "repository_registry", None)
    factory = getattr(registry, "session_factory", None)
    if factory is None:
        raise _safe_error(404, "not_found")
    return ConfirmationRepository(factory)


def _view(row: Any) -> dict[str, Any]:
    return {
        "confirmation_id": row.confirmation_id,
        "proposal_id": row.proposal_id,
        "action": row.action,
        "risk": row.risk,
        "status": row.status,
        "expires_at": row.expires_at.isoformat() if row.expires_at else None,
        "approval_id": (row.approval or {}).get("approval_id"),
        "sender_verified": bool(row.sender_verified),
        "reject_reason": row.reject_reason,
        "audit": list(row.audit or []),
    }


def _human_or_403(claims: dict[str, Any]) -> None:
    if not _eligible_for_server_scopes(claims):
        raise _safe_error(403, "reviewer_not_eligible")


async def _load(request: Request, claims: dict[str, Any], cid: str) -> tuple[Any, Any]:
    repo = _confirmations(request)
    for scope in await _caller_scopes(request, claims):
        row = await repo.get(scope, cid)
        if row is not None:
            return repo, row
    raise _safe_error(404, "not_found")


@router.post("/confirmations")
async def request_confirmation(
    request: Request,
    response: Response,
    body: RequestBody,
    claims: Annotated[dict[str, Any], Depends(current_access)],
) -> dict[str, Any]:
    _human_or_403(claims)
    try:
        proposal = TaskProposal.model_validate(body.proposal)
    except ValidationError:
        raise _safe_error(422, "invalid_proposal") from None
    scopes = set(await _caller_scopes(request, claims))
    assessments = _repo(request)
    if proposal.account_scope not in scopes or assessments is None:
        raise _safe_error(404, "not_found")
    row = await assessments.get_assessment(proposal.account_scope, proposal.assessment_id)
    if row is None:
        raise _safe_error(404, "not_found")
    findings = await assessments.get_findings(proposal.account_scope, proposal.assessment_id)
    advice = verification_advice(
        ((f.category, f.status) for f in findings),
        ((row.assessment or {}).get("policy") or {}).get("reason_codes") or [],
    )
    if row.disposition != "SAFE_TO_PROCESS" or advice:
        raise _safe_error(409, "not_safe")
    now = datetime.now(UTC)
    try:
        c = cf.request_confirmation(
            proposal,
            body.action,
            principal=_principal(claims),
            requested_by=reviewer_ref(str(claims.get("sub") or "")),
            now=now,
            ttl=timedelta(minutes=body.ttl_minutes),
        )
    except cf.ConfirmationError as exc:
        raise _safe_error(422, exc.code.value.lower()) from None
    repo = _confirmations(request)
    await repo.create(c, proposal=proposal.payload())
    response.status_code = 201
    log.info("mail.security.confirmation.requested", risk=c.risk.value, action=c.action)
    return {"data": _view(await repo.get(c.account_scope, c.confirmation_id))}


@router.post("/confirmations/{confirmation_id}/approve")
async def approve_confirmation(
    request: Request,
    confirmation_id: str,
    body: ApproveBody,
    claims: Annotated[dict[str, Any], Depends(current_access)],
) -> dict[str, Any]:
    _human_or_403(claims)
    repo, row = await _load(request, claims, confirmation_id)
    now = datetime.now(UTC)
    ctx = getattr(request.app.state, "auth_ctx", None)
    step_up = await verify_step_up(ctx, claims, body.step_up_id_token, now=now)
    try:
        updated, approval = cf.approve(
            to_confirmation(row),
            principal=_principal(claims),
            now=now,
            step_up=step_up,
            sender_verified=body.sender_verified_out_of_band,
        )
    except cf.ConfirmationError as exc:
        status = 401 if exc.code is cf.Refusal.STEP_UP_REQUIRED else 409
        raise _safe_error(status, exc.code.value.lower()) from None
    try:
        await repo.save(updated, approval=approval.model_dump(mode="json"))
    except StepUpReused:
        raise _safe_error(409, "step_up_reused") from None
    log.info("mail.security.confirmation.approved", risk=updated.risk.value)
    return {"data": _view(await repo.get(updated.account_scope, updated.confirmation_id))}


@router.post("/confirmations/{confirmation_id}/reject")
async def reject_confirmation(
    request: Request,
    confirmation_id: str,
    body: RejectBody,
    claims: Annotated[dict[str, Any], Depends(current_access)],
) -> dict[str, Any]:
    _human_or_403(claims)
    repo, row = await _load(request, claims, confirmation_id)
    try:
        updated = cf.reject(
            to_confirmation(row),
            principal=_principal(claims),
            reason=body.reason,
            now=datetime.now(UTC),
        )
    except cf.ConfirmationError as exc:
        raise _safe_error(409, exc.code.value.lower()) from None
    await repo.save(updated)
    log.info("mail.security.confirmation.rejected", reason=body.reason.value)
    return {"data": _view(await repo.get(updated.account_scope, updated.confirmation_id))}


@router.get("/confirmations")
async def list_confirmations(
    request: Request,
    claims: Annotated[dict[str, Any], Depends(current_access)],
) -> dict[str, Any]:
    repo = _confirmations(request)
    out: list[dict[str, Any]] = []
    for scope in await _caller_scopes(request, claims):
        out.extend(_view(r) for r in await repo.list(scope))
    return {"data": out}


__all__ = ["router"]
