"""Pure, deterministic commitment / request / deadline extractors (R103).

Each extractor is a *pure function* over one normalized
:class:`~backend.communications.models.Communication` plus the (transient)
resolved text — no global state, no clock reads, no model calls. Given the
same ``(communication, text, now)`` input it always returns the same
:class:`~backend.communications.detection.findings.DetectionReport`
(AC-R04: deterministic path fully green with assist off).

Coverage (prompt2 §16–§18):

- **Commitments** (§16) — first-person promises *the user made*
  (``direction == outgoing``): "I'll send it tonight.", "I'll call
  tomorrow.", "I can get that done Friday.", "Remind me next week.",
  "I'll take care of it.", "I'll check and get back to you."
- **Requests** (§17) — incoming asks of the user: "Can you send me...",
  "Please call...", "Are you available...", "Could you review...",
  "Need this by Friday.", "Can you confirm..."
- **Deadlines** (§18) — an explicit due expression (explicit date, relative
  date, business-hour constraint) on a communication that is not already an
  outgoing first-person promise (the due then lives on the commitment).

Findings carry only ``created_from`` (the communication id), the
``content_ref`` pointer, and short metadata — never the plaintext body
(no-plaintext policy, §7 / R107).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Sequence

from backend.communications.models import (
    Communication,
    CommunicationDirection,
)

from .dates import parse_due_expression
from .findings import (
    DETERMINISTIC_DETECTOR,
    Commitment,
    Deadline,
    DetectionReport,
    DueInfo,
    FindingStatus,
    Request,
)

_SHORT_MAX = 80


def _short(s: str, n: int = _SHORT_MAX) -> str:
    s = " ".join((s or "").split())
    if len(s) <= n:
        return s
    return s[: n - 1] + "…"


# ---------------------------------------------------------------------------
# commitment patterns (§16 — first-person promises made by the user)
# ---------------------------------------------------------------------------
#: first-person future promise: "I'll <verb> ..." / "I will <verb> ..."
_PROMISE_FUTURE = re.compile(
    r"\b(?:i(?:'|\s)?ll|i will)\s+[a-zA-Z]", re.IGNORECASE
)
#: first-person ability promise: "I can get that done Friday"
_PROMISE_ABILITY = re.compile(
    r"\bi\s+(?:can|could)\s+(?:get|do|have|make|take|finish|complete|handle|"
    r"manage|send|call|review|check|confirm|submit|deliver|arrange|book|"
    r"schedule|pay|sign|email|text|return|refund|sort)\b",
    re.IGNORECASE,
)
#: explicit self-reminder: "Remind me next week."
_PROMISE_REMIND = re.compile(
    r"\b(?:remind me|keep me posted|let me not forget)\b", re.IGNORECASE
)
#: "I'll take care of it." / "I'll handle it."
_PROMISE_CARE = re.compile(
    r"\b(?:i(?:'|\s)?ll\s+(?:take care of|handle)|let me handle|let me sort)\b",
    re.IGNORECASE,
)
#: "get back to you" — a promise to follow up (example 6).
_PROMISE_CALLBACK = re.compile(
    r"\b(?:get|reach|come) back to (?:you|them|her|him)\b", re.IGNORECASE
)

#: (pattern, label, confidence) for commitment signals, in match-priority
#: order. The label becomes a ``why`` entry.
_COMMITMENT_SIGNALS = (
    (_PROMISE_FUTURE, "first-person promise", 0.90),
    (_PROMISE_ABILITY, "ability promise (can/could)", 0.85),
    (_PROMISE_REMIND, "explicit self-reminder ('remind me')", 0.85),
    (_PROMISE_CARE, "'take care of / handle' promise", 0.85),
    (_PROMISE_CALLBACK, "follow-up promise ('get back to you')", 0.80),
)


# ---------------------------------------------------------------------------
# request patterns (§17 — incoming asks of the user)
# ---------------------------------------------------------------------------
_REQ_ASK_YOU = re.compile(
    r"\b(?:can|could|would|will)\s+you\s+(?:please\s+)?[a-zA-Z]", re.IGNORECASE
)
_REQ_PLEASE = re.compile(
    r"\bplease\s+(?:call|send|review|confirm|check|verify|help|let|email|get|"
    r"make|do|handle|arrange|book|schedule|share|provide|give|return|refund|"
    r"reply|respond|sign|approve)\b",
    re.IGNORECASE,
)
_REQ_AVAILABLE = re.compile(
    r"\b(?:are|is|you're)\s+you\s+(?:available|free|around|okay|ok|open|able|"
    r"in|free to|around to)\b",
    re.IGNORECASE,
)
_REQ_NEED = re.compile(
    r"\bneed\s+(?:this|that|it|the|your|some|my)(?:\s+\w+)?\s+"
    r"(?:by|from|before|until|on|to)\b",
    re.IGNORECASE,
)
_REQ_CONFIRM = re.compile(
    r"\b(?:can|could|would|will)\s+you\s+(?:confirm|review|check|verify|approve|"
    r"send|call|email|reply|respond|let me know|get back to me)\b",
    re.IGNORECASE,
)

#: (pattern, label, confidence) for request signals.
_REQUEST_SIGNALS = (
    (_REQ_ASK_YOU, "direct question to the user", 0.90),
    (_REQ_PLEASE, "'please …' imperative", 0.85),
    (_REQ_AVAILABLE, "availability question", 0.90),
    (_REQ_CONFIRM, "confirmation/review request", 0.85),
    (_REQ_NEED, "need-by request", 0.85),
)


# ---------------------------------------------------------------------------
# deadline markers (§18)
# ---------------------------------------------------------------------------
#: A communication is a deadline when it carries one of these markers *and*
#: a parseable due date. (A first-person outgoing promise is excluded — the
#: due then belongs to the commitment, not a separate deadline.)
_DEADLINE_MARKERS = re.compile(
    r"\b(?:due|by|before|until|deadline|submit|turn in|turn in the|delivered|"
    r"deliver|expired|expiration|expiring|payment)\b",
    re.IGNORECASE,
)


def _match_signal(text: str, signals: Sequence[tuple[re.Pattern[str], str, float]]) -> tuple[re.Pattern[str], str, float, str] | None:
    """Return the first matching signal (pattern, label, conf, matched-text)."""
    for pattern, label, conf in signals:
        m = pattern.search(text)
        if m:
            return pattern, label, conf, m.group(0)
    return None


def _due_or_none(text: str, *, now: datetime) -> DueInfo | None:
    return parse_due_expression(text, now=now)


def _confidence(base: float, due: DueInfo | None) -> float:
    """Nudge confidence slightly up when a concrete due date is present."""
    if due is None:
        return round(base, 2)
    return round(min(1.0, base + 0.03), 2)


def detect_communication(
    communication: Communication,
    text: str,
    *,
    now: datetime | None = None,
) -> DetectionReport:
    """Run all three extractors over one normalized communication.

    Pure and deterministic: the same ``(communication, text, now)`` yields
    an identical :class:`DetectionReport`. ``now`` defaults to the current
    UTC time (callers that need reproducibility pass an explicit anchor).
    """
    now = now if now is not None else datetime.now(UTC)
    now = now.astimezone(UTC) if now.tzinfo else now.replace(tzinfo=UTC)

    comm_id = communication.communication_id
    content_ref = communication.content_ref
    low = " ".join((text or "").split())
    findings: list[Commitment | Request | Deadline] = []

    if low:
        due = _due_or_none(low, now=now)

        # -- commitments (§16): first-person promises the user made --------
        if communication.direction == CommunicationDirection.OUTGOING:
            sig = _match_signal(low, _COMMITMENT_SIGNALS)
            if sig is not None:
                _, label, base_conf, matched = sig
                why = [f"commitment: {label} — {_short(matched)}"]
                if due is not None:
                    why.append(f"due {due.expr}")
                else:
                    why.append("no explicit due date (open-ended)")
                findings.append(
                    Commitment(
                        finding_id=f"commitment-{comm_id}",
                        created_from=comm_id,
                        content_ref=content_ref,
                        status=FindingStatus.OPEN,
                        confidence=_confidence(base_conf, due),
                        model_used=DETERMINISTIC_DETECTOR,
                        why=why,
                        due=due.due if due else None,
                        due_info=due,
                        matched=_short(matched),
                    )
                )

        # -- requests (§17): incoming asks of the user --------------------
        if communication.direction == CommunicationDirection.INCOMING:
            sig = _match_signal(low, _REQUEST_SIGNALS)
            if sig is not None:
                _, label, base_conf, matched = sig
                why = [f"request: {label} — {_short(matched)}"]
                if due is not None:
                    why.append(f"asked with a due: {due.expr}")
                findings.append(
                    Request(
                        finding_id=f"request-{comm_id}",
                        created_from=comm_id,
                        content_ref=content_ref,
                        status=FindingStatus.OPEN,
                        confidence=_confidence(base_conf, None),
                        model_used=DETERMINISTIC_DETECTOR,
                        why=why,
                        matched=_short(matched),
                    )
                )

        # -- deadlines (§18): explicit due, not an outgoing promise -------
        # A first-person outgoing promise already carries its due on the
        # commitment, so it is not a separate deadline.
        already_commitment = (
            communication.direction == CommunicationDirection.OUTGOING
            and _match_signal(low, _COMMITMENT_SIGNALS) is not None
        )
        if due is not None and not already_commitment and _DEADLINE_MARKERS.search(low):
            why = [f"deadline: explicit due — {_short(low)}"]
            why.append(f"due {due.expr}")
            if due.time_stated:
                why.append("time of day explicitly stated")
            findings.append(
                Deadline(
                    finding_id=f"deadline-{comm_id}",
                    created_from=comm_id,
                    content_ref=content_ref,
                    status=FindingStatus.OPEN,
                    confidence=_confidence(0.88, due),
                    model_used=DETERMINISTIC_DETECTOR,
                    why=why,
                    due=due.due,
                    due_info=due,
                    matched=_short(due.expr),
                )
            )

    return DetectionReport(
        communication_id=comm_id,
        content_ref=content_ref,
        findings=tuple(findings),
        detected_at=now,
    )


__all__ = ["detect_communication"]
