"""Personal expectedness, SHADOW only (NS-ADR-075, spike 8.3 follow-on).

"Does this sender normally ask me for this?" Runtime port of the 8.3 model
(``research/expectedness.py`` is research code and is never imported here):

* **Inputs.** The semantic observer's request types (NS-ADR-073) and the
  sender's registrable domain, HMAC-keyed per account (label
  ``expectedness/v1`` under the ledger key). Counts only: no address,
  subject, body or message id.
* **Prior.** A FIXED, versioned population prior (:data:`POPULATION_PRIOR`),
  never the account's own mix. Spike 8.3 showed an account-derived prior is
  dominated by heavy senders (bills make PAY look normal for everyone).
* **Uncertainty.** Dirichlet posterior (prior strength 4) with a ~95%
  interval against a 5% cutoff: ``UNEXPECTED`` only when the whole interval is
  below it, ``EXPECTED`` only when it is above, else ``UNCERTAIN``. Fewer than
  5 prior observations of the sender -> ``ABSTAIN`` (sparse and new senders
  are never flagged for lacking history).
* **Advisory only.** No status means safe. The only output that reaches the
  review queue is an ``UNEXPECTED`` high-impact ask (PAY,
  CHANGE_PAYMENT_DETAILS, SUBMIT_CREDENTIALS, SHARE_PERSONAL_DATA).
  Dispositions, the gate and attention never change.

Not in this cut: feedback ("this was expected"), which needs a UI.
"""
from __future__ import annotations

import hashlib
import hmac
import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

POLICY_VERSION = "mail-expectedness@1"
PRIOR_STRENGTH = 4.0
MIN_HISTORY = 5
UNEXPECTED_BELOW = 0.05

HIGH_IMPACT = frozenset(
    {"PAY", "CHANGE_PAYMENT_DETAILS", "SUBMIT_CREDENTIALS", "SHARE_PERSONAL_DATA"}
)

#: Versioned population prior over observer request types (sums to 1). Owner-
#: reviewable data: a plain "what kinds of asks arrive in a mailbox" mix,
#: deliberately NOT learned from this account.
POPULATION_PRIOR: dict[str, float] = {
    "READ": 0.22,
    "OPEN_LINK": 0.24,
    "REPLY": 0.18,
    "SCHEDULE": 0.08,
    "DOWNLOAD": 0.07,
    "PAY": 0.08,
    "SUBMIT_CREDENTIALS": 0.05,
    "SHARE_PERSONAL_DATA": 0.04,
    "CHANGE_PAYMENT_DETAILS": 0.04,
}


class Status(StrEnum):
    ABSTAIN = "ABSTAIN"
    EXPECTED = "EXPECTED"
    UNCERTAIN = "UNCERTAIN"
    UNEXPECTED = "UNEXPECTED"


@dataclass(frozen=True)
class Score:
    request_type: str
    status: Status
    probability: float
    interval: tuple[float, float] | None
    history: int
    high_impact: bool

    def as_codes(self) -> dict[str, Any]:
        return {
            "request_type": self.request_type,
            "status": self.status.value,
            "history": self.history,
            "high_impact": self.high_impact,
            "probability": round(self.probability, 4),
        }


def sender_key(secret: bytes, account_scope: str, sender_domain: str) -> str:
    base = hmac.new(secret, f"expectedness/v1/{account_scope}".encode(), hashlib.sha256).digest()
    return hmac.new(base, sender_domain.strip().lower().encode(), hashlib.sha256).hexdigest()[:24]


#: "This was expected" feedback (NS-ADR-078): pseudo-counts per live record.
FEEDBACK_WEIGHT = 2
FEEDBACK_TTL_DAYS = 180


def score(
    request_type: str,
    counts: Mapping[str, float],
    feedback: Mapping[str, float] | None = None,
) -> Score:
    """Score one request type against the sender's counts BEFORE this message.

    ``feedback`` (live "expected" pseudo-counts per type) shifts the estimate
    but never counts as history: it cannot lift a sparse sender out of
    ABSTAIN, and it can never make anything "safe".
    """
    hi = request_type in HIGH_IMPACT
    prior = POPULATION_PRIOR.get(request_type, min(POPULATION_PRIOR.values()))
    history = int(sum(counts.values()))
    if history < MIN_HISTORY:
        return Score(request_type, Status.ABSTAIN, prior, None, history, hi)
    fb = dict(feedback or {})
    a = counts.get(request_type, 0.0) + fb.get(request_type, 0.0) + PRIOR_STRENGTH * prior
    n = history + sum(fb.values()) + PRIOR_STRENGTH
    p = a / n
    sd = math.sqrt(p * (1 - p) / (n + 1))
    interval = (max(0.0, p - 1.96 * sd), min(1.0, p + 1.96 * sd))
    if interval[1] < UNEXPECTED_BELOW:
        status = Status.UNEXPECTED
    elif interval[0] > UNEXPECTED_BELOW:
        status = Status.EXPECTED
    else:
        status = Status.UNCERTAIN
    return Score(request_type, status, p, interval, history, hi)


def unexpected_high_impact(scores: Iterable[Mapping[str, Any]]) -> bool:
    """True when any stored score is an UNEXPECTED high-impact ask."""
    return any(
        s.get("status") == Status.UNEXPECTED.value and bool(s.get("high_impact")) for s in scores
    )


__all__ = [
    "HIGH_IMPACT",
    "MIN_HISTORY",
    "POLICY_VERSION",
    "POPULATION_PRIOR",
    "Score",
    "Status",
    "score",
    "sender_key",
    "unexpected_high_impact",
]
