"""Threat-intelligence indicator domain logic for NerveShield (Phase 2, Task 2.2).

Pure, **network-free** and I/O-free. This module defines what a trustworthy
threat-intel indicator looks like, how it is canonicalized and validated on
ingest, when it is *live*, how a live match becomes typed evidence, and how a
user override is scoped. Persistence lives in
:mod:`backend.persistence.repositories.mail_intel_repo`; feed fetching,
signature verification and licensing checks arrive with Task 2.3.

Requirement anchors:

- **R-006.1/.2** — records carry explicit source, license, provenance, feed
  version, observation time and expiry; a match carries source, match method,
  confidence *type*, feed version, observation time and expiry.
- **R-006.3** — expired / disabled / rolled-back entries are never live but are
  retained for audit (:func:`is_live`).
- **R-006.4 / T-30 / R-023** — overrides are scoped by account + sender +
  indicator + expiry and need an approval reference; a personally trusted
  sender never becomes a global allowlist (:func:`validate_override_scope`).
- **R-015.1** — intel is an expiring, versioned store, never model weights.
- **T-19** — feed poisoning / stale intel: malformed or oversized records,
  already-expired records, absurdly long TTLs (:data:`MAX_TTL`), single-label
  (TLD-wide) domains, and missing source/license are rejected with typed,
  content-free errors.
- **R-004.5 / I-02** — no data means *no match* (UNKNOWN), never "safe".
- **I-09 / R-006.2** — a match is **evidence only**: :func:`match_to_finding`
  builds a typed finding; nothing here (or in the repository) triggers an
  action. This module is deliberately *not* wired into the worker, inspections
  or policy (a later task does that).

Privacy: only canonical domains / IP literals or SHA-256 hashes are stored;
errors carry a code, never the offending value; findings carry safe codes and
opaque refs only.
"""

from __future__ import annotations

import hashlib
import ipaddress
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Annotated, Any
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    StringConstraints,
    ValidationError,
    model_validator,
)

from backend.mail_security._domains import domain_of
from backend.mail_security.models import (
    FindingCategory,
    FindingSeverity,
    FindingStatus,
    OpaqueRef,
    SecurityFinding,
)

INTEL_VERSION = "intel-1"

#: Maximum lifetime of an indicator measured from ingest time (T-19). Longer
#: TTLs are rejected rather than clamped so a poisoned feed cannot pin a record.
MAX_TTL = timedelta(days=90)
#: Maximum lifetime of a user override (R-006.4).
MAX_OVERRIDE_TTL = timedelta(days=365)
#: Maximum records in one promotion batch.
MAX_BATCH = 1000
#: Tolerated clock skew for ``first_seen`` / ``last_seen`` in the future.
MAX_FUTURE_SKEW = timedelta(days=1)
#: Cap for any single incoming string value before parsing (oversize guard).
MAX_INPUT_LEN = 2048
_MAX_DOMAIN_LEN = 253

_HEX64 = re.compile(r"^[A-Fa-f0-9]{64}$")
_LABEL = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")

#: ``source`` / ``feed_version`` end up inside opaque finding refs, so they use
#: the OpaqueRef alphabet.
_RefToken = Annotated[
    str, StringConstraints(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9:_-]+$")
]
_SourceToken = Annotated[
    str, StringConstraints(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9:_-]+$")
]
_LicenseToken = Annotated[
    str, StringConstraints(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._:+-]+$")
]


class IndicatorType(StrEnum):
    """Supported indicator kinds. Hashes are stored, never raw URLs."""

    DOMAIN = "DOMAIN"
    URL_SHA256 = "URL_SHA256"
    FILE_SHA256 = "FILE_SHA256"
    IP_LITERAL = "IP_LITERAL"


class ConfidenceType(StrEnum):
    """What the source's confidence *means*. Never a fabricated probability."""

    UNVERIFIED = "UNVERIFIED"
    FEED_ASSERTED = "FEED_ASSERTED"
    ANALYST_REVIEWED = "ANALYST_REVIEWED"


