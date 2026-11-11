"""Thread and campaign keys for leakage-safe splits (closes 6.2 gaps).

Library only. Produces the opaque ``thread_key`` and ``campaign_key`` that
:class:`~backend.mail_security.dataset_manifest.ExampleMeta` needs.

* **Thread key.** The thread root is the first id in ``References``, else
  ``In-Reply-To``, else the message's own ``Message-ID`` (RFC 5322 order). It
  is HMAC-SHA256'd under a per-account key, so ids never appear and threads
  never join across accounts. Broken reference chains split a thread into two
  roots; the campaign clusterer usually reunites them by content.
* **Campaign key.** Near-duplicate content clusters: word 3-shingle MinHash
  (64 hashes, keyed blake2b) with LSH banding (16 bands x 4 rows); candidate
  pairs are linked only when the MinHash Jaccard estimate reaches the
  threshold. Clusters are found with union-find and named by an HMAC of their
  smallest member id. Deterministic: same inputs, same keys, any order.

Inputs should be the pseudonymised text from 6.1, so raw PII never enters.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Iterable, Mapping

from backend.mail_security.trust_index import message_ids

NUM_HASHES = 64
BANDS = 16
ROWS = NUM_HASHES // BANDS
SHINGLE = 3  # 3-word shingles tolerate per-recipient token edits better than 5
_MAX_WORDS = 4000
#: Pairwise comparisons per LSH bucket are capped. Members past the cap still
#: join through other bands; an all-identical blast is linked via the head pairs.
MAX_BUCKET = 256


def _account_key(secret: bytes, account_scope: str) -> bytes:
    if not isinstance(secret, bytes) or len(secret) < 32:
        raise ValueError("secret must be at least 32 bytes")
    return hmac.new(secret, b"split-keys/v1/" + account_scope.encode(), hashlib.sha256).digest()


def thread_root(
    message_id: str | None, in_reply_to: str | None = None, references: str | None = None
) -> str | None:
    """Raw thread root id (lower-cased), or None when no id is usable."""
    for header in (references, in_reply_to, message_id):
        ids = message_ids(header)
        if ids:
            return ids[0]
    return None


def thread_key(
    secret: bytes,
    account_scope: str,
    *,
    message_id: str | None,
    in_reply_to: str | None = None,
    references: str | None = None,
    fallback: str,
) -> str:
    """Opaque per-account thread key. ``fallback`` (e.g. the message sha256) is used
    when no Message-ID exists, so such a message forms its own thread."""
    root = thread_root(message_id, in_reply_to, references)
    material = f"mid\x1f{root}" if root else f"fallback\x1f{fallback}"
    digest = hmac.new(_account_key(secret, account_scope), material.encode(), hashlib.sha256)
    return "t:" + digest.hexdigest()[:32]


def _shingles(text: str) -> set[str]:
    words = re.sub(r"[^\w<>]+", " ", text.lower()).split()[:_MAX_WORDS]
    if len(words) < SHINGLE:
        return {" ".join(words)} if words else set()
    return {" ".join(words[i : i + SHINGLE]) for i in range(len(words) - SHINGLE + 1)}


_HASHES_PER_CALL = 8  # one 64-byte blake2b digest = 8 x 64-bit hash values


def minhash(text: str, key: bytes) -> tuple[int, ...]:
    shingles = _shingles(text)
    if not shingles:
        return ()
    sig = [2**64] * NUM_HASHES
    for s in shingles:
        data = s.encode()
        for call in range(NUM_HASHES // _HASHES_PER_CALL):
            digest = hashlib.blake2b(
                data, digest_size=64, key=key, salt=call.to_bytes(16, "big")
            ).digest()
            for j in range(_HASHES_PER_CALL):
                k = call * _HASHES_PER_CALL + j
                value = int.from_bytes(digest[j * 8 : (j + 1) * 8], "big")
                if value < sig[k]:
                    sig[k] = value
    return tuple(sig)


def _estimate(a: tuple[int, ...], b: tuple[int, ...]) -> float:
    return sum(x == y for x, y in zip(a, b, strict=True)) / NUM_HASHES


def campaign_keys(
    secret: bytes,
    account_scope: str,
    texts: Mapping[str, str],
    *,
    threshold: float = 0.6,
) -> dict[str, str]:
    """Cluster ``{example_id: pseudonymised text}`` into campaigns. Returns example -> key."""
    if not 0 < threshold <= 1:
        raise ValueError("threshold must be in (0, 1]")
    key = _account_key(secret, account_scope)
    mh_key = hashlib.sha256(key + b"/minhash").digest()[:32]
    ids = sorted(texts)
    sigs = {eid: minhash(texts[eid], mh_key) for eid in ids}
    parent = {eid: eid for eid in ids}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    buckets: dict[tuple[int, tuple[int, ...]], list[str]] = {}
    for eid in ids:
        sig = sigs[eid]
        if not sig:
            continue
        for band in range(BANDS):
            buckets.setdefault((band, sig[band * ROWS : (band + 1) * ROWS]), []).append(eid)
    for members in buckets.values():
        members = members[:MAX_BUCKET]  # bounds the worst case (e.g. a huge identical blast)
        for i, a in enumerate(members):
            for b in members[i + 1 :]:
                if find(a) != find(b) and _estimate(sigs[a], sigs[b]) >= threshold:
                    ra, rb = find(a), find(b)
                    parent[max(ra, rb)] = min(ra, rb)
    out: dict[str, str] = {}
    for eid in ids:
        root = find(eid)
        out[eid] = (
            "c:" + hmac.new(key, f"campaign\x1f{root}".encode(), hashlib.sha256).hexdigest()[:32]
        )
    return out


def campaign_clusters(keys: Iterable[str]) -> int:
    return len(set(keys))


__all__ = ["campaign_clusters", "campaign_keys", "minhash", "thread_key", "thread_root"]
