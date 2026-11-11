"""Signed, versioned dataset manifest with leakage-safe splits (NerveShield task 6.2).

Library only; trains nothing. Takes the 6.1 selection plus per-example split
metadata and produces an immutable manifest (R-016, R-021):

* **Leakage-safe split.** Examples are linked into groups by thread key and
  campaign key (union-find). Eval is every group whose *earliest* member is at
  or after ``cutoff``; train is every group whose *latest* member is before
  ``cutoff - embargo``. Groups that straddle the cutoff or fall in the embargo
  window are dropped. So no thread or campaign appears in both splits and no
  train example is later than any eval example.
* **Reproducible.** No randomness; input order does not matter; the manifest is
  canonical JSON (sorted keys, fixed separators) and its digest is stable.
* **Signed and immutable.** The canonical body is signed with Ed25519. Any
  change to an entry, a count, the rule or the review breaks verification.
* **Provenance.** Each entry records its label source (human feedback
  proposal), consent reference and a digest of the pseudonymised text, never
  the text. Provider labels may be attached as weak, explicitly non-label
  hints. A manifest is signed only with a recorded human review.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from backend.mail_security.signing_keys import KeyCustodyError, key_id, resolve_trusted
from backend.mail_security.ssh_signing import (
    ManifestSigner,
    SshPublicKey,
    SshSignatureError,
    verify_sshsig,
)
from backend.mail_security.training_selection import SelectedExample, SelectionManifest

MANIFEST_VERSION = "mail-dataset-manifest@1"
SPLIT_RULE = "temporal-group-cutoff@1"
_REF_RE = re.compile(r"^[A-Za-z0-9:_.-]{1,200}$")


class Split(StrEnum):
    TRAIN = "TRAIN"
    EVAL = "EVAL"


class DropReason(StrEnum):
    STRADDLES_CUTOFF = "STRADDLES_CUTOFF"
    IN_EMBARGO = "IN_EMBARGO"
    MISSING_METADATA = "MISSING_METADATA"


class ManifestError(ValueError):
    pass


@dataclass(frozen=True)
class ExampleMeta:
    """Split metadata for one example. Keys are opaque digests, never raw ids."""

    received_at: datetime
    thread_key: str
    campaign_key: str


@dataclass(frozen=True)
class HumanReview:
    reviewer_ref: str
    reviewed_at: datetime
    decision: str = "APPROVED"


@dataclass(frozen=True)
class SignedManifest:
    body: dict[str, Any]
    digest: str
    signature: str
    public_key: str

    def to_json(self) -> str:
        return json.dumps(
            {
                "body": self.body,
                "digest": self.digest,
                "signature": self.signature,
                "public_key": self.public_key,
            },
            sort_keys=True,
            separators=(",", ":"),
        )


def _canonical(body: Mapping[str, Any]) -> bytes:
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        raise ManifestError("timestamps must be timezone-aware")
    return value.isoformat()


class _Groups:
    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def find(self, x: str) -> str:
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[max(ra, rb)] = min(ra, rb)


def assign_splits(
    examples: list[SelectedExample],
    meta: Mapping[str, ExampleMeta],
    *,
    cutoff: datetime,
    embargo: timedelta = timedelta(days=7),
) -> tuple[dict[str, Split], dict[str, DropReason]]:
    """Pure, deterministic split. Returns (assigned, dropped) keyed by example id."""
    if cutoff.tzinfo is None:
        raise ManifestError("cutoff must be timezone-aware")
    if embargo < timedelta(0):
        raise ManifestError("embargo must not be negative")
    dropped: dict[str, DropReason] = {}
    usable: list[tuple[str, ExampleMeta]] = []
    for ex in sorted(examples, key=lambda e: e.example_id):
        m = meta.get(ex.example_id)
        if (
            m is None
            or m.received_at.tzinfo is None
            or not _REF_RE.fullmatch(m.thread_key)
            or not _REF_RE.fullmatch(m.campaign_key)
        ):
            dropped[ex.example_id] = DropReason.MISSING_METADATA
            continue
        usable.append((ex.example_id, m))
    groups = _Groups()
    for eid, m in usable:
        groups.union(f"e:{eid}", f"t:{m.thread_key}")
        groups.union(f"e:{eid}", f"c:{m.campaign_key}")
    members: dict[str, list[tuple[str, ExampleMeta]]] = {}
    for eid, m in usable:
        members.setdefault(groups.find(f"e:{eid}"), []).append((eid, m))
    assigned: dict[str, Split] = {}
    train_until = cutoff - embargo
    for group in members.values():
        first = min(m.received_at for _, m in group)
        last = max(m.received_at for _, m in group)
        if first >= cutoff:
            split: Split | DropReason = Split.EVAL
        elif last < train_until:
            split = Split.TRAIN
        elif last < cutoff:
            split = DropReason.IN_EMBARGO
        else:
            split = DropReason.STRADDLES_CUTOFF
        for eid, _ in group:
            if isinstance(split, Split):
                assigned[eid] = split
            else:
                dropped[eid] = split
    return assigned, dropped


def build_manifest(
    dataset_id: str,
    selection: SelectionManifest,
    examples: list[SelectedExample],
    meta: Mapping[str, ExampleMeta],
    *,
    cutoff: datetime,
    created_at: datetime,
    embargo: timedelta = timedelta(days=7),
    weak_label_hints: Mapping[str, list[str]] | None = None,
) -> dict[str, Any]:
    """Unsigned canonical body. ``weak_label_hints`` are provider codes, never labels."""
    if not _REF_RE.fullmatch(dataset_id):
        raise ManifestError("invalid dataset_id")
    ids = [e.example_id for e in examples]
    if len(set(ids)) != len(ids):
        raise ManifestError("duplicate example ids")
    assigned, dropped = assign_splits(examples, meta, cutoff=cutoff, embargo=embargo)
    hints = weak_label_hints or {}
    entries = []
    for ex in sorted(examples, key=lambda e: e.example_id):
        if ex.example_id not in assigned:
            continue
        entry: dict[str, Any] = {
            "example_id": ex.example_id,
            "split": assigned[ex.example_id].value,
            "label": ex.label.value,
            "label_source": "HUMAN_FEEDBACK",
            "label_proposal_id": ex.label_proposal_id,
            "consent_ref": ex.consent_ref,
            "source": ex.source.value,
            "content_sha256": hashlib.sha256(ex.text.encode()).hexdigest(),
            "received_at": _iso(meta[ex.example_id].received_at),
        }
        if ex.example_id in hints:
            entry["weak_hints"] = {
                "source": "PROVIDER_EXPORT",
                "strength": "WEAK",
                "used_as_label": False,
                "codes": sorted(set(hints[ex.example_id])),
            }
        entries.append(entry)
    counts: dict[str, dict[str, int]] = {}
    for e in entries:
        counts.setdefault(e["split"], {}).setdefault(e["label"], 0)
        counts[e["split"]][e["label"]] += 1
    drops: dict[str, int] = {}
    for reason in dropped.values():
        drops[reason.value] = drops.get(reason.value, 0) + 1
    return {
        "version": MANIFEST_VERSION,
        "dataset_id": dataset_id,
        "account_scope": selection.account_scope,
        "created_at": _iso(created_at),
        "selection": {
            "version": selection.version,
            "digest": hashlib.sha256(_canonical(selection.to_dict())).hexdigest(),
        },
        "split_rule": {
            "name": SPLIT_RULE,
            "cutoff": _iso(cutoff),
            "embargo_s": int(embargo.total_seconds()),
        },
        "counts": dict(sorted(counts.items())),
        "dropped": dict(sorted(drops.items())),
        "entries": entries,
        "review": None,
    }


def sign_manifest(
    body: dict[str, Any], review: HumanReview, key: Ed25519PrivateKey
) -> SignedManifest:
    if review.decision != "APPROVED" or not _REF_RE.fullmatch(review.reviewer_ref):
        raise ManifestError("a signed manifest needs an approving human review")
    if body.get("review") is not None:
        raise ManifestError("manifest already reviewed")
    final = json.loads(_canonical(body))  # deep copy; the caller's dict stays unsigned
    final["review"] = {
        "reviewer_ref": review.reviewer_ref,
        "reviewed_at": _iso(review.reviewed_at),
        "decision": review.decision,
    }
    final["signing_key_id"] = key_id(key.public_key().public_bytes_raw())  # signed
    canonical = _canonical(final)
    digest = hashlib.sha256(canonical).hexdigest()
    signature = base64.b64encode(key.sign(canonical)).decode()
    public = base64.b64encode(key.public_key().public_bytes_raw()).decode()
    return SignedManifest(final, digest, signature, public)


def _reviewed_copy(body: dict[str, Any], review: HumanReview) -> dict[str, Any]:
    if review.decision != "APPROVED" or not _REF_RE.fullmatch(review.reviewer_ref):
        raise ManifestError("a signed manifest needs an approving human review")
    if body.get("review") is not None:
        raise ManifestError("manifest already reviewed")
    final = json.loads(_canonical(body))
    final["review"] = {
        "reviewer_ref": review.reviewer_ref,
        "reviewed_at": _iso(review.reviewed_at),
        "decision": review.decision,
    }
    return final


def sign_manifest_ssh(
    body: dict[str, Any], review: HumanReview, signer: ManifestSigner, signer_key: SshPublicKey
) -> SignedManifest:
    """Sign with the owner's SSH key (SSHSIG, dedicated namespace).

    ``signer_key`` is the public half of the key ``signer`` uses; its fingerprint
    is embedded and signed so verification can pick the pinned key.
    """
    final = _reviewed_copy(body, review)
    final["signing_key_id"] = "ssh:" + signer_key.fingerprint
    canonical = _canonical(final)
    armored = signer.sign(canonical)
    try:
        signed_by = verify_sshsig(canonical, armored, {signer_key.fingerprint: signer_key})
    except SshSignatureError:
        raise ManifestError("signature does not match the declared signer key") from None
    digest = hashlib.sha256(canonical).hexdigest()
    return SignedManifest(final, digest, armored, signed_by)


def verify_manifest_ssh(raw: str, pinned: Mapping[str, SshPublicKey]) -> dict[str, Any]:
    """Verify an SSH-signed manifest against pinned keys (``pinned_ssh_keys``)."""
    try:
        doc = json.loads(raw)
        body, digest, signature = doc["body"], doc["digest"], doc["signature"]
    except (ValueError, KeyError, TypeError):
        raise ManifestError("malformed manifest") from None
    canonical = _canonical(body)
    if hashlib.sha256(canonical).hexdigest() != digest:
        raise ManifestError("digest mismatch")
    if not isinstance(body.get("review"), dict) or body["review"].get("decision") != "APPROVED":
        raise ManifestError("missing human review")
    try:
        signed_by = verify_sshsig(canonical, signature, dict(pinned))
    except SshSignatureError as exc:
        raise ManifestError(str(exc)) from None
    if body.get("signing_key_id") != "ssh:" + signed_by:
        raise ManifestError("signing key id does not match the signature")
    return body


def verify_manifest(raw: str, trusted: bytes | Mapping[str, bytes]) -> dict[str, Any]:
    """Verify against a trusted key, or a pinned ``key_id -> public key`` set
    (:func:`backend.mail_security.signing_keys.pinned_keys`). The key carried in
    the document is never trusted. Returns the body."""
    try:
        doc = json.loads(raw)
        body, digest, signature = doc["body"], doc["digest"], doc["signature"]
    except (ValueError, KeyError, TypeError):
        raise ManifestError("malformed manifest") from None
    canonical = _canonical(body)
    if hashlib.sha256(canonical).hexdigest() != digest:
        raise ManifestError("digest mismatch")
    if not isinstance(body.get("review"), dict) or body["review"].get("decision") != "APPROVED":
        raise ManifestError("missing human review")
    if isinstance(trusted, Mapping):
        try:
            trusted_public_key = resolve_trusted(trusted, body.get("signing_key_id"))
        except KeyCustodyError as exc:
            raise ManifestError(str(exc)) from None
    else:
        trusted_public_key = trusted
        if body.get("signing_key_id") != key_id(trusted_public_key):
            raise ManifestError("bad signature")
    try:
        Ed25519PublicKey.from_public_bytes(trusted_public_key).verify(
            base64.b64decode(signature), canonical
        )
    except (InvalidSignature, ValueError):
        raise ManifestError("bad signature") from None
    return body


__all__ = [
    "MANIFEST_VERSION",
    "SPLIT_RULE",
    "DropReason",
    "ExampleMeta",
    "HumanReview",
    "ManifestError",
    "SignedManifest",
    "Split",
    "assign_splits",
    "build_manifest",
    "sign_manifest",
    "sign_manifest_ssh",
    "verify_manifest",
    "verify_manifest_ssh",
]
