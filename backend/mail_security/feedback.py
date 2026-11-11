"""Typed review feedback with provenance and store boundaries (NerveShield task 5.5).

Library only; nothing calls it yet. Feedback is recorded in an append-only
ledger and turned into *proposals* for three separate stores (R-015, R-016):

* ``THREAT_INTEL`` - candidate indicators for the expiring intel store.
* ``PERSONAL_MEMORY`` - account-scoped trust notes for the trust index.
* ``TRAINING`` - candidate examples for the Phase 6 dataset registry.

Nothing here writes to any store, changes model weights or promotes a global
indicator. A proposal only becomes ``APPROVED`` with an explicit approval
reference from an eligible human source, and even then this module does not
apply it: a separate, later executor must (design: "typed corrections, no
automatic retraining"). Boundary rules:

* Provider labels and model observations are weak signals, never feedback.
  They are refused as feedback sources.
* Intel proposals are ACCOUNT scope by default. GLOBAL scope needs an
  ``ANALYST`` source, and is never derived from personal-memory feedback.
* Training proposals need a recorded consent reference (Phase 6) and stay
  ``BLOCKED_NO_CONSENT`` without one; they never mix accounts.
* Personal-memory proposals stay in the account they came from.
* Records hold enum codes and opaque references only: no free text, no
  addresses, no content. Every proposal keeps the feedback id it came from.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum

FEEDBACK_SCHEMA_VERSION = "mail-feedback@1"
_REF_RE = re.compile(r"^[A-Za-z0-9:_.-]{1,200}$")
_ACCOUNT_RE = re.compile(r"^[A-Za-z0-9:_-]{1,128}$")
_CODE_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
MAX_REFS = 16


class FeedbackKind(StrEnum):
    FALSE_POSITIVE = "FALSE_POSITIVE"  # flagged, but legitimate
    FALSE_NEGATIVE = "FALSE_NEGATIVE"  # missed something harmful
    CONFIRMED_THREAT = "CONFIRMED_THREAT"
    CATEGORY_CORRECTION = "CATEGORY_CORRECTION"
    SENDER_KNOWN = "SENDER_KNOWN"  # owner knows this sender
    SENDER_UNKNOWN = "SENDER_UNKNOWN"  # owner does not know this sender
    NOT_SURE = "NOT_SURE"


class FeedbackSource(StrEnum):
    ACCOUNT_OWNER = "ACCOUNT_OWNER"
    DELEGATED_REVIEWER = "DELEGATED_REVIEWER"
    ANALYST = "ANALYST"
    # Weak signals: listed so they can be refused explicitly.
    PROVIDER_LABEL = "PROVIDER_LABEL"
    MODEL_OBSERVATION = "MODEL_OBSERVATION"


_HUMAN = frozenset(
    {FeedbackSource.ACCOUNT_OWNER, FeedbackSource.DELEGATED_REVIEWER, FeedbackSource.ANALYST}
)


class Destination(StrEnum):
    THREAT_INTEL = "THREAT_INTEL"
    PERSONAL_MEMORY = "PERSONAL_MEMORY"
    TRAINING = "TRAINING"


class Scope(StrEnum):
    ACCOUNT = "ACCOUNT"
    GLOBAL = "GLOBAL"


class ProposalStatus(StrEnum):
    PENDING_REVIEW = "PENDING_REVIEW"
    BLOCKED_NO_CONSENT = "BLOCKED_NO_CONSENT"
    APPROVED = "APPROVED"  # approved for a later executor; NOT applied here
    REJECTED = "REJECTED"
    SUPERSEDED = "SUPERSEDED"


class FeedbackRefusal(StrEnum):
    INVALID_ACCOUNT = "INVALID_ACCOUNT"
    INVALID_REFERENCE = "INVALID_REFERENCE"
    INVALID_CODE = "INVALID_CODE"
    WEAK_SOURCE_NOT_FEEDBACK = "WEAK_SOURCE_NOT_FEEDBACK"
    NAIVE_TIMESTAMP = "NAIVE_TIMESTAMP"
    UNKNOWN_PROPOSAL = "UNKNOWN_PROPOSAL"
    NOT_PENDING = "NOT_PENDING"
    APPROVER_NOT_ELIGIBLE = "APPROVER_NOT_ELIGIBLE"
    WRONG_ACCOUNT = "WRONG_ACCOUNT"
    INVALID_APPROVAL = "INVALID_APPROVAL"


class FeedbackError(ValueError):
    def __init__(self, code: FeedbackRefusal) -> None:
        super().__init__(code.value)
        self.code = code


@dataclass(frozen=True)
class FeedbackRecord:
    """One typed correction. Codes and opaque references only."""

    feedback_id: str
    account_scope: str
    assessment_ref: str
    kind: FeedbackKind
    source: FeedbackSource
    reviewer_ref: str
    created_at: datetime
    reason_codes: tuple[str, ...] = ()
    indicator_refs: tuple[str, ...] = ()  # opaque refs to finding evidence, never values
    consent_ref: str | None = None  # training consent (Phase 6)
    request_global: bool = False  # ask for GLOBAL intel scope (analyst only)
    schema_version: str = FEEDBACK_SCHEMA_VERSION


@dataclass(frozen=True)
class Proposal:
    proposal_id: str
    feedback_id: str  # provenance: the record this came from
    account_scope: str
    destination: Destination
    scope: Scope
    kind: FeedbackKind
    source: FeedbackSource
    status: ProposalStatus
    indicator_refs: tuple[str, ...] = ()
    approval_ref: str | None = None
    decided_by: FeedbackSource | None = None
    history: tuple[str, ...] = field(default_factory=tuple)  # status trail


def _need(ok: bool, code: FeedbackRefusal) -> None:
    if not ok:
        raise FeedbackError(code)


def _validate(record: FeedbackRecord) -> None:
    _need(
        isinstance(record.account_scope, str) and bool(_ACCOUNT_RE.fullmatch(record.account_scope)),
        FeedbackRefusal.INVALID_ACCOUNT,
    )
    refs = [record.feedback_id, record.assessment_ref, record.reviewer_ref, *record.indicator_refs]
    if record.consent_ref is not None:
        refs.append(record.consent_ref)
    _need(
        len(record.indicator_refs) <= MAX_REFS
        and all(isinstance(r, str) and _REF_RE.fullmatch(r) for r in refs),
        FeedbackRefusal.INVALID_REFERENCE,
    )
    _need(
        len(record.reason_codes) <= MAX_REFS
        and all(isinstance(c, str) and _CODE_RE.fullmatch(c) for c in record.reason_codes),
        FeedbackRefusal.INVALID_CODE,
    )
    _need(FeedbackKind(record.kind) is record.kind, FeedbackRefusal.INVALID_CODE)
    _need(FeedbackSource(record.source) in _HUMAN, FeedbackRefusal.WEAK_SOURCE_NOT_FEEDBACK)
    _need(
        isinstance(record.created_at, datetime) and record.created_at.tzinfo is not None,
        FeedbackRefusal.NAIVE_TIMESTAMP,
    )


_INTEL_KINDS = frozenset({FeedbackKind.FALSE_NEGATIVE, FeedbackKind.CONFIRMED_THREAT})
_MEMORY_KINDS = frozenset(
    {FeedbackKind.SENDER_KNOWN, FeedbackKind.SENDER_UNKNOWN, FeedbackKind.FALSE_POSITIVE}
)
_TRAINING_KINDS = frozenset(
    {
        FeedbackKind.FALSE_POSITIVE,
        FeedbackKind.FALSE_NEGATIVE,
        FeedbackKind.CONFIRMED_THREAT,
        FeedbackKind.CATEGORY_CORRECTION,
    }
)


def route(record: FeedbackRecord) -> list[tuple[Destination, Scope, ProposalStatus]]:
    """Pure routing rules. NOT_SURE routes nowhere."""
    out: list[tuple[Destination, Scope, ProposalStatus]] = []
    if record.kind in _INTEL_KINDS and record.indicator_refs:
        wants_global = record.request_global and record.source is FeedbackSource.ANALYST
        out.append(
            (
                Destination.THREAT_INTEL,
                Scope.GLOBAL if wants_global else Scope.ACCOUNT,
                ProposalStatus.PENDING_REVIEW,
            )
        )
    if record.kind in _MEMORY_KINDS:
        out.append((Destination.PERSONAL_MEMORY, Scope.ACCOUNT, ProposalStatus.PENDING_REVIEW))
    if record.kind in _TRAINING_KINDS:
        status = (
            ProposalStatus.PENDING_REVIEW
            if record.consent_ref
            else ProposalStatus.BLOCKED_NO_CONSENT
        )
        out.append((Destination.TRAINING, Scope.ACCOUNT, status))
    return out


class FeedbackLedger:
    """Append-only feedback ledger and proposal queue (in memory)."""

    def __init__(self) -> None:
        self._records: list[FeedbackRecord] = []
        self._proposals: dict[str, Proposal] = {}
        self._latest: dict[tuple[str, str, str], str] = {}  # (account, assessment, dest) -> id

    def record(self, record: FeedbackRecord) -> list[Proposal]:
        _validate(record)
        if any(r.feedback_id == record.feedback_id for r in self._records):
            return [p for p in self._proposals.values() if p.feedback_id == record.feedback_id]
        self._records.append(record)
        made: list[Proposal] = []
        for n, (dest, scope, status) in enumerate(route(record)):
            key = (record.account_scope, record.assessment_ref, dest.value)
            previous = self._latest.get(key)
            if previous is not None:
                old = self._proposals[previous]
                if old.status in (ProposalStatus.PENDING_REVIEW, ProposalStatus.BLOCKED_NO_CONSENT):
                    self._proposals[previous] = replace(
                        old,
                        status=ProposalStatus.SUPERSEDED,
                        history=(*old.history, ProposalStatus.SUPERSEDED.value),
                    )
            pid = f"{record.feedback_id}:{n}"
            proposal = Proposal(
                proposal_id=pid,
                feedback_id=record.feedback_id,
                account_scope=record.account_scope,
                destination=dest,
                scope=scope,
                kind=record.kind,
                source=record.source,
                status=status,
                indicator_refs=record.indicator_refs if dest is Destination.THREAT_INTEL else (),
                history=(status.value,),
            )
            self._proposals[pid] = proposal
            self._latest[key] = pid
            made.append(proposal)
        return made

    def decide(
        self,
        proposal_id: str,
        *,
        account_scope: str,
        approve: bool,
        approver: FeedbackSource,
        approval_ref: str,
    ) -> Proposal:
        proposal = self._proposals.get(proposal_id)
        _need(proposal is not None, FeedbackRefusal.UNKNOWN_PROPOSAL)
        assert proposal is not None  # noqa: S101 - checked above
        _need(proposal.account_scope == account_scope, FeedbackRefusal.WRONG_ACCOUNT)
        _need(proposal.status is ProposalStatus.PENDING_REVIEW, FeedbackRefusal.NOT_PENDING)
        _need(
            isinstance(approval_ref, str) and bool(_REF_RE.fullmatch(approval_ref)),
            FeedbackRefusal.INVALID_APPROVAL,
        )
        eligible = approver in _HUMAN and (
            proposal.scope is not Scope.GLOBAL or approver is FeedbackSource.ANALYST
        )
        _need(eligible, FeedbackRefusal.APPROVER_NOT_ELIGIBLE)
        status = ProposalStatus.APPROVED if approve else ProposalStatus.REJECTED
        updated = replace(
            proposal,
            status=status,
            approval_ref=approval_ref,
            decided_by=approver,
            history=(*proposal.history, status.value),
        )
        self._proposals[proposal_id] = updated
        return updated

    def proposals(
        self, account_scope: str, destination: Destination | None = None
    ) -> list[Proposal]:
        return [
            p
            for p in self._proposals.values()
            if p.account_scope == account_scope
            and (destination is None or p.destination is destination)
        ]

    def records(self, account_scope: str) -> list[FeedbackRecord]:
        return [r for r in self._records if r.account_scope == account_scope]


def now_utc() -> datetime:
    return datetime.now(UTC)


__all__ = [
    "FEEDBACK_SCHEMA_VERSION",
    "Destination",
    "FeedbackError",
    "FeedbackKind",
    "FeedbackLedger",
    "FeedbackRecord",
    "FeedbackRefusal",
    "FeedbackSource",
    "Proposal",
    "ProposalStatus",
    "Scope",
    "route",
]
