"""Explicit confirmation workflow for email-origin proposals (Task 7.6, R-011/R-019).

A proposal (7.5) grants nothing. Acting on it needs an explicit, action-
specific confirmation by an authenticated human, decided by the existing PDP
through an :class:`~schemas.nc_platform.NCApproval` bound to the exact
execution payload, target URI and action.

Rules (``mail-confirmation@1``):

* **Closed actions per kind.** Each proposal kind allows one low-risk
  internal write and, at most, a few named high-risk actions:

  - calendar: ``resource.write:calendar.events`` (low);
    ``resource.disclose:calendar.invites`` (high, EXTERNAL_DISCLOSURE)
  - bill: ``resource.write:bills.reminders`` (low);
    ``resource.pay:bills.payments`` (high, PAYMENT)
  - relationship: ``resource.write:relationships.followups`` (low);
    ``resource.send:messages.outbound`` (high, OUTBOUND_MESSAGE)

* **Every action needs a confirmation** (low risk included); nothing in a
  message can create one. Confirmations expire (default 10 min, max 60).
* **High risk needs more** (R-019.3): a recent independent sign-in
  (step-up, ``auth_time`` within :data:`STEP_UP_MAX_AGE_SECONDS`) *and* the
  approver's attestation that the sender was verified through a channel
  they already trust. A step-up proof is single-use.
* **Humans only.** Machine principals cannot request or approve.
* **Approval is exact and single-use.** The issued approval binds the
  execution payload hash (proposal payload + action), the proposal's
  resource URI and ``{"action": <action>}``; it is consumed by the first
  authorized execution and cannot authorize a different action or payload.
* **Audit.** Every transition appends a codes-only entry. A rejection keeps
  a safe record and never feeds training (R-019.5).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from backend.mail_security.task_proposals import ProposalKind, TaskProposal
from backend.policy.pdp import Decision, PolicyDecisionPoint
from schemas.base import content_hash
from schemas.nc_platform import NCApproval, NCPrincipal

CONFIRMATION_SCHEMA_VERSION = "mail-confirmation@1"
DEFAULT_TTL = timedelta(minutes=10)
MAX_TTL = timedelta(minutes=60)
STEP_UP_MAX_AGE_SECONDS = 600
#: Tolerated clock skew between the issuer and this API (matches the verifier).
STEP_UP_CLOCK_SKEW = timedelta(seconds=60)
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


class RiskClass(StrEnum):
    LOW = "LOW"
    PAYMENT = "PAYMENT"
    OUTBOUND_MESSAGE = "OUTBOUND_MESSAGE"
    EXTERNAL_DISCLOSURE = "EXTERNAL_DISCLOSURE"


ACTIONS: dict[ProposalKind, dict[str, RiskClass]] = {
    ProposalKind.CALENDAR_EVENT: {
        "resource.write:calendar.events": RiskClass.LOW,
        "resource.disclose:calendar.invites": RiskClass.EXTERNAL_DISCLOSURE,
    },
    ProposalKind.BILL_REMINDER: {
        "resource.write:bills.reminders": RiskClass.LOW,
        "resource.pay:bills.payments": RiskClass.PAYMENT,
    },
    ProposalKind.RELATIONSHIP_FOLLOWUP: {
        "resource.write:relationships.followups": RiskClass.LOW,
        "resource.send:messages.outbound": RiskClass.OUTBOUND_MESSAGE,
    },
}


class Status(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    CONSUMED = "CONSUMED"


class Refusal(StrEnum):
    ACTION_NOT_ALLOWED = "ACTION_NOT_ALLOWED"
    NOT_HUMAN = "NOT_HUMAN"
    NOT_PENDING = "NOT_PENDING"
    EXPIRED = "EXPIRED"
    STEP_UP_REQUIRED = "STEP_UP_REQUIRED"
    SENDER_VERIFICATION_REQUIRED = "SENDER_VERIFICATION_REQUIRED"
    INVALID_TTL = "INVALID_TTL"
    NOT_APPROVED = "NOT_APPROVED"
    PROPOSAL_MISMATCH = "PROPOSAL_MISMATCH"


class ConfirmationError(ValueError):
    def __init__(self, code: Refusal) -> None:
        super().__init__(code.value)
        self.code = code


class RejectReason(StrEnum):
    NOT_WANTED = "NOT_WANTED"
    LOOKS_SUSPICIOUS = "LOOKS_SUSPICIOUS"
    WRONG_DETAILS = "WRONG_DETAILS"


@dataclass(frozen=True)
class StepUp:
    """Verified independent sign-in (produced by the API step-up verifier)."""

    auth_time: datetime
    proof_id: str


@dataclass(frozen=True)
class Confirmation:
    confirmation_id: str
    account_scope: str
    proposal_id: str
    proposal_hash: str
    execution_hash: str
    action: str
    risk: RiskClass
    target: str
    requested_by: str
    created_at: datetime
    expires_at: datetime
    status: Status = Status.PENDING
    approval_id: str | None = None
    step_up_proof_id: str | None = None
    sender_verified: bool = False
    reject_reason: str | None = None
    audit: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    schema_version: str = CONFIRMATION_SCHEMA_VERSION


def execution_payload(proposal: TaskProposal, action: str) -> dict[str, Any]:
    """The immutable payload an approval binds to: proposal + action."""
    return {"proposal": proposal.payload(), "action": action}


def risk_for(proposal: TaskProposal, action: str) -> RiskClass:
    allowed = ACTIONS[proposal.kind]
    if action not in allowed:
        raise ConfirmationError(Refusal.ACTION_NOT_ALLOWED)
    return allowed[action]


def _ulid_like(seed: str) -> str:
    digest = hashlib.sha256(seed.encode()).digest()
    return "".join(_CROCKFORD[b % 32] for b in digest[:26])


def _human(principal: NCPrincipal) -> None:
    if not isinstance(principal, NCPrincipal) or principal.kind != "human":
        raise ConfirmationError(Refusal.NOT_HUMAN)


def _entry(event: str, at: datetime, **codes: Any) -> dict[str, Any]:
    return {"event": event, "at": at.isoformat(), **codes}


def request_confirmation(
    proposal: TaskProposal,
    action: str,
    *,
    principal: NCPrincipal,
    requested_by: str,
    now: datetime,
    ttl: timedelta = DEFAULT_TTL,
) -> Confirmation:
    _human(principal)
    if now.tzinfo is None:
        raise ConfirmationError(Refusal.INVALID_TTL)
    if not timedelta(minutes=1) <= ttl <= MAX_TTL:
        raise ConfirmationError(Refusal.INVALID_TTL)
    risk = risk_for(proposal, action)
    execution_hash = content_hash(execution_payload(proposal, action))
    cid = "cf-" + hashlib.sha256(f"{execution_hash}:{now.isoformat()}".encode()).hexdigest()[:24]
    return Confirmation(
        confirmation_id=cid,
        account_scope=proposal.account_scope,
        proposal_id=proposal.proposal_id,
        proposal_hash=content_hash(proposal.payload()),
        execution_hash=execution_hash,
        action=action,
        risk=risk,
        target=proposal.resource_uri,
        requested_by=requested_by,
        created_at=now,
        expires_at=now + ttl,
        audit=(_entry("REQUESTED", now, action=action, risk=risk.value),),
    )


def _live(c: Confirmation, now: datetime) -> None:
    if c.status is not Status.PENDING:
        raise ConfirmationError(Refusal.NOT_PENDING)
    if now >= c.expires_at:
        raise ConfirmationError(Refusal.EXPIRED)


def approve(
    c: Confirmation,
    *,
    principal: NCPrincipal,
    now: datetime,
    step_up: StepUp | None = None,
    sender_verified: bool = False,
) -> tuple[Confirmation, NCApproval]:
    """Approve one pending confirmation; returns the exact PDP approval."""
    _human(principal)
    _live(c, now)
    if c.risk is not RiskClass.LOW:
        fresh = step_up is not None and -STEP_UP_CLOCK_SKEW <= now - step_up.auth_time <= timedelta(
            seconds=STEP_UP_MAX_AGE_SECONDS
        )
        if not fresh:
            raise ConfirmationError(Refusal.STEP_UP_REQUIRED)
        if sender_verified is not True:
            raise ConfirmationError(Refusal.SENDER_VERIFICATION_REQUIRED)
    approval_id = "app_" + _ulid_like(f"{c.confirmation_id}:{now.isoformat()}")
    approval = NCApproval(
        id=f"row-{approval_id}",
        approval_id=approval_id,
        execution_id=c.confirmation_id,
        proposed_action={"action": c.action},
        target=c.target,
        payload_hash=c.execution_hash,
        expires_at=c.expires_at,
        state="APPROVED",
    )
    updated = replace(
        c,
        status=Status.APPROVED,
        approval_id=approval_id,
        step_up_proof_id=step_up.proof_id if step_up and c.risk is not RiskClass.LOW else None,
        sender_verified=bool(sender_verified),
        audit=(
            *c.audit,
            _entry(
                "APPROVED",
                now,
                risk=c.risk.value,
                step_up=step_up is not None and c.risk is not RiskClass.LOW,
                sender_verified=bool(sender_verified),
            ),
        ),
    )
    return updated, approval


def reject(
    c: Confirmation, *, principal: NCPrincipal, reason: RejectReason, now: datetime
) -> Confirmation:
    _human(principal)
    if c.status is not Status.PENDING:
        raise ConfirmationError(Refusal.NOT_PENDING)
    return replace(
        c,
        status=Status.REJECTED,
        reject_reason=RejectReason(reason).value,
        audit=(*c.audit, _entry("REJECTED", now, reason=RejectReason(reason).value)),
    )


def expire(c: Confirmation, *, now: datetime) -> Confirmation:
    if c.status in (Status.PENDING, Status.APPROVED) and now >= c.expires_at:
        return replace(c, status=Status.EXPIRED, audit=(*c.audit, _entry("EXPIRED", now)))
    return c


@dataclass(frozen=True)
class ExecutionDecision:
    allowed: bool
    reason_code: str
    pdp: Decision | None = None


def authorize_execution(
    pdp: PolicyDecisionPoint,
    principal: NCPrincipal,
    proposal: TaskProposal,
    action: str,
    *,
    confirmation: Confirmation | None,
    approval: NCApproval | None,
    now: datetime,
) -> ExecutionDecision:
    """May ``principal`` execute ``action`` on ``proposal`` right now?

    Without an APPROVED, unexpired confirmation for this exact proposal and
    action, the answer is no; with one, the existing PDP decides with the
    exact-payload approval (execution actions are default-deny, so only the
    verified binding can allow).
    """
    if not isinstance(principal, NCPrincipal) or principal.kind != "human":
        return ExecutionDecision(False, Refusal.NOT_HUMAN.value)
    try:
        risk_for(proposal, action)
    except ConfirmationError as exc:
        return ExecutionDecision(False, exc.code.value)
    if confirmation is None or approval is None or confirmation.status is not Status.APPROVED:
        return ExecutionDecision(False, Refusal.NOT_APPROVED.value)
    if now >= confirmation.expires_at:
        return ExecutionDecision(False, Refusal.EXPIRED.value)
    if (
        confirmation.proposal_id != proposal.proposal_id
        or confirmation.action != action
        or confirmation.approval_id != approval.approval_id
    ):
        return ExecutionDecision(False, Refusal.PROPOSAL_MISMATCH.value)
    decision = pdp.evaluate(
        principal,
        action,
        proposal.resource_uri,
        {"origin": proposal.origin, "risk": confirmation.risk.value, "kind": proposal.kind.value},
        approval=approval,
        payload=execution_payload(proposal, action),
        now=now,
    )
    return ExecutionDecision(decision.effect == "ALLOW", decision.reason_code, decision)


def consume(c: Confirmation, *, now: datetime) -> Confirmation:
    if c.status is not Status.APPROVED:
        raise ConfirmationError(Refusal.NOT_APPROVED)
    return replace(c, status=Status.CONSUMED, audit=(*c.audit, _entry("CONSUMED", now)))


__all__ = [
    "ACTIONS",
    "CONFIRMATION_SCHEMA_VERSION",
    "DEFAULT_TTL",
    "MAX_TTL",
    "STEP_UP_MAX_AGE_SECONDS",
    "Confirmation",
    "ConfirmationError",
    "ExecutionDecision",
    "Refusal",
    "RejectReason",
    "RiskClass",
    "Status",
    "StepUp",
    "approve",
    "authorize_execution",
    "consume",
    "execution_payload",
    "expire",
    "reject",
    "request_confirmation",
    "risk_for",
]
