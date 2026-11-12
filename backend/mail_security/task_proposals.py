"""Capability-scoped task proposals from mail + PDP adapters (Task 7.5, R-011/R-019).

A *proposal* is the only thing a safe actionable message may produce
(R-019.1): minimal normalized facts, opaque evidence refs, a sensitivity
class and exactly one requested capability. It is not a task, grants
nothing, and is decided by NerveCenter's existing PDP (``backend.policy.pdp``)
with principal, capability, resource, sensitivity, evidence and approval
context (R-011.2).

Contract (``mail-task-proposal@1``):

* **Closed kinds.** Calendar event, bill reminder, relationship follow-up.
  Each kind maps to exactly one capability, one ``resource.propose:*``
  action and one ``nc://personal/nervecenter/<area>/proposals/<id>``
  resource. The capability is derived from the kind; a caller cannot name
  another one (R-019.2: no tool authority from email text).
* **No free text.** Every string is an enum, an opaque reference or a
  bounded code; models forbid extra fields. A calendar title is a template
  code, a bill has no account number, payment link or "pay" capability, and
  URLs are rejected everywhere. Instructions, urgency or "prior approval"
  claims in a message have no field to land in (R-011.4).
* **Safe source only.** Proposals are built only from ``SAFE_TO_PROCESS``
  verdicts with no open verification advice (R-011.3, R-013.4).
* **Machines never act on email-origin proposals.** The adapter admits
  only ``human`` principals before the PDP, on top of the PDP's own
  no-wildcard rule for machine principals.
* **Approval is exact.** A PDP approval binding is checked against the
  proposal's exact payload hash, target URI and action; one approval never
  authorizes a different proposal.

Proposing is not executing: the execution actions (create event, schedule
reminder, payment, sending) are a separate, later confirmation step (7.6).
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from backend.policy.pdp import Decision, PolicyDecisionPoint
from schemas.nc_platform import NCApproval, NCPolicy, NCPrincipal

PROPOSAL_SCHEMA_VERSION = "mail-task-proposal@1"
PROPOSAL_POLICY_VERSION = "mail-task-proposal-policy@1"
ORIGIN_EMAIL = "EMAIL"
NAMESPACE = "personal"

_REF_RE = re.compile(r"^[A-Za-z0-9:_.-]{1,160}$")
_SCOPE_RE = re.compile(r"^conn:[0-9a-f-]{36}$")
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
MAX_EVIDENCE_REFS = 16
MAX_AMOUNT_MINOR = 1_000_000_000

#: The only principal kind that may act on an email-origin proposal; every
#: other kind (agent, workflow, function, connector, device, api_client) is a
#: machine and is refused before the PDP.
HUMAN_KIND = "human"


class ProposalKind(StrEnum):
    CALENDAR_EVENT = "CALENDAR_EVENT"
    BILL_REMINDER = "BILL_REMINDER"
    RELATIONSHIP_FOLLOWUP = "RELATIONSHIP_FOLLOWUP"


@dataclass(frozen=True)
class KindSpec:
    capability: str
    action: str
    area: str
    sensitivity: str


KIND_SPECS: dict[ProposalKind, KindSpec] = {
    ProposalKind.CALENDAR_EVENT: KindSpec(
        "personal.calendar.propose",
        "resource.propose:calendar.events",
        "calendar",
        "PERSONAL_PRIVATE",
    ),
    ProposalKind.BILL_REMINDER: KindSpec(
        "personal.bills.propose",
        "resource.propose:bills.reminders",
        "bills",
        "FINANCIAL",
    ),
    ProposalKind.RELATIONSHIP_FOLLOWUP: KindSpec(
        "personal.relationships.propose",
        "resource.propose:relationships.followups",
        "relationships",
        "PERSONAL_PRIVATE",
    ),
}


class ProposalRefusal(StrEnum):
    NOT_SAFE = "NOT_SAFE"
    VERIFICATION_REQUIRED = "VERIFICATION_REQUIRED"
    MACHINE_PRINCIPAL = "MACHINE_PRINCIPAL"
    INVALID = "INVALID"


class ProposalError(ValueError):
    def __init__(self, code: ProposalRefusal) -> None:
        super().__init__(code.value)
        self.code = code


def _opaque(value: str) -> str:
    if not isinstance(value, str) or not _REF_RE.fullmatch(value) or "//" in value:
        raise ValueError("must be an opaque reference")
    return value


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("must be timezone-aware")
    return value


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CalendarTitle(StrEnum):
    MEETING = "MEETING"
    APPOINTMENT = "APPOINTMENT"
    CALL = "CALL"
    DEADLINE = "DEADLINE"


class CalendarFacts(_Strict):
    kind: Literal["CALENDAR_EVENT"] = "CALENDAR_EVENT"
    title: CalendarTitle
    starts_at: datetime
    duration_minutes: int = Field(ge=5, le=480)
    counterpart_ref: str | None = None

    _starts = field_validator("starts_at")(_aware)

    @field_validator("counterpart_ref")
    @classmethod
    def _ref(cls, v: str | None) -> str | None:
        return None if v is None else _opaque(v)


class BillFacts(_Strict):
    kind: Literal["BILL_REMINDER"] = "BILL_REMINDER"
    due_date: date
    amount_minor: int | None = Field(default=None, ge=0, le=MAX_AMOUNT_MINOR)
    currency: str | None = None
    payee_ref: str

    @field_validator("currency")
    @classmethod
    def _currency(cls, v: str | None) -> str | None:
        if v is not None and not _CURRENCY_RE.fullmatch(v):
            raise ValueError("currency must be ISO-4217")
        return v

    @field_validator("payee_ref")
    @classmethod
    def _payee(cls, v: str) -> str:
        return _opaque(v)

    @model_validator(mode="after")
    def _amount_needs_currency(self) -> BillFacts:
        if (self.amount_minor is None) != (self.currency is None):
            raise ValueError("amount and currency go together")
        return self


class FollowupReason(StrEnum):
    REPLY_PENDING = "REPLY_PENDING"
    CHECK_IN = "CHECK_IN"
    THANK_YOU = "THANK_YOU"


class RelationshipFacts(_Strict):
    kind: Literal["RELATIONSHIP_FOLLOWUP"] = "RELATIONSHIP_FOLLOWUP"
    person_ref: str
    follow_up_on: date
    reason: FollowupReason

    @field_validator("person_ref")
    @classmethod
    def _person(cls, v: str) -> str:
        return _opaque(v)


Facts = CalendarFacts | BillFacts | RelationshipFacts
_FACTS_FOR: dict[ProposalKind, type[_Strict]] = {
    ProposalKind.CALENDAR_EVENT: CalendarFacts,
    ProposalKind.BILL_REMINDER: BillFacts,
    ProposalKind.RELATIONSHIP_FOLLOWUP: RelationshipFacts,
}


class TaskProposal(_Strict):
    """The immutable proposal payload the PDP and any approval bind to."""

    schema_version: Literal["mail-task-proposal@1"] = PROPOSAL_SCHEMA_VERSION
    proposal_id: str
    origin: Literal["EMAIL"] = ORIGIN_EMAIL
    account_scope: str
    assessment_id: str
    communication_ref: str | None = None
    evidence_refs: tuple[str, ...] = ()
    kind: ProposalKind
    requested_capability: str
    sensitivity: str
    disposition: Literal["SAFE_TO_PROCESS"]
    facts: Facts

    @field_validator("account_scope")
    @classmethod
    def _scope(cls, v: str) -> str:
        if not _SCOPE_RE.fullmatch(v):
            raise ValueError("account scope must be conn:<uuid>")
        return v

    @field_validator("assessment_id", "communication_ref", "proposal_id")
    @classmethod
    def _refs(cls, v: str | None) -> str | None:
        return None if v is None else _opaque(v)

    @field_validator("evidence_refs")
    @classmethod
    def _evidence(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        if len(v) > MAX_EVIDENCE_REFS:
            raise ValueError("too many evidence refs")
        return tuple(_opaque(r) for r in v)

    @model_validator(mode="after")
    def _derived(self) -> TaskProposal:
        spec = KIND_SPECS[self.kind]
        if self.requested_capability != spec.capability:
            raise ValueError("capability is derived from the kind")
        if self.sensitivity != spec.sensitivity:
            raise ValueError("sensitivity is derived from the kind")
        if not isinstance(self.facts, _FACTS_FOR[self.kind]):
            raise ValueError("facts do not match the kind")
        return self

    @property
    def resource_uri(self) -> str:
        return f"nc://{NAMESPACE}/nervecenter/{KIND_SPECS[self.kind].area}/proposals/{self.proposal_id}"

    @property
    def action(self) -> str:
        return KIND_SPECS[self.kind].action

    def payload(self) -> dict[str, Any]:
        """Canonical JSON payload (what an approval's payload hash covers)."""
        return self.model_dump(mode="json")


def proposal_id_for(account_scope: str, assessment_id: str, kind: ProposalKind, facts: Any) -> str:
    material = json.dumps(
        [account_scope, assessment_id, kind.value, facts.model_dump(mode="json")],
        sort_keys=True,
        separators=(",", ":"),
    )
    return "tp-" + hashlib.sha256(material.encode()).hexdigest()[:24]


def build_proposal(
    *,
    account_scope: str,
    assessment_id: str,
    disposition: str,
    verification_advice: tuple[str, ...] | list[str] = (),
    kind: ProposalKind,
    facts: Facts,
    communication_ref: str | None = None,
    evidence_refs: tuple[str, ...] = (),
) -> TaskProposal:
    """Build one proposal, or refuse with a stable code."""
    if disposition != "SAFE_TO_PROCESS":
        raise ProposalError(ProposalRefusal.NOT_SAFE)
    if verification_advice:
        raise ProposalError(ProposalRefusal.VERIFICATION_REQUIRED)
    spec = KIND_SPECS[kind]
    try:
        return TaskProposal(
            proposal_id=proposal_id_for(account_scope, assessment_id, kind, facts),
            account_scope=account_scope,
            assessment_id=assessment_id,
            communication_ref=communication_ref,
            evidence_refs=tuple(evidence_refs),
            kind=kind,
            requested_capability=spec.capability,
            sensitivity=spec.sensitivity,
            disposition="SAFE_TO_PROCESS",
            facts=facts,
        )
    except ValueError as exc:
        raise ProposalError(ProposalRefusal.INVALID) from exc


# -- PDP adapter ----------------------------------------------------------------------


@dataclass(frozen=True)
class ProposalDecision:
    allowed: bool
    reason_code: str
    pdp: Decision | None = None


def pdp_context(proposal: TaskProposal) -> dict[str, Any]:
    """Scalar-only PDP context (capability, sensitivity, evidence, origin)."""
    return {
        "origin": proposal.origin,
        "kind": proposal.kind.value,
        "capability": proposal.requested_capability,
        "sensitivity": proposal.sensitivity,
        "disposition": proposal.disposition,
        "evidence_count": len(proposal.evidence_refs),
        "policy_version": PROPOSAL_POLICY_VERSION,
    }


def authorize_proposal(
    pdp: PolicyDecisionPoint,
    principal: NCPrincipal,
    proposal: TaskProposal,
    *,
    approval: NCApproval | None = None,
    now: datetime | None = None,
) -> ProposalDecision:
    """Decide whether ``principal`` may *see/accept* this proposal.

    NerveShield invariants first (fail closed), then the existing PDP with
    the proposal's exact action, resource URI, scalar context and, when
    given, an approval bound to the exact payload.
    """
    if not isinstance(principal, NCPrincipal) or principal.kind != HUMAN_KIND:
        return ProposalDecision(False, ProposalRefusal.MACHINE_PRINCIPAL.value)
    if proposal.disposition != "SAFE_TO_PROCESS":  # unreachable by schema; defensive
        return ProposalDecision(False, ProposalRefusal.NOT_SAFE.value)
    decision = pdp.evaluate(
        principal,
        proposal.action,
        proposal.resource_uri,
        pdp_context(proposal),
        approval=approval,
        payload=proposal.payload() if approval is not None else None,
        now=now,
    )
    return ProposalDecision(decision.effect == "ALLOW", decision.reason_code, decision)


#: Fixed policy ids (pol_ + ULID-shaped body) for the default proposal bundle.
POLICY_ALLOW_HUMAN_PROPOSALS = "pol_01JNS7T5PRP0SA1A110W000001"
POLICY_DENY_UNSAFE_EMAIL = "pol_01JNS7T5PRP0SADENY00000002"


def default_proposal_policies() -> list[NCPolicy]:
    """Default bundle: humans in ``personal`` may receive email-origin
    proposals from SAFE verdicts; anything else from email is denied."""
    return [
        NCPolicy(
            id="row-ns-proposal-allow",
            policy_id=POLICY_ALLOW_HUMAN_PROPOSALS,
            effect="ALLOW",
            principal_match="kind:human,namespace:personal",
            action_match="resource.propose:*",
            resource_match="nc://personal/nervecenter/*",
            condition='origin == "EMAIL" && disposition == "SAFE_TO_PROCESS"',
            priority=10,
        ),
        NCPolicy(
            id="row-ns-proposal-deny-unsafe",
            policy_id=POLICY_DENY_UNSAFE_EMAIL,
            effect="DENY",
            principal_match="*",
            action_match="resource.propose:*",
            resource_match="nc://personal/nervecenter/*",
            condition='origin == "EMAIL" && disposition != "SAFE_TO_PROCESS"',
            priority=100,
        ),
    ]


__all__ = [
    "KIND_SPECS",
    "HUMAN_KIND",
    "ORIGIN_EMAIL",
    "POLICY_ALLOW_HUMAN_PROPOSALS",
    "POLICY_DENY_UNSAFE_EMAIL",
    "PROPOSAL_POLICY_VERSION",
    "PROPOSAL_SCHEMA_VERSION",
    "BillFacts",
    "CalendarFacts",
    "CalendarTitle",
    "FollowupReason",
    "ProposalDecision",
    "ProposalError",
    "ProposalKind",
    "ProposalRefusal",
    "RelationshipFacts",
    "TaskProposal",
    "authorize_proposal",
    "build_proposal",
    "default_proposal_policies",
    "pdp_context",
    "proposal_id_for",
]
