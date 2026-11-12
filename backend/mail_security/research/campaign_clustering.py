"""Spike 8.1: threat-campaign clustering from privacy-safe fingerprints (offline research).

Question: can campaigns (many near-identical malicious messages) be grouped
*without content* — no words, no addresses — so clusters could one day be
shared as aggregate threat intelligence, while staying time-limited?

Design (R-006, R-015, R-024):

* **Structural tokens only.** Sender registrable domain and link-host
  registrables (as keyed hashes), the *shape* of URL paths and the subject
  (character classes and length buckets, never words), attachment
  extensions, MIME layout, which of a fixed set of headers are present, and
  count buckets. No body word, local part, display name or full URL.
* **Keyed per epoch.** Every token is an HMAC under an epoch key derived from
  a secret and ``floor(t / epoch)``. Signatures from different epochs share
  nothing, so stored fingerprints cannot be linked across epochs or reversed
  by dictionary (the shapes themselves are low-entropy, the key is not).
* **Time-limited store.** Clusters keep only a signature, a size, first/last
  seen and an expiry; :meth:`CampaignStore.expire` drops them after the TTL.
  Aggregates are released only for clusters with at least ``k`` members.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from urllib.parse import urlsplit

from backend.mail_security._domains import domain_of
from backend.mail_security.inspections import _registrable

NUM_HASHES = 64
BANDS = 16
ROWS = NUM_HASHES // BANDS
#: Headers whose *presence* (not value) is a structural feature.
PRESENCE_HEADERS = (
    "reply-to",
    "x-mailer",
    "list-unsubscribe",
    "list-id",
    "precedence",
    "x-priority",
    "mime-version",
    "content-transfer-encoding",
    "x-originating-ip",
    "user-agent",
)


def epoch_key(secret: bytes, when: datetime, *, epoch: timedelta = timedelta(days=7)) -> bytes:
    index = int(when.timestamp() // epoch.total_seconds())
    return hmac.new(secret, f"campaign-epoch:{index}".encode(), hashlib.sha256).digest()


def _bucket(n: int) -> str:
    return "0" if n <= 0 else "1" if n == 1 else "2-3" if n <= 3 else "4-9" if n <= 9 else "10+"


def shape(text: str, limit: int = 24) -> str:
    """Character-class shape with collapsed runs: ``Inv-2024!`` -> ``Aa-9!``."""
    out: list[str] = []
    for ch in text[:200]:
        c = (
            "9"
            if ch.isdigit()
            else "A"
            if ch.isupper()
            else "a"
            if ch.isalpha()
            else (ch if ch in "-_./!$%?#&=:" else " " if ch.isspace() else "*")
        )
        if not out or out[-1] != c:
            out.append(c)
    return "".join(out)[:limit]


def _subject_shape(subject: str) -> list[str]:
    letters = [c for c in subject if c.isalpha()]
    caps = sum(c.isupper() for c in letters) / len(letters) if letters else 0.0
    return [
        f"sl:{_bucket(len(subject) // 10)}",
        f"sc:{'hi' if caps > 0.6 else 'mid' if caps > 0.2 else 'lo'}",
        f"sd:{any(c.isdigit() for c in subject)}",
        f"sx:{'!' in subject}{'$' in subject}",
        f"ss:{shape(subject, 12)}",
    ]


def structural_tokens(evidence: object) -> set[str]:
    """Plain (pre-keying) structural tokens of one parsed message."""
    headers = list(getattr(evidence, "headers", []) or [])
    names = {k.lower() for k, _ in headers}
    get = {k.lower(): v for k, v in headers}
    toks: set[str] = set()
    sender = _registrable(domain_of(get.get("from")) or "")
    if sender:
        toks.add(f"sd:{sender}")
    # Presence only for headers that ARE present: always-on "absent" tokens are
    # shared by almost every message and inflate Jaccard similarity (v1 finding).
    toks.update(f"h:{name}" for name in PRESENCE_HEADERS if name in names)
    toks.update(_subject_shape(get.get("subject") or ""))
    links = list(getattr(evidence, "links", []) or [])
    toks.add(f"lc:{_bucket(len(links))}")
    hosts = set()
    for link in links[:50]:
        try:
            parts = urlsplit(getattr(link, "target", "") or "")
        except ValueError:
            continue
        if parts.hostname:
            hosts.add(_registrable(parts.hostname))
            toks.add(f"lp:{shape(parts.path or '/', 16)}")
        if parts.scheme:
            toks.add(f"ls:{parts.scheme}")
    toks.update(f"lh:{h}" for h in sorted(hosts)[:20])
    toks.add(f"lhc:{_bucket(len(hosts))}")
    if sender and sender in hosts:
        toks.add("lself")
    atts = list(getattr(evidence, "attachments", []) or [])
    toks.add(f"ac:{_bucket(len(atts))}")
    for att in atts[:10]:
        name = getattr(att, "filename", "") or ""
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else "none"
        toks.add(f"ae:{ext if re.fullmatch(r'[a-z0-9]{1,5}', ext) else 'other'}")
    images = getattr(evidence, "images", []) or []
    remote = sum(1 for i in images if getattr(i, "is_remote", False))
    toks.add(f"ri:{_bucket(remote)}")
    subtypes = sorted(
        {getattr(p, "subtype", "") for p in getattr(evidence, "text_parts", []) or []}
    )
    toks.add(f"tp:{'/'.join(subtypes) or 'none'}")
    toks.add(f"mp:{_bucket(int(getattr(evidence, 'part_count', 0) or 0))}")
    toks.add(f"md:{min(int(getattr(evidence, 'max_depth_seen', 0) or 0), 5)}")
    return toks


def keyed(tokens: Iterable[str], key: bytes) -> frozenset[str]:
    return frozenset(hmac.new(key, t.encode(), hashlib.sha256).hexdigest()[:16] for t in tokens)


def minhash(keyed_tokens: frozenset[str], key: bytes) -> tuple[int, ...]:
    if not keyed_tokens:
        return ()
    sig = []
    for i in range(NUM_HASHES):
        seed = key + i.to_bytes(2, "big")
        sig.append(
            min(
                int.from_bytes(
                    hashlib.blake2b(t.encode(), key=seed[:64], digest_size=8).digest(), "big"
                )
                for t in keyed_tokens
            )
        )
    return tuple(sig)


def similarity(a: tuple[int, ...], b: tuple[int, ...]) -> float:
    if not a or not b:
        return 0.0
    return sum(x == y for x, y in zip(a, b, strict=True)) / len(a)


def cluster(signatures: Mapping[str, tuple[int, ...]], threshold: float = 0.7) -> dict[str, str]:
    """LSH banding + union-find. Returns id -> cluster root id."""
    ids = sorted(signatures)
    parent = {i: i for i in ids}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    buckets: dict[tuple[int, tuple[int, ...]], list[str]] = {}
    for i in ids:
        sig = signatures[i]
        if not sig:
            continue
        for band in range(BANDS):
            buckets.setdefault((band, sig[band * ROWS : (band + 1) * ROWS]), []).append(i)
    for members in buckets.values():
        members = members[:256]
        for x, a in enumerate(members):
            for b in members[x + 1 :]:
                ra, rb = find(a), find(b)
                if ra != rb and similarity(signatures[a], signatures[b]) >= threshold:
                    parent[max(ra, rb)] = min(ra, rb)
    return {i: find(i) for i in ids}


@dataclass
class Cluster:
    signature: tuple[int, ...]
    size: int
    first_seen: datetime
    last_seen: datetime
    expires_at: datetime


@dataclass
class CampaignStore:
    """Time-limited cluster summaries (no message ids, no tokens)."""

    ttl: timedelta = timedelta(days=30)
    k: int = 3
    clusters: dict[str, Cluster] = field(default_factory=dict)

    def observe(self, cluster_id: str, signature: tuple[int, ...], when: datetime) -> None:
        c = self.clusters.get(cluster_id)
        if c is None:
            self.clusters[cluster_id] = Cluster(signature, 1, when, when, when + self.ttl)
            return
        c.size += 1
        c.last_seen = max(c.last_seen, when)
        c.first_seen = min(c.first_seen, when)
        c.expires_at = c.last_seen + self.ttl

    def expire(self, now: datetime) -> int:
        dead = [cid for cid, c in self.clusters.items() if c.expires_at <= now]
        for cid in dead:
            del self.clusters[cid]
        return len(dead)

    def aggregates(self) -> list[dict[str, object]]:
        """Releasable summaries: only clusters with at least ``k`` members."""
        return [
            {
                "size": c.size,
                "first_seen": c.first_seen.isoformat(),
                "last_seen": c.last_seen.isoformat(),
            }
            for c in sorted(self.clusters.values(), key=lambda c: -c.size)
            if c.size >= self.k
        ]


__all__ = [
    "BANDS",
    "NUM_HASHES",
    "PRESENCE_HEADERS",
    "CampaignStore",
    "cluster",
    "epoch_key",
    "keyed",
    "minhash",
    "shape",
    "similarity",
    "structural_tokens",
]
