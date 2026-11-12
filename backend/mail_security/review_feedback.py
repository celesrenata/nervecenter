"""Reviewer feedback mapping and independent-verification advice (Task 7.2).

Pure helpers used by the feedback API. Nothing here writes anywhere.

Reviewer verdicts are what a person clicks. They cover both wordings in the
spec (R-020.2 ``safe/suspicious/misclassified/not relevant`` and task 7.2
``safe/suspicious/unimportant/wrong intent``):

* ``SAFE`` -> ``FALSE_POSITIVE`` when the verdict flagged the message,
  otherwise ``CONFIRMED_SAFE`` (audit only).
* ``SUSPICIOUS`` -> ``FALSE_NEGATIVE`` when the verdict did not flag it,
  otherwise ``CONFIRMED_THREAT``.
* ``NOT_RELEVANT`` (unimportant) -> ``NOT_RELEVANT`` (audit only).
* ``WRONG_INTENT`` (misclassified / wrong category) -> ``CATEGORY_CORRECTION``.

No verdict creates a trust allowlist. ``SAFE`` at most yields a
``PERSONAL_MEMORY`` proposal in ``PENDING_REVIEW`` for the same account; it
is never applied here and never touches intel overrides or policy.

Verification advice is derived from the assessment's own findings and reason
codes, never from message text. It is shown even when the sender
authenticates (R-013.4): a verified sender can still make a bad request.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from enum import StrEnum

from backend.mail_security.feedback import FeedbackKind


class ReviewerVerdict(StrEnum):
    SAFE = "SAFE"
    SUSPICIOUS = "SUSPICIOUS"
    NOT_RELEVANT = "NOT_RELEVANT"
    WRONG_INTENT = "WRONG_INTENT"


class VerificationAdvice(StrEnum):
    VERIFY_PAYMENT_REQUEST = "VERIFY_PAYMENT_REQUEST"
    VERIFY_CREDENTIAL_REQUEST = "VERIFY_CREDENTIAL_REQUEST"
    VERIFY_HIGH_IMPACT_REQUEST = "VERIFY_HIGH_IMPACT_REQUEST"
    VERIFY_SENDER_IDENTITY = "VERIFY_SENDER_IDENTITY"


#: Dispositions where the verdict flagged the message.
FLAGGED_DISPOSITIONS = frozenset({"REVIEW", "RESTRICTED", "INCOMPLETE"})

#: Finding statuses that count as a real signal for advice.
_ACTIVE_STATUSES = frozenset({"OBSERVED", "SUSPECTED"})

_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


def kind_for_verdict(verdict: ReviewerVerdict, disposition: str | None) -> FeedbackKind:
    """Map a reviewer verdict onto a 5.5 ledger kind using the stored disposition."""
    flagged = (disposition or "") in FLAGGED_DISPOSITIONS
    if verdict is ReviewerVerdict.SAFE:
        return FeedbackKind.FALSE_POSITIVE if flagged else FeedbackKind.CONFIRMED_SAFE
    if verdict is ReviewerVerdict.SUSPICIOUS:
        return FeedbackKind.CONFIRMED_THREAT if flagged else FeedbackKind.FALSE_NEGATIVE
    if verdict is ReviewerVerdict.NOT_RELEVANT:
        return FeedbackKind.NOT_RELEVANT
    return FeedbackKind.CATEGORY_CORRECTION


def reviewer_ref(subject: str) -> str:
    """Opaque, stable reviewer reference (never the raw subject)."""
    return "rev:" + hashlib.sha256(f"reviewer:{subject}".encode()).hexdigest()[:32]


def valid_idempotency_key(key: str | None) -> bool:
    return key is None or bool(_IDEMPOTENCY_RE.fullmatch(key))


def feedback_id(
    account_scope: str,
    assessment_id: str,
    reviewer: str,
    verdict: ReviewerVerdict,
    idempotency_key: str,
) -> str:
    """Deterministic id so a replayed submission resolves to the same record."""
    material = "\x1f".join([account_scope, assessment_id, reviewer, verdict.value, idempotency_key])
    return "fb:" + hashlib.sha256(material.encode()).hexdigest()[:40]


def verification_advice(
    findings: Iterable[tuple[str, str]], reason_codes: Iterable[str]
) -> list[str]:
    """Advice codes from ``(category, status)`` findings and policy reason codes."""
    active = {cat for cat, status in findings if status in _ACTIVE_STATUSES}
    reasons = set(reason_codes)
    out: set[VerificationAdvice] = set()
    if "PAYMENT_FRAUD" in active:
        out.add(VerificationAdvice.VERIFY_PAYMENT_REQUEST)
    if "PHISHING" in active:
        out.add(VerificationAdvice.VERIFY_CREDENTIAL_REQUEST)
    if not out and ("SOCIAL_ENGINEERING" in active or "CREDENTIAL_OR_PAYMENT_ASK" in reasons):
        out.add(VerificationAdvice.VERIFY_HIGH_IMPACT_REQUEST)
    if "SENDER_IMPERSONATION" in active or "SENDER_IMPERSONATION_FINDING" in reasons:
        out.add(VerificationAdvice.VERIFY_SENDER_IDENTITY)
    return sorted(a.value for a in out)


__all__ = [
    "FLAGGED_DISPOSITIONS",
    "ReviewerVerdict",
    "VerificationAdvice",
    "feedback_id",
    "kind_for_verdict",
    "reviewer_ref",
    "valid_idempotency_key",
    "verification_advice",
]
