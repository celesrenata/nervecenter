"""Synthetic evaluation for spike 8.3 (no real mail).

Accounts have senders in relationship classes, each with its own request
distribution. History is drawn from that distribution (with sparse senders);
test events are either *normal* (drawn from the relationship) or *anomalous*
(a high-impact type implausible for that relationship). We compare the
personalized surprise with the unpersonalized global-prior surprise.
"""

from __future__ import annotations

import json
import random
from collections import Counter
from datetime import UTC, datetime

from backend.mail_security.research.expectedness import (
    HIGH_IMPACT,
    REQUEST_TYPES,
    ExpectednessModel,
    Status,
    global_surprise,
)

NOW = datetime(2026, 11, 12, tzinfo=UTC)
REL = {
    "biller": {"PAY": 0.6, "READ": 0.3, "OPEN_LINK": 0.1},
    "vendor": {"PAY": 0.4, "REPLY": 0.3, "READ": 0.25, "DOWNLOAD": 0.05},
    "employer": {"SCHEDULE": 0.4, "REPLY": 0.4, "DOWNLOAD": 0.1, "READ": 0.1},
    "friend": {"REPLY": 0.7, "SCHEDULE": 0.2, "OPEN_LINK": 0.1},
    "bank": {"READ": 0.8, "OPEN_LINK": 0.15, "PAY": 0.05},
    "saas": {"READ": 0.5, "OPEN_LINK": 0.3, "SUBMIT_CREDENTIALS": 0.2},  # login flows
}
ANOMALY = {
    "biller": ["SUBMIT_CREDENTIALS", "CHANGE_PAYMENT_DETAILS"],
    "vendor": ["CHANGE_PAYMENT_DETAILS", "SUBMIT_CREDENTIALS"],
    "employer": ["SUBMIT_CREDENTIALS", "PAY", "SHARE_PERSONAL_DATA"],
    "friend": ["PAY", "SUBMIT_CREDENTIALS", "SHARE_PERSONAL_DATA"],
    "bank": ["SUBMIT_CREDENTIALS", "CHANGE_PAYMENT_DETAILS", "SHARE_PERSONAL_DATA"],
    "saas": ["PAY", "CHANGE_PAYMENT_DETAILS"],
}


def _draw(rng: random.Random, dist: dict[str, float]) -> str:
    r, acc = rng.random(), 0.0
    for t, p in dist.items():
        acc += p
        if r <= acc:
            return t
    return next(iter(dist))


def _auc(pos: list[float], neg: list[float]) -> float:
    if not pos or not neg:
        return float("nan")
    wins = sum((p > n) + 0.5 * (p == n) for p in pos for n in neg)
    return wins / (len(pos) * len(neg))


def evaluate(seed: int = 11, accounts: int = 40, senders: int = 24) -> dict:
    rng = random.Random(seed)
    rows = []  # (label, personal_surprise, global_surprise, status, sparse, high_impact)
    for _ in range(accounts):
        model = ExpectednessModel(salt=rng.randbytes(16), opted_in=True)
        people = []
        for s in range(senders):
            rel = rng.choice(list(REL))
            sparse = rng.random() < 0.3
            n = rng.randint(1, 4) if sparse else rng.randint(6, 40)
            dom = f"s{s}-{rel}.example"
            for _ in range(n):
                model.observe(dom, _draw(rng, REL[rel]))
            people.append((dom, rel, sparse))
        prior = model._prior()
        for dom, rel, sparse in people:
            for label in (0, 1):
                t = rng.choice(ANOMALY[rel]) if label else _draw(rng, REL[rel])
                e = model.score(dom, t, now=NOW)
                rows.append(
                    (
                        label,
                        e.surprise,
                        global_surprise(prior, t),
                        e.status.value,
                        sparse,
                        t in HIGH_IMPACT,
                    )
                )

    def auc(idx, subset=lambda r: True):
        pos = [r[idx] for r in rows if r[0] == 1 and subset(r)]
        neg = [r[idx] for r in rows if r[0] == 0 and subset(r)]
        return round(_auc(pos, neg), 4)

    evaluated = lambda r: r[3] != Status.ABSTAIN.value  # noqa: E731
    hi_only = lambda r: r[5]  # noqa: E731

    def rate(pred, subset):
        sel = [r for r in rows if subset(r)]
        return round(sum(1 for r in sel if pred(r)) / len(sel), 4) if sel else None

    flagged = lambda r: r[3] == Status.UNEXPECTED.value  # noqa: E731
    out = {
        "seed": seed,
        "accounts": accounts,
        "senders_per_account": senders,
        "events": len(rows),
        "auc_all": {"personalized": auc(1), "global_baseline": auc(2)},
        "auc_evaluated_only": {
            "personalized": auc(1, evaluated),
            "global_baseline": auc(2, evaluated),
        },
        "auc_high_impact_types_only": {
            "personalized": auc(1, hi_only),
            "global_baseline": auc(2, hi_only),
        },
        "abstain_rate": {
            "sparse_senders": rate(lambda r: r[3] == "ABSTAIN", lambda r: r[4]),
            "rich_senders": rate(lambda r: r[3] == "ABSTAIN", lambda r: not r[4]),
        },
        "false_unexpected_rate_on_normal": {
            "sparse_senders": rate(flagged, lambda r: r[0] == 0 and r[4]),
            "rich_senders": rate(flagged, lambda r: r[0] == 0 and not r[4]),
        },
        "unexpected_recall_on_anomalous": {
            "sparse_senders": rate(flagged, lambda r: r[0] == 1 and r[4]),
            "rich_senders": rate(flagged, lambda r: r[0] == 1 and not r[4]),
        },
        "status_counts": dict(Counter(r[3] for r in rows)),
        "request_types": list(REQUEST_TYPES),
    }
    return out


if __name__ == "__main__":  # pragma: no cover
    print(json.dumps(evaluate(), indent=1))
