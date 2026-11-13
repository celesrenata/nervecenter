"""Per-account content campaigns, SHADOW only (NS-ADR-069, G8 item 6).

Spike 8.1 recommended per-account content campaigns kept inside the account
boundary; structural ("same kit") fingerprints were too low-recall to be worth
shipping, and cross-account sharing is NOT approved. So:

* **Signature.** The 6.2 keyed word-3-shingle MinHash
  (:func:`backend.mail_security.split_keys.minhash`, 64 hashes) over the
  message's normalized text, under a per-account key. LSH uses 16 bands x 4
  rows; each band is stored only as an HMAC ``band_key``. Two messages are in
  the same campaign when a band collides AND the MinHash Jaccard estimate is
  at least :data:`THRESHOLD` (0.6, the 6.2 setting).
* **Keys.** Derived from ``NERVE_MAIL_SECURITY_LEDGER_KEY`` with a separate
  label (``campaigns/v1``), so signatures never correlate across accounts and
  never with ledger fingerprints. Without the key the feature is inert.
* **Retention.** Members expire after :data:`TTL_DAYS` (30). A signature is
  derived from content, so it stays inside the account and is never released.
* **Campaign ids are assigned on insert** (the first matching member's
  campaign, else a new one) and never merged afterwards. That is a deliberate
  simplification; it can split a campaign whose early members did not match
  each other, never join unrelated ones.
* **SHADOW only.** The only output is a review-queue reason,
  ``CAMPAIGN_OF_FLAGGED``: a message whose campaign contains ANOTHER message
  assessed REVIEW or RESTRICTED. Dispositions, gate decisions and attention
  are never changed.
"""
from __future__ import annotations

import hashlib
import hmac
from collections.abc import Iterable
from typing import Any

from backend.mail_security.split_keys import BANDS, NUM_HASHES, ROWS, minhash
from backend.mail_security.thread_ledger import ledger_key_from_env

POLICY_VERSION = "mail-campaigns@1"
THRESHOLD = 0.6
TTL_DAYS = 30
#: Statuses of a sibling that make the rest of its campaign worth a look.
FLAGGED_DISPOSITIONS = frozenset({"REVIEW", "RESTRICTED"})
#: Minimum distinct shingles: very short texts (one-liners) collide too easily.
MIN_TEXT_WORDS = 12
#: Upper bound on candidate members compared per insert.
MAX_CANDIDATES = 256


def _account_keys(secret: bytes, account_scope: str) -> tuple[bytes, bytes]:
    base = hmac.new(secret, f"campaigns/v1/{account_scope}".encode(), hashlib.sha256).digest()
    return (
        hashlib.sha256(base + b"/minhash").digest()[:32],
        hashlib.sha256(base + b"/band").digest(),
    )


def campaign_secret_from_env() -> bytes | None:
    return ledger_key_from_env()


def text_of(evidence: Any) -> str:
    """Normalized text of a parsed message: plain parts, else html parts."""
    parts = list(getattr(evidence, "text_parts", None) or [])
    plain = [p.normalized for p in parts if getattr(p, "subtype", "") == "plain"]
    chosen = plain or [p.normalized for p in parts]
    return "\n".join(t for t in chosen if t)


def signature(secret: bytes, account_scope: str, text: str) -> tuple[int, ...]:
    """Keyed MinHash, or ``()`` for texts too short to be meaningful."""
    if len(text.split()) < MIN_TEXT_WORDS:
        return ()
    mh_key, _ = _account_keys(secret, account_scope)
    return minhash(text, mh_key)


def band_keys(secret: bytes, account_scope: str, sig: tuple[int, ...]) -> list[str]:
    if len(sig) != NUM_HASHES:
        return []
    _, band_key = _account_keys(secret, account_scope)
    out = []
    for band in range(BANDS):
        rows = sig[band * ROWS : (band + 1) * ROWS]
        material = f"{band}:" + ",".join(str(v) for v in rows)
        out.append(hmac.new(band_key, material.encode(), hashlib.sha256).hexdigest()[:32])
    return out


def similarity(a: Iterable[int], b: Iterable[int]) -> float:
    a, b = tuple(a), tuple(b)
    if len(a) != NUM_HASHES or len(b) != NUM_HASHES:
        return 0.0
    return sum(x == y for x, y in zip(a, b, strict=True)) / NUM_HASHES


def siblings_of_flagged(
    members: Iterable[tuple[str, str]], flagged_sources: set[str]
) -> dict[str, int]:
    """``source_key -> number of OTHER flagged members`` in its campaign.

    ``members`` is ``(source_key, campaign_id)``. A flagged message never
    counts itself, so one flagged message alone flags only its siblings.
    """
    by_campaign: dict[str, set[str]] = {}
    for source, campaign in members:
        by_campaign.setdefault(campaign, set()).add(source)
    out: dict[str, int] = {}
    for sources in by_campaign.values():
        flagged = sources & flagged_sources
        if not flagged:
            continue
        for s in sources:
            n = len(flagged - {s})
            if n:
                out[s] = n
    return out


_campaign_repository: Any = None


def set_campaign_repository(repository: Any) -> None:
    global _campaign_repository
    _campaign_repository = repository


def get_campaign_repository() -> Any:
    return _campaign_repository


__all__ = [
    "FLAGGED_DISPOSITIONS",
    "POLICY_VERSION",
    "THRESHOLD",
    "TTL_DAYS",
    "band_keys",
    "campaign_secret_from_env",
    "get_campaign_repository",
    "set_campaign_repository",
    "siblings_of_flagged",
    "signature",
    "similarity",
    "text_of",
]