class IndicatorScope(StrEnum):
    """Indicator audience. Indicators are global threat intel (design §5.3)."""

    GLOBAL = "GLOBAL"


class MatchMethod(StrEnum):
    EXACT = "EXACT"
    PARENT_DOMAIN = "PARENT_DOMAIN"


class IntelErrorCode(StrEnum):
    MALFORMED_RECORD = "MALFORMED_RECORD"
    OVERSIZED_RECORD = "OVERSIZED_RECORD"
    INVALID_INDICATOR = "INVALID_INDICATOR"
    MISSING_SOURCE = "MISSING_SOURCE"
    MISSING_LICENSE = "MISSING_LICENSE"
    EXPIRES_IN_PAST = "EXPIRES_IN_PAST"
    TTL_TOO_LONG = "TTL_TOO_LONG"
    INVALID_TIMESTAMPS = "INVALID_TIMESTAMPS"
    BATCH_TOO_LARGE = "BATCH_TOO_LARGE"
    DUPLICATE_IN_BATCH = "DUPLICATE_IN_BATCH"
    INVALID_ROLLBACK_TARGET = "INVALID_ROLLBACK_TARGET"
    INVALID_OVERRIDE_SCOPE = "INVALID_OVERRIDE_SCOPE"
    MISSING_APPROVAL = "MISSING_APPROVAL"
    OVERRIDE_TTL_INVALID = "OVERRIDE_TTL_INVALID"


class IntelError(ValueError):
    """Typed, content-free ingest/promotion error. ``str(e)`` is the code only."""

    def __init__(self, code: IntelErrorCode) -> None:
        super().__init__(code.value)
        self.code = code


