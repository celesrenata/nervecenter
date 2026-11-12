"""Security review queue: impact prioritization and attention budgeting (Task 7.3).

Pure and deterministic: same candidates + same clock + same settings give the
same queue. Nothing here writes, notifies, mints attention or acts on mail.
The queue is a separate ``security.review`` surface (design §8.3); its items
cannot authorize anything.

Rules (R-012, R-013, R-020.5 "calm, prioritized, not every threat urgent"):

* **Who enters.** Only flagged verdicts (REVIEW / RESTRICTED / INCOMPLETE)
  that nobody has reviewed yet. SAFE_TO_PROCESS / LOW_PRIORITY never enter.
* **Impact.** A transparent additive score: disposition base (RESTRICTED 3,
  REVIEW 2, INCOMPLETE 1), +2 payment/credential verification advice, +1 an
  active HIGH/CRITICAL finding, +1 important human sender (VIP), +1 linked
  real commitments. Tier: >=6 CRITICAL, >=4 HIGH, >=2 MEDIUM, else LOW.
* **Interrupts are scarce.** Only CRITICAL items that arrived *today* (novel)
  may interrupt, at most ``daily_interrupt_cap`` per day, earliest arrivals
  first. During an active quiet period nothing interrupts; deferred items
  stay in the queue with ``deliver_after`` = the quiet period's end.
* **Digest, not noise.** LOW items go to an aggregated digest (counts by
  primary reason code) unless the sender is an important human or the
  message carries real commitments: those are never digested or dropped.
  Beyond ``visible_cap`` queued items, MEDIUM ones overflow into the digest;
  CRITICAL, HIGH and protected items always stay visible.
* **Commitments behind restricted mail.** Commitments / follow-ups /
  attention items linked to a flagged message are preserved as
  ``REVIEW_CANDIDATE`` refs on the queue item: kept, shown, never promoted
  or acted on automatically.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

QUEUE_POLICY_VERSION = "mail-review-queue@2"

QUEUED_DISPOSITIONS = frozenset({"REVIEW", "RESTRICTED", "INCOMPLETE"})
_BASE = {"RESTRICTED": 3, "REVIEW": 2, "INCOMPLETE": 1}

#: SHADOW conversation-continuity statuses that queue a message for review
#: regardless of its disposition (NS-ADR-067). They add a reason and weight
#: only; the disposition itself is never changed. BROKEN alone is MEDIUM
#: (visible); SUSPECT alone is LOW (digest, unless protected), because a
#: legitimate new participant looks exactly like injection (spike 8.2).
THREAD_WEIGHTS = {"BROKEN": 3, "SUSPECT": 1}
_HIGH_IMPACT_ADVICE = frozenset({"VERIFY_PAYMENT_REQUEST", "VERIFY_CREDENTIAL_REQUEST"})
_HIGH_SEVERITIES = frozenset({"HIGH", "CRITICAL"})
_ACTIVE = frozenset({"OBSERVED", "SUSPECTED"})

DEFAULT_DAILY_INTERRUPT_CAP = 1
DEFAULT_VISIBLE_CAP = 10


class ImpactTier(StrEnum):
    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


_TIER_RANK = {ImpactTier.CRITICAL: 0, ImpactTier.HIGH: 1, ImpactTier.MEDIUM: 2, ImpactTier.LOW: 3}


class Delivery(StrEnum):
    INTERRUPT = "INTERRUPT"  # may notify now (still SHADOW: informational only)
    QUEUE = "QUEUE"  # visible in the review queue, no notification
    DIGEST = "DIGEST"  # counted in the aggregated digest only


class QueueReason(StrEnum):
    BASE_RESTRICTED = "BASE_RESTRICTED"
    BASE_REVIEW = "BASE_REVIEW"
    BASE_INCOMPLETE = "BASE_INCOMPLETE"
    PAYMENT_OR_CREDENTIAL_ASK = "PAYMENT_OR_CREDENTIAL_ASK"
    HIGH_SEVERITY_FINDING = "HIGH_SEVERITY_FINDING"
    IMPORTANT_HUMAN_SENDER = "IMPORTANT_HUMAN_SENDER"
    COMMITMENTS_PRESERVED = "COMMITMENTS_PRESERVED"
    INTERRUPT_NOVEL_CRITICAL = "INTERRUPT_NOVEL_CRITICAL"
    INTERRUPT_DEFERRED_QUIET_PERIOD = "INTERRUPT_DEFERRED_QUIET_PERIOD"
    INTERRUPT_BUDGET_EXHAUSTED = "INTERRUPT_BUDGET_EXHAUSTED"
    NOT_NOVEL = "NOT_NOVEL"
    LOW_IMPACT_DIGESTED = "LOW_IMPACT_DIGESTED"
    PROTECTED_FROM_DIGEST = "PROTECTED_FROM_DIGEST"
    VISIBLE_CAP_OVERFLOW = "VISIBLE_CAP_OVERFLOW"
    THREAD_BROKEN = "THREAD_BROKEN"
    THREAD_SUSPECT = "THREAD_SUSPECT"


@dataclass(frozen=True)
class CommitmentRef:
    kind: str  # "commitment" | "followup" | "attention"
    ref_id: str
    status: str = "REVIEW_CANDIDATE"


@dataclass(frozen=True)
class QueueCandidate:
    account_scope: str
    assessment_id: str
    disposition: str
    created_at: datetime
    communication_id: str | None = None
    reason_codes: tuple[str, ...] = ()
    findings: tuple[tuple[str, str, str], ...] = ()  # (category, severity, status)
    verification_advice: tuple[str, ...] = ()
    important_sender: bool = False
    commitments: tuple[CommitmentRef, ...] = ()
    reviewed: bool = False
    #: SHADOW conversation-continuity status (``SUSPECT`` / ``BROKEN``), if any.
    thread_continuity: str | None = None


def is_queued(c: QueueCandidate) -> bool:
    """Queued by disposition, or by a SHADOW thread-continuity flag."""
    return c.disposition in QUEUED_DISPOSITIONS or c.thread_continuity in THREAD_WEIGHTS


@dataclass(frozen=True)
class QueueSettings:
    daily_interrupt_cap: int = DEFAULT_DAILY_INTERRUPT_CAP
    visible_cap: int = DEFAULT_VISIBLE_CAP
    quiet_until: datetime | None = None  # end of the active quiet period, if any


@dataclass(frozen=True)
class QueueItem:
    candidate: QueueCandidate
    score: int
    tier: ImpactTier
    delivery: Delivery
    reasons: tuple[QueueReason, ...]
    deliver_after: datetime | None = None


@dataclass(frozen=True)
class Digest:
    count: int
    by_reason: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class QueueResult:
    items: tuple[QueueItem, ...]
    digest: Digest
    excluded_reviewed: int
    policy_version: str = QUEUE_POLICY_VERSION


def score(c: QueueCandidate) -> tuple[int, list[QueueReason]]:
    reasons: list[QueueReason] = []
    total = 0
    if c.disposition in _BASE:
        reasons.append(QueueReason(f"BASE_{c.disposition}"))
        total += _BASE[c.disposition]
    if c.thread_continuity in THREAD_WEIGHTS:
        total += THREAD_WEIGHTS[c.thread_continuity]
        reasons.append(QueueReason(f"THREAD_{c.thread_continuity}"))
    if _HIGH_IMPACT_ADVICE & set(c.verification_advice):
        total += 2
        reasons.append(QueueReason.PAYMENT_OR_CREDENTIAL_ASK)
    if any(sev in _HIGH_SEVERITIES and st in _ACTIVE for _cat, sev, st in c.findings):
        total += 1
        reasons.append(QueueReason.HIGH_SEVERITY_FINDING)
    if c.important_sender:
        total += 1
        reasons.append(QueueReason.IMPORTANT_HUMAN_SENDER)
    if c.commitments:
        total += 1
        reasons.append(QueueReason.COMMITMENTS_PRESERVED)
    return total, reasons


def tier_for(total: int) -> ImpactTier:
    if total >= 6:
        return ImpactTier.CRITICAL
    if total >= 4:
        return ImpactTier.HIGH
    if total >= 2:
        return ImpactTier.MEDIUM
    return ImpactTier.LOW


def _protected(c: QueueCandidate) -> bool:
    return c.important_sender or bool(c.commitments)


def _primary_reason(c: QueueCandidate) -> str:
    if c.disposition not in QUEUED_DISPOSITIONS and c.thread_continuity in THREAD_WEIGHTS:
        return f"THREAD_{c.thread_continuity}"
    return c.reason_codes[0] if c.reason_codes else f"DISPOSITION_{c.disposition}"


def build_queue(
    candidates: list[QueueCandidate], *, now: datetime, settings: QueueSettings | None = None
) -> QueueResult:
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    cfg = settings or QueueSettings()
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    quiet = cfg.quiet_until is not None and cfg.quiet_until > now

    pending = [c for c in candidates if is_queued(c) and not c.reviewed]
    excluded = sum(1 for c in candidates if is_queued(c) and c.reviewed)

    scored: list[tuple[QueueCandidate, int, ImpactTier, list[QueueReason]]] = []
    for c in pending:
        total, reasons = score(c)
        scored.append((c, total, tier_for(total), reasons))

    # Interrupt budget: earliest novel CRITICAL arrivals of today, capped.
    novel_critical = sorted(
        (s for s in scored if s[2] is ImpactTier.CRITICAL and day_start <= s[0].created_at <= now),
        key=lambda s: (s[0].created_at, s[0].assessment_id),
    )
    budgeted = {s[0].assessment_id for s in novel_critical[: max(0, cfg.daily_interrupt_cap)]}

    items: list[QueueItem] = []
    digest_reasons: Counter[str] = Counter()
    for c, total, tier, reasons in scored:
        deliver_after = None
        if tier is ImpactTier.CRITICAL:
            if not (day_start <= c.created_at <= now):
                delivery, extra = Delivery.QUEUE, QueueReason.NOT_NOVEL
            elif c.assessment_id not in budgeted:
                delivery, extra = Delivery.QUEUE, QueueReason.INTERRUPT_BUDGET_EXHAUSTED
            elif quiet:
                delivery, extra = Delivery.QUEUE, QueueReason.INTERRUPT_DEFERRED_QUIET_PERIOD
                deliver_after = cfg.quiet_until
            else:
                delivery, extra = Delivery.INTERRUPT, QueueReason.INTERRUPT_NOVEL_CRITICAL
            reasons = [*reasons, extra]
        elif tier is ImpactTier.LOW:
            if _protected(c):
                delivery = Delivery.QUEUE
                reasons = [*reasons, QueueReason.PROTECTED_FROM_DIGEST]
            else:
                delivery = Delivery.DIGEST
                reasons = [*reasons, QueueReason.LOW_IMPACT_DIGESTED]
        else:
            delivery = Delivery.QUEUE
        items.append(
            QueueItem(c, total, tier, delivery, tuple(reasons), deliver_after=deliver_after)
        )

    items.sort(
        key=lambda i: (
            _TIER_RANK[i.tier],
            -i.score,
            -i.candidate.created_at.timestamp(),
            i.candidate.assessment_id,
        )
    )

    # Visible cap: only unprotected MEDIUM items overflow into the digest.
    visible = 0
    final: list[QueueItem] = []
    for item in items:
        if item.delivery is Delivery.DIGEST:
            digest_reasons[_primary_reason(item.candidate)] += 1
            final.append(item)
            continue
        overflow = (
            visible >= cfg.visible_cap
            and item.tier is ImpactTier.MEDIUM
            and not _protected(item.candidate)
        )
        if overflow:
            digest_reasons[_primary_reason(item.candidate)] += 1
            final.append(
                QueueItem(
                    item.candidate,
                    item.score,
                    item.tier,
                    Delivery.DIGEST,
                    (*item.reasons, QueueReason.VISIBLE_CAP_OVERFLOW),
                )
            )
            continue
        visible += 1
        final.append(item)

    return QueueResult(
        items=tuple(final),
        digest=Digest(
            count=sum(digest_reasons.values()),
            by_reason=dict(sorted(digest_reasons.items())),
        ),
        excluded_reviewed=excluded,
    )


def quiet_until_from(period_end: datetime | None, now: datetime) -> datetime | None:
    """Normalize an active quiet period end (``None`` when not active)."""
    if period_end is None or period_end <= now:
        return None
    return period_end


__all__ = [
    "QUEUED_DISPOSITIONS",
    "QUEUE_POLICY_VERSION",
    "CommitmentRef",
    "Delivery",
    "Digest",
    "ImpactTier",
    "QueueCandidate",
    "QueueItem",
    "QueueReason",
    "QueueResult",
    "QueueSettings",
    "build_queue",
    "quiet_until_from",
    "score",
    "tier_for",
]
