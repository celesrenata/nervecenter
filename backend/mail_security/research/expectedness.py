"""Spike 8.3: personal expectedness engine (offline research).

Question: does a per-account model of "what does this sender normally ask
me for?" separate anomalous high-impact asks (a friend asking for money, an
employer asking for a password) better than an unpersonalized prior — and
can it do that with opt-in, uncertainty, fair treatment of sparse senders and
reversible feedback?

Design (R-013, R-015, R-024):

* **Opt-in per account.** A model that is not opted in returns
  ``NOT_OPTED_IN`` and records nothing.
* **Keyed senders, counts only.** Senders are HMAC(account salt, registrable
  domain); the model stores request-type counts per key. No address, subject
  or body. ``forget()`` deletes everything.
* **Bayesian with uncertainty.** P(type | sender) is a Dirichlet posterior
  with the account's (or global) type distribution as prior
  (``prior_strength`` pseudo-counts). Fewer than ``min_history`` observations
  -> ``ABSTAIN`` (the unpersonalized prior is reported instead), so sparse /
  new senders are not punished for having no history.
* **Feedback is reversible and bounded.** "This was expected" adds a capped
  pseudo-count recorded under a feedback id, expires, and ``undo()`` restores
  the exact previous state. Feedback can never make a high-impact type
  *trusted*: the result is advisory and never overrides policy (e.g. a
  credential ask still requires review under 4.x policy).
"""

from __future__ import annotations

import hashlib
import hmac
import math
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum

REQUEST_TYPES = (
    "READ",
    "REPLY",
    "SCHEDULE",
    "PAY",
    "CHANGE_PAYMENT_DETAILS",
    "SUBMIT_CREDENTIALS",
    "SHARE_PERSONAL_DATA",
    "DOWNLOAD",
    "OPEN_LINK",
)
HIGH_IMPACT = frozenset(
    {"PAY", "CHANGE_PAYMENT_DETAILS", "SUBMIT_CREDENTIALS", "SHARE_PERSONAL_DATA"}
)
FEEDBACK_WEIGHT = 2.0
FEEDBACK_TTL = timedelta(days=180)


class Status(StrEnum):
    NOT_OPTED_IN = "NOT_OPTED_IN"
    ABSTAIN = "ABSTAIN"
    EXPECTED = "EXPECTED"
    #: The credible interval straddles the cutoff: not enough evidence either way.
    UNCERTAIN = "UNCERTAIN"
    UNEXPECTED = "UNEXPECTED"


@dataclass(frozen=True)
class Expectedness:
    status: Status
    probability: float | None  # P(type | sender), or the prior when abstaining
    interval: tuple[float, float] | None  # ~95% credible interval
    history: int
    surprise: float | None  # -log2 probability (higher = more anomalous)
    high_impact: bool


@dataclass
class _Feedback:
    key: str
    request_type: str
    weight: float
    expires_at: datetime


@dataclass
class ExpectednessModel:
    salt: bytes
    opted_in: bool = False
    prior: Mapping[str, float] | None = None
    prior_strength: float = 4.0
    min_history: int = 5
    unexpected_below: float = 0.05
    _counts: dict[str, dict[str, float]] = field(default_factory=lambda: defaultdict(dict))
    _feedback: dict[str, _Feedback] = field(default_factory=dict)

    def _key(self, sender_domain: str) -> str:
        return hmac.new(
            self.salt, sender_domain.strip().lower().encode(), hashlib.sha256
        ).hexdigest()[:24]

    def _prior(self) -> dict[str, float]:
        base = dict(self.prior or {})
        if not base:
            totals: dict[str, float] = defaultdict(float)
            for c in self._counts.values():
                for t, n in c.items():
                    totals[t] += n
            base = dict(totals)
        smoothed = {t: base.get(t, 0.0) + 0.5 for t in REQUEST_TYPES}
        z = sum(smoothed.values())
        return {t: v / z for t, v in smoothed.items()}

    def observe(self, sender_domain: str, request_type: str) -> None:
        if not self.opted_in or request_type not in REQUEST_TYPES:
            return
        c = self._counts[self._key(sender_domain)]
        c[request_type] = c.get(request_type, 0.0) + 1.0

    def feedback_expected(
        self, feedback_id: str, sender_domain: str, request_type: str, *, now: datetime
    ) -> None:
        if not self.opted_in or feedback_id in self._feedback or request_type not in REQUEST_TYPES:
            return
        key = self._key(sender_domain)
        self._feedback[feedback_id] = _Feedback(
            key, request_type, FEEDBACK_WEIGHT, now + FEEDBACK_TTL
        )

    def undo(self, feedback_id: str) -> bool:
        return self._feedback.pop(feedback_id, None) is not None

    def expire(self, now: datetime) -> int:
        dead = [f for f, fb in self._feedback.items() if fb.expires_at <= now]
        for f in dead:
            del self._feedback[f]
        return len(dead)

    def forget(self) -> None:
        self._counts.clear()
        self._feedback.clear()

    def state(self) -> tuple:
        """Comparable snapshot (counts + live feedback) for reversibility tests."""
        counts = tuple(sorted((k, tuple(sorted(v.items()))) for k, v in self._counts.items()))
        fb = tuple(sorted((f, v.key, v.request_type, v.weight) for f, v in self._feedback.items()))
        return counts, fb

    def score(self, sender_domain: str, request_type: str, *, now: datetime) -> Expectedness:
        hi = request_type in HIGH_IMPACT
        if not self.opted_in:
            return Expectedness(Status.NOT_OPTED_IN, None, None, 0, None, hi)
        key = self._key(sender_domain)
        counts = dict(self._counts.get(key, {}))
        for fb in self._feedback.values():
            if fb.key == key and fb.expires_at > now:
                counts[fb.request_type] = counts.get(fb.request_type, 0.0) + fb.weight
        observed = int(sum(v for v in self._counts.get(key, {}).values()))
        prior = self._prior()
        if observed < self.min_history:
            p = prior[request_type]
            return Expectedness(Status.ABSTAIN, p, None, observed, -math.log2(p), hi)
        total = sum(counts.values())
        alpha = self.prior_strength * prior[request_type]
        a = counts.get(request_type, 0.0) + alpha
        n = total + self.prior_strength
        p = a / n
        sd = math.sqrt(p * (1 - p) / (n + 1))
        interval = (max(0.0, p - 1.96 * sd), min(1.0, p + 1.96 * sd))
        if interval[1] < self.unexpected_below:
            status = Status.UNEXPECTED
        elif interval[0] > self.unexpected_below:
            status = Status.EXPECTED
        else:
            status = Status.UNCERTAIN
        return Expectedness(status, p, interval, observed, -math.log2(p), hi)


def global_surprise(prior: Mapping[str, float], request_type: str) -> float:
    """Unpersonalized baseline: surprise of the type under the global prior."""
    return -math.log2(prior[request_type])


__all__ = [
    "FEEDBACK_TTL",
    "FEEDBACK_WEIGHT",
    "HIGH_IMPACT",
    "REQUEST_TYPES",
    "Expectedness",
    "ExpectednessModel",
    "Status",
    "global_surprise",
]