def aware(value: datetime) -> datetime:
    """Treat a naive datetime (SQLite round-trip) as UTC."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


# -- canonicalization ---------------------------------------------------------


def _canonical_domain(value: str) -> str:
    text = value.strip()
    if "@" in text:
        extracted = domain_of(text)
        if extracted is None:
            raise IntelError(IntelErrorCode.INVALID_INDICATOR)
        text = extracted
    text = text.lower().rstrip(".")
    if not text or len(text) > _MAX_DOMAIN_LEN:
        raise IntelError(IntelErrorCode.INVALID_INDICATOR)
    try:
        ascii_form = text.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise IntelError(IntelErrorCode.INVALID_INDICATOR) from exc
    labels = ascii_form.split(".")
    # A single label would be a TLD-wide indicator: a poisoning vector (T-19).
    if len(labels) < 2 or not all(_LABEL.match(label) for label in labels):
        raise IntelError(IntelErrorCode.INVALID_INDICATOR)
    return ascii_form


def _canonical_url_hash(value: str) -> str:
    text = value.strip()
    if _HEX64.match(text):
        return text.lower()
    try:
        parts = urlsplit(text)
        host = parts.hostname
        port = parts.port
    except ValueError as exc:
        raise IntelError(IntelErrorCode.INVALID_INDICATOR) from exc
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https") or not host:
        raise IntelError(IntelErrorCode.INVALID_INDICATOR)
    try:
        host_part = str(ipaddress.ip_address(host))
        if ":" in host_part:
            host_part = f"[{host_part}]"
    except ValueError:
        host_part = _canonical_domain(host)
    default = 443 if scheme == "https" else 80
    netloc = host_part if port in (None, default) else f"{host_part}:{port}"
    query = f"?{parts.query}" if parts.query else ""
    canonical = f"{scheme}://{netloc}{parts.path or '/'}{query}"
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def canonicalize_indicator(indicator_type: IndicatorType, value: str) -> str:
    """Return the canonical stored form of ``value`` or raise :class:`IntelError`.

    Domains are lowercased IDNA/ASCII (reusing :func:`domain_of` for address
    input); URLs are canonicalized then SHA-256 hashed (or a 64-hex hash is
    accepted as-is); IP literals use :mod:`ipaddress` normal form. No network.
    """
    if not isinstance(value, str) or not value.strip():
        raise IntelError(IntelErrorCode.INVALID_INDICATOR)
    if len(value) > MAX_INPUT_LEN:
        raise IntelError(IntelErrorCode.OVERSIZED_RECORD)
    if indicator_type is IndicatorType.DOMAIN:
        return _canonical_domain(value)
    if indicator_type is IndicatorType.URL_SHA256:
        return _canonical_url_hash(value)
    if indicator_type is IndicatorType.FILE_SHA256:
        text = value.strip()
        if not _HEX64.match(text):
            raise IntelError(IntelErrorCode.INVALID_INDICATOR)
        return text.lower()
    if indicator_type is IndicatorType.IP_LITERAL:
        try:
            return str(ipaddress.ip_address(value.strip()))
        except ValueError as exc:
            raise IntelError(IntelErrorCode.INVALID_INDICATOR) from exc
    raise IntelError(IntelErrorCode.INVALID_INDICATOR)  # pragma: no cover


def lookup_candidates(
    indicator_type: IndicatorType, value: str
) -> list[tuple[str, MatchMethod]]:
    """Canonical values to look up (with match method). Invalid input → ``[]``.

    Domains also match their parent domains (never a bare TLD).
    """
    try:
        canonical = canonicalize_indicator(indicator_type, value)
    except IntelError:
        return []
    out = [(canonical, MatchMethod.EXACT)]
    if indicator_type is IndicatorType.DOMAIN:
        labels = canonical.split(".")
        for i in range(1, len(labels) - 1):
            out.append((".".join(labels[i:]), MatchMethod.PARENT_DOMAIN))
    return out


# -- record validation --------------------------------------------------------


class IntelRecord(BaseModel):
    """A validated, canonical indicator ready for promotion."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, frozen=True)

    indicator_type: IndicatorType
    canonical_indicator: Annotated[str, StringConstraints(min_length=1, max_length=255)]
    source: _SourceToken
    confidence_type: ConfidenceType
    license: _LicenseToken
    provenance: OpaqueRef
    scope: IndicatorScope = IndicatorScope.GLOBAL
    feed_version: _RefToken
    first_seen: datetime
    last_seen: datetime
    expires_at: datetime
    enabled: bool = True

    @model_validator(mode="after")
    def _ordering(self) -> IntelRecord:
        if aware(self.first_seen) > aware(self.last_seen) or aware(
            self.last_seen
        ) >= aware(self.expires_at):
            raise ValueError("timestamps out of order")
        return self

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.indicator_type.value, self.canonical_indicator, self.source)


_RAW_FIELDS = frozenset(
    {
        "indicator_type",
        "value",
        "source",
        "confidence_type",
        "license",
        "provenance",
        "scope",
        "feed_version",
        "first_seen",
        "last_seen",
        "expires_at",
        "enabled",
    }
)


def check_ttl(expires_at: datetime, now: datetime) -> None:
    """Reject expired or absurdly long-lived indicators (T-19)."""
    expires = aware(expires_at)
    if expires <= aware(now):
        raise IntelError(IntelErrorCode.EXPIRES_IN_PAST)
    if expires - aware(now) > MAX_TTL:
        raise IntelError(IntelErrorCode.TTL_TOO_LONG)


def validate_record(raw: Mapping[str, Any], *, now: datetime) -> IntelRecord:
    """Validate one raw feed record. Never crashes; never echoes the input.

    Raises :class:`IntelError` with a typed code. ``raw['value']`` is the
    uncanonicalized indicator; it is canonicalized and *not* retained.
    """
    if not isinstance(raw, Mapping):
        raise IntelError(IntelErrorCode.MALFORMED_RECORD)
    if len(raw) > len(_RAW_FIELDS) + 4:
        raise IntelError(IntelErrorCode.OVERSIZED_RECORD)
    for item in raw.values():
        if isinstance(item, str) and len(item) > MAX_INPUT_LEN:
            raise IntelError(IntelErrorCode.OVERSIZED_RECORD)
    if not str(raw.get("source") or "").strip():
        raise IntelError(IntelErrorCode.MISSING_SOURCE)
    if not str(raw.get("license") or "").strip():
        raise IntelError(IntelErrorCode.MISSING_LICENSE)
    if set(raw) - _RAW_FIELDS:
        raise IntelError(IntelErrorCode.MALFORMED_RECORD)
    try:
        indicator_type = IndicatorType(raw.get("indicator_type"))
    except ValueError as exc:
        raise IntelError(IntelErrorCode.MALFORMED_RECORD) from exc
    canonical = canonicalize_indicator(indicator_type, raw.get("value"))  # type: ignore[arg-type]
    fields = {k: v for k, v in raw.items() if k != "value"}
    try:
        record = IntelRecord(canonical_indicator=canonical, **fields)
    except (ValidationError, TypeError, ValueError) as exc:
        raise IntelError(IntelErrorCode.MALFORMED_RECORD) from exc
    now_utc = aware(now)
    if (
        aware(record.first_seen) > now_utc + MAX_FUTURE_SKEW
        or aware(record.last_seen) > now_utc + MAX_FUTURE_SKEW
    ):
        raise IntelError(IntelErrorCode.INVALID_TIMESTAMPS)
    check_ttl(record.expires_at, now_utc)
    return record


# -- liveness and matches -----------------------------------------------------


def is_live(
    *,
    enabled: bool,
    rolled_back: bool,
    superseded: bool,
    expires_at: datetime,
    now: datetime,
) -> bool:
    """An indicator is live only if enabled, not rolled back/superseded and
    strictly before ``expires_at`` (at exactly ``expires_at`` it is expired)."""
    return (
        enabled
        and not rolled_back
        and not superseded
        and aware(now) < aware(expires_at)
    )


@dataclass(frozen=True)
class IntelMatch:
    """A live indicator match. Evidence only — never an action (I-09)."""

    indicator_id: str
    indicator_type: IndicatorType
    canonical_indicator: str
    source: str
    confidence_type: ConfidenceType
    feed_version: str
    first_seen: datetime
    last_seen: datetime
    expires_at: datetime
    promotion_version: int
    match_method: MatchMethod


_SEVERITY = {
    ConfidenceType.UNVERIFIED: FindingSeverity.UNKNOWN,
    ConfidenceType.FEED_ASSERTED: FindingSeverity.MEDIUM,
    ConfidenceType.ANALYST_REVIEWED: FindingSeverity.HIGH,
}


def _epoch(value: datetime) -> int:
    return int(aware(value).timestamp())


def match_to_finding(
    match: IntelMatch, *, evidence_ref: str | None = None
) -> SecurityFinding:
    """Turn a live match into a typed finding (R-006.2).

    Source, match method, confidence *type*, feed version, observation time and
    expiry are carried as opaque refs (``intel:<field>:<token>``). No raw
    content or indicator text is included; the matched value is represented
    only by the deterministic ``finding_key`` digest. The finding is evidence,
    not an action (I-09).
    """
    digest = hashlib.sha256(
        "\x1f".join(
            (
                match.indicator_type.value,
                match.canonical_indicator,
                match.source,
                match.feed_version,
            )
        ).encode("utf-8")
    ).hexdigest()[:32]
    refs = [
        f"intel:src:{match.source}",
        f"intel:method:{match.match_method.value}",
        f"intel:conf:{match.confidence_type.value}",
        f"intel:feed:{match.feed_version}",
        f"intel:obs:{_epoch(match.last_seen)}",
        f"intel:exp:{_epoch(match.expires_at)}",
    ]
    if evidence_ref is not None:
        refs.append(evidence_ref)
    return SecurityFinding(
        finding_key=f"intel:{digest}",
        rule_id="THREAT_INTEL_MATCH",
        category=FindingCategory.INTEL_MATCH,
        severity=_SEVERITY[match.confidence_type],
        status=FindingStatus.SUSPECTED,
        detector_version=INTEL_VERSION,
        evidence_refs=refs,
        explanation_code=f"INTEL_MATCH.{match.indicator_type.value}",
    )


# -- overrides ----------------------------------------------------------------


def sender_key(account_scope: str, address: str) -> str:
    """Opaque, account-bound sender key. Never stores the address (T-30)."""
    material = f"{len(account_scope)}:{account_scope}\x1f{address.strip().lower()}"
    return f"sender:{hashlib.sha256(material.encode('utf-8')).hexdigest()[:32]}"


_SCOPE_RE = re.compile(r"^[A-Za-z0-9:_-]{1,128}$")
_REF_RE = re.compile(r"^[A-Za-z0-9:_-]{1,200}$")


def is_valid_approval_ref(ref: object) -> bool:
    """True for an opaque approval reference (same alphabet as override refs)."""
    # fullmatch: ``$`` would also accept a trailing newline
    return isinstance(ref, str) and _REF_RE.fullmatch(ref) is not None


def validate_override_scope(
    *,
    account_scope: str,
    sender: str,
    indicator_type: IndicatorType,
    indicator_value: str,
    approval_ref: str | None,
    expires_at: datetime,
    now: datetime,
) -> str:
    """Validate an override request; return the canonical indicator.

    Account, sender, an exact indicator and an expiry are all mandatory and
    wildcard-free, so an override can never be a global allowlist (T-30), and
    an approval reference is required (R-006.4).
    """
    if not isinstance(account_scope, str) or not _SCOPE_RE.match(account_scope):
        raise IntelError(IntelErrorCode.INVALID_OVERRIDE_SCOPE)
    if not isinstance(sender, str) or not _REF_RE.match(sender):
        raise IntelError(IntelErrorCode.INVALID_OVERRIDE_SCOPE)
    if not approval_ref or not _REF_RE.match(approval_ref):
        raise IntelError(IntelErrorCode.MISSING_APPROVAL)
    canonical = canonicalize_indicator(indicator_type, indicator_value)
    expires = aware(expires_at)
    if expires <= aware(now) or expires - aware(now) > MAX_OVERRIDE_TTL:
        raise IntelError(IntelErrorCode.OVERRIDE_TTL_INVALID)
    return canonical


@dataclass(frozen=True)
class SuppressedMatch:
    """A live match suppressed for this account+sender by an override.

    Retained as evidence (not erased); ``approval_ref`` records who approved.
    """

    match: IntelMatch
    approval_ref: str
    override_expires_at: datetime


@dataclass(frozen=True)
class IntelEvaluation:
    """Result of an account/sender-scoped lookup. Empty ``matches`` means
    *unknown* (no intel), never "safe" (R-004.5)."""

    matches: tuple[IntelMatch, ...]
    suppressed: tuple[SuppressedMatch, ...]


@dataclass(frozen=True)
class IntelChange:
    indicator_type: str
    canonical_indicator: str
    source: str
    action: str  # ADDED | REPLACED | UNCHANGED | ROLLED_BACK | RESTORED


@dataclass(frozen=True)
class PromotionPlan:
    """Outcome (or dry-run preview) of a promotion or rollback."""

    from_version: int
    to_version: int
    dry_run: bool
    changes: tuple[IntelChange, ...]

    def count(self, action: str) -> int:
        return sum(1 for c in self.changes if c.action == action)


__all__ = [
    "INTEL_VERSION",
    "MAX_BATCH",
    "MAX_OVERRIDE_TTL",
    "MAX_TTL",
    "ConfidenceType",
    "IndicatorScope",
    "IndicatorType",
    "IntelChange",
    "IntelError",
    "IntelErrorCode",
    "IntelEvaluation",
    "IntelMatch",
    "IntelRecord",
    "MatchMethod",
    "PromotionPlan",
    "SuppressedMatch",
    "aware",
    "canonicalize_indicator",
    "check_ttl",
    "is_live",
    "is_valid_approval_ref",
    "lookup_candidates",
    "match_to_finding",
    "sender_key",
    "validate_override_scope",
    "validate_record",
]
