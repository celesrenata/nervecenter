"""Optional, disabled-by-default threat-intel feed adapters (Phase 2, Task 2.3).

There is **no network client in this module** and none may be added here. The
outbound egress policy and SSRF controls (T-09) are Task 2.5; until that
exists, fetching is an injected :class:`FeedFetcher` callable that returns
bytes plus transport metadata. Tests use a fake fetcher and synthetic offline
fixtures. A real fetcher over the egress policy is explicitly deferred to
Task 2.5 (it must also bound bytes read, enforce timeouts, and resolve a
``source`` name to an allow-listed endpoint from deployment config).

What this module is:

(a) pure, bounded parsers/validators for payload bytes (:func:`parse_payload`);
(b) an integrity + license gate (:func:`verify_integrity`, :func:`validate_config`);
(c) explicit-call orchestration :func:`ingest_feed` that feeds the Task 2.2
    store through its validated :meth:`MailIntelRepository.promote` API
    (versioned, ``dry_run`` and rollback already supported there).

It is not wired into the worker, inspections, policy, startup or a scheduler.

Requirement anchors: R-006.1 (provenance, integrity, license, parser bounds,
indicator schema), R-006.3 (stale/unavailable feeds invalidate, never erase
audit), R-017.3 (remote lookup needs explicit grants: this module sends
nothing), R-022.5 / T-27 (supply chain: pinned integrity, license allow-list),
T-19 (feed poisoning / stale intel), I-04.

Poisoning guards (all run before anything is written):

1. integrity pin mismatch  -> whole payload rejected;
2. payload size / record / per-record caps, control chars, nesting depth;
3. protected domains: a feed that lists one is **rejected entirely** (a feed
   claiming a top site is malicious is treated as poisoned, not trimmed);
   matching is exact on the canonical domain;
4. change guards, measured against the source's live set (dry-run plan is
   returned, nothing is written, pending explicit owner approval of that exact
   feed version):

   - additions beyond ``max_change_ratio`` (``CHANGE_RATIO_EXCEEDED``);
   - removals beyond ``max_removal_ratio`` (``REMOVAL_RATIO_EXCEEDED``). A
     *removal* is a live indicator the update would drop without replacement:
     either absent from the incoming snapshot (counted conservatively even
     though ``promote`` alone would leave it live until expiry, because the
     feed is a snapshot and operators treat omission as retraction) or
     replaced by a version that expires *earlier* (early expiry);
   - the first load into an empty live set (including a fully expired or
     rolled-back one) has no ratio, so it needs an explicit opaque
     ``initial_load_approval_ref`` (``INITIAL_LOAD_APPROVAL_REQUIRED``);
5. duplicates collapse deterministically (shortest TTL wins);
6. TTL is capped by config (<= :data:`~backend.mail_security.intel.MAX_TTL`)
   so a feed that stops updating ages out.

Clock: one instant is read per ingest (the injected ``now``, or the repository
clock when ``now`` is omitted) and passed to the live-set read and to
``promote``, so the guard, record validation and promotion agree.

A fetch failure or timeout never touches the store and never marks anything
safe: the previous live set simply runs to its natural expiry.

Privacy: adapters take **no mail-derived input**. The fetcher receives only the
configured source name.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from enum import StrEnum
from typing import Any, Protocol

import structlog

from backend.mail_security.ingestion_seam import mail_security_enabled
from backend.mail_security.intel import (
    MAX_BATCH,
    MAX_TTL,
    ConfidenceType,
    IndicatorType,
    IntelError,
    PromotionPlan,
    aware,
    canonicalize_indicator,
    is_valid_approval_ref,
    validate_record,
)

_log = structlog.get_logger(__name__)

FEED_SCHEMA = "nerveshield.feed.v1"
#: Absolute ceilings; a config may only be stricter.
MAX_PAYLOAD_CEILING = 1_048_576
MAX_RECORD_LEN = 253
MAX_JSON_DEPTH = 4
DEFAULT_MAX_CHANGE_RATIO = 0.5
#: Fraction of the live set a single update may remove / expire early.
DEFAULT_MAX_REMOVAL_RATIO = 0.5
DEFAULT_FETCH_TIMEOUT_S = 10.0
_SHA256_HEX = re.compile(r"^[a-f0-9]{64}$")
_SOURCE = re.compile(r"^[A-Za-z0-9:_-]{1,100}$")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")

#: Declared licenses a feed may carry (permissive/open data licenses only).
ALLOWED_LICENSES = frozenset(
    {"CC0-1.0", "CC-BY-4.0", "MIT", "Apache-2.0", "ODbL-1.0", "ODC-By-1.0"}
)

#: Domains no feed may ever mark malicious (exact match on canonical form).
#: Deliberately small and documented; deployments extend it via
#: ``FeedConfig.extra_protected_domains`` (e.g. their own domains).
PROTECTED_DOMAINS = frozenset(
    {
        "google.com",
        "gmail.com",
        "microsoft.com",
        "outlook.com",
        "office.com",
        "apple.com",
        "icloud.com",
        "amazon.com",
        "paypal.com",
        "github.com",
        "cloudflare.com",
        "wikipedia.org",
        "facebook.com",
        "linkedin.com",
        "yahoo.com",
    }
)


class FeedFormat(StrEnum):
    DOMAIN_LINES = "DOMAIN_LINES"
    JSON_LIST = "JSON_LIST"


class IntegrityScheme(StrEnum):
    SHA256_PIN = "SHA256_PIN"
    HMAC_SHA256 = "HMAC_SHA256"


class FeedErrorCode(StrEnum):
    MASTER_FLAG_OFF = "MASTER_FLAG_OFF"
    FEED_DISABLED = "FEED_DISABLED"
    CONFIG_INVALID = "CONFIG_INVALID"
    MISSING_LICENSE = "MISSING_LICENSE"
    LICENSE_NOT_ALLOWED = "LICENSE_NOT_ALLOWED"
    LICENSE_MISMATCH = "LICENSE_MISMATCH"
    MISSING_INTEGRITY = "MISSING_INTEGRITY"
    FETCH_FAILED = "FETCH_FAILED"
    FETCH_TIMEOUT = "FETCH_TIMEOUT"
    TRANSPORT_INSECURE = "TRANSPORT_INSECURE"
    INTEGRITY_MISMATCH = "INTEGRITY_MISMATCH"
    PAYLOAD_TOO_LARGE = "PAYLOAD_TOO_LARGE"
    TOO_MANY_RECORDS = "TOO_MANY_RECORDS"
    RECORD_TOO_LONG = "RECORD_TOO_LONG"
    CONTROL_CHARACTERS = "CONTROL_CHARACTERS"
    MALFORMED_PAYLOAD = "MALFORMED_PAYLOAD"
    NESTING_TOO_DEEP = "NESTING_TOO_DEEP"
    SCHEMA_VIOLATION = "SCHEMA_VIOLATION"
    RECORD_INVALID = "RECORD_INVALID"
    PROTECTED_DOMAIN = "PROTECTED_DOMAIN"
    CHANGE_RATIO_EXCEEDED = "CHANGE_RATIO_EXCEEDED"
    REMOVAL_RATIO_EXCEEDED = "REMOVAL_RATIO_EXCEEDED"
    INITIAL_LOAD_APPROVAL_REQUIRED = "INITIAL_LOAD_APPROVAL_REQUIRED"
    INGEST_REJECTED = "INGEST_REJECTED"


class FeedError(ValueError):
    """Typed, content-free feed error. ``str(e)`` is the code only."""

    def __init__(self, code: FeedErrorCode) -> None:
        super().__init__(code.value)
        self.code = code


class RunStatus(StrEnum):
    INGESTED = "INGESTED"
    DRY_RUN = "DRY_RUN"
    REJECTED = "REJECTED"  # payload judged unsafe/invalid; nothing written
    FAILED = "FAILED"  # could not obtain a payload; previous live set kept
    NEEDS_OWNER_REVIEW = "NEEDS_OWNER_REVIEW"  # change guards; nothing written
    REFUSED = "REFUSED"  # disabled / unconfigured; nothing attempted


@dataclass(frozen=True)
class IntegrityPin:
    """Pinned integrity value.

    ``SHA256_PIN``: ``sha256_hex`` is the expected digest of the exact payload
    bytes (suits a pinned snapshot; a rolling feed needs a new pin per
    release). ``HMAC_SHA256``: ``hmac_key`` is a pre-shared secret and the
    fetcher supplies a hex detached signature (symmetric: it proves origin only
    to holders of the key; there is no public-key signature in this task).
    """

    scheme: IntegrityScheme
    sha256_hex: str | None = None
    hmac_key: bytes | None = field(default=None, repr=False)


@dataclass(frozen=True)
class FeedConfig:
    """Per-feed configuration. ``enabled`` defaults to False (R-017 / design §14)."""

    source: str
    license: str
    format: FeedFormat
    integrity: IntegrityPin | None
    max_payload_bytes: int
    max_records: int
    ttl: timedelta
    enabled: bool = False
    max_change_ratio: float = DEFAULT_MAX_CHANGE_RATIO
    max_removal_ratio: float = DEFAULT_MAX_REMOVAL_RATIO
    require_tls: bool = True
    fetch_timeout_s: float = DEFAULT_FETCH_TIMEOUT_S
    confidence_type: ConfidenceType = ConfidenceType.FEED_ASSERTED
    extra_protected_domains: frozenset[str] = frozenset()


@dataclass(frozen=True)
class FetchedFeed:
    """What a :class:`FeedFetcher` returns: bytes plus transport metadata."""

    payload: bytes
    tls_verified: bool
    signature_hex: str | None = None


class FeedFetcher(Protocol):
    """Injected fetch capability. Receives **only** the configured source name
    (no mail-derived input, no URL). No default implementation exists until the
    Task 2.5 egress policy."""

    def __call__(self, source: str) -> Awaitable[FetchedFeed]: ...


@dataclass(frozen=True)
class ParsedEntry:
    indicator_type: IndicatorType
    value: str  # canonical
    ttl: timedelta | None  # per-record request, already <= cap


@dataclass(frozen=True)
class FeedRunResult:
    """Typed outcome record. Contains codes and counts only."""

    source: str
    status: RunStatus
    code: FeedErrorCode | None = None
    feed_version: str | None = None
    accepted: int = 0
    duplicates: int = 0
    change_ratio: float | None = None
    removed: int = 0  # live indicators dropped without replacement
    expired_early: int = 0  # live indicators replaced by an earlier expiry
    removal_ratio: float | None = None
    plan: PromotionPlan | None = None


def validate_config(config: FeedConfig) -> None:
    """Refuse an incomplete or unsafe configuration (R-006.1, T-27)."""
    if not _SOURCE.match(config.source or ""):
        raise FeedError(FeedErrorCode.CONFIG_INVALID)
    if not (config.license or "").strip():
        raise FeedError(FeedErrorCode.MISSING_LICENSE)
    if config.license not in ALLOWED_LICENSES:
        raise FeedError(FeedErrorCode.LICENSE_NOT_ALLOWED)
    pin = config.integrity
    if pin is None:
        raise FeedError(FeedErrorCode.MISSING_INTEGRITY)
    if pin.scheme is IntegrityScheme.SHA256_PIN:
        if not pin.sha256_hex or not _SHA256_HEX.match(pin.sha256_hex):
            raise FeedError(FeedErrorCode.MISSING_INTEGRITY)
    elif pin.scheme is IntegrityScheme.HMAC_SHA256:
        if not pin.hmac_key:
            raise FeedError(FeedErrorCode.MISSING_INTEGRITY)
    else:  # pragma: no cover - enum exhaustive
        raise FeedError(FeedErrorCode.MISSING_INTEGRITY)
    if not isinstance(config.format, FeedFormat):
        raise FeedError(FeedErrorCode.CONFIG_INVALID)
    if not 0 < config.max_payload_bytes <= MAX_PAYLOAD_CEILING:
        raise FeedError(FeedErrorCode.CONFIG_INVALID)
    if not 0 < config.max_records <= MAX_BATCH:
        raise FeedError(FeedErrorCode.CONFIG_INVALID)
    if not timedelta(0) < config.ttl <= MAX_TTL:
        raise FeedError(FeedErrorCode.CONFIG_INVALID)
    if not 0.0 <= config.max_change_ratio <= 1.0:
        raise FeedError(FeedErrorCode.CONFIG_INVALID)
    if not 0.0 <= config.max_removal_ratio <= 1.0:
        raise FeedError(FeedErrorCode.CONFIG_INVALID)
    if not 0 < config.fetch_timeout_s <= 120:
        raise FeedError(FeedErrorCode.CONFIG_INVALID)


class FeedRegistry:
    """Explicit registry of configured feeds. Registering never enables or runs
    anything; every config is validated on registration."""

    def __init__(self) -> None:
        self._feeds: dict[str, FeedConfig] = {}

    def register(self, config: FeedConfig) -> None:
        validate_config(config)
        if config.source in self._feeds:
            raise FeedError(FeedErrorCode.CONFIG_INVALID)
        self._feeds[config.source] = config

    def get(self, source: str) -> FeedConfig | None:
        return self._feeds.get(source)

    def enabled_sources(self) -> list[str]:
        return sorted(s for s, c in self._feeds.items() if c.enabled)


# -- integrity ----------------------------------------------------------------


def verify_integrity(config: FeedConfig, payload: bytes, signature_hex: str | None) -> None:
    """Raise ``INTEGRITY_MISMATCH`` unless the payload matches the pinned value."""
    pin = config.integrity
    if pin is None:
        raise FeedError(FeedErrorCode.MISSING_INTEGRITY)
    if pin.scheme is IntegrityScheme.SHA256_PIN:
        expected = pin.sha256_hex or ""
        actual = hashlib.sha256(payload).hexdigest()
    else:
        if not pin.hmac_key:
            raise FeedError(FeedErrorCode.MISSING_INTEGRITY)
        expected = hmac.new(pin.hmac_key, payload, hashlib.sha256).hexdigest()
        actual = (signature_hex or "").lower()
    if not hmac.compare_digest(expected.encode(), actual.encode()):
        raise FeedError(FeedErrorCode.INTEGRITY_MISMATCH)


# -- bounded parsing ----------------------------------------------------------


def _check_text(text: str) -> None:
    if _CONTROL.search(text):
        raise FeedError(FeedErrorCode.CONTROL_CHARACTERS)
    if len(text) > MAX_RECORD_LEN:
        raise FeedError(FeedErrorCode.RECORD_TOO_LONG)


def _json_depth_ok(text: str) -> bool:
    """Linear scan (outside strings) so deep nesting is rejected before the
    recursive JSON decoder ever runs."""
    depth = 0
    in_string = False
    escaped = False
    for ch in text:
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
        elif ch in "[{":
            depth += 1
            if depth > MAX_JSON_DEPTH:
                return False
        elif ch in "]}":
            depth -= 1
    return True


def _decode(payload: bytes) -> str:
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise FeedError(FeedErrorCode.MALFORMED_PAYLOAD) from exc


def _canon(indicator_type: IndicatorType, value: str) -> str:
    try:
        return canonicalize_indicator(indicator_type, value)
    except IntelError as exc:
        raise FeedError(FeedErrorCode.RECORD_INVALID) from exc


def _parse_lines(
    text: str, config: FeedConfig
) -> list[tuple[IndicatorType, str, timedelta | None]]:
    out: list[tuple[IndicatorType, str, timedelta | None]] = []
    # A bounded payload bounds line count, but comments must not hide records.
    for line in text.split("\n"):
        line = line.rstrip("\r")
        _check_text(line)
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if len(out) >= config.max_records:
            raise FeedError(FeedErrorCode.TOO_MANY_RECORDS)
        out.append((IndicatorType.DOMAIN, _canon(IndicatorType.DOMAIN, stripped), None))
    return out


def _reject_dupe_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    keys = [k for k, _ in pairs]
    if len(set(keys)) != len(keys):
        raise ValueError("duplicate key")
    return dict(pairs)


def _parse_json(text: str, config: FeedConfig) -> list[tuple[IndicatorType, str, timedelta | None]]:
    if not _json_depth_ok(text):
        raise FeedError(FeedErrorCode.NESTING_TOO_DEEP)
    try:
        doc = json.loads(
            text,
            object_pairs_hook=_reject_dupe_keys,
            parse_constant=lambda _c: (_ for _ in ()).throw(ValueError("constant")),
        )
    except RecursionError as exc:
        raise FeedError(FeedErrorCode.NESTING_TOO_DEEP) from exc
    except ValueError as exc:
        raise FeedError(FeedErrorCode.MALFORMED_PAYLOAD) from exc
    if not isinstance(doc, dict) or set(doc) != {"schema", "source", "license", "indicators"}:
        raise FeedError(FeedErrorCode.SCHEMA_VIOLATION)
    if doc["schema"] != FEED_SCHEMA or doc["source"] != config.source:
        raise FeedError(FeedErrorCode.SCHEMA_VIOLATION)
    if doc["license"] != config.license:
        raise FeedError(FeedErrorCode.LICENSE_MISMATCH)
    items = doc["indicators"]
    if not isinstance(items, list):
        raise FeedError(FeedErrorCode.SCHEMA_VIOLATION)
    if len(items) > config.max_records:
        raise FeedError(FeedErrorCode.TOO_MANY_RECORDS)
    out: list[tuple[IndicatorType, str, timedelta | None]] = []
    for item in items:
        if not isinstance(item, dict) or not {"value"} <= set(item) <= {"value", "ttl_hours"}:
            raise FeedError(FeedErrorCode.SCHEMA_VIOLATION)
        value = item["value"]
        if not isinstance(value, str):
            raise FeedError(FeedErrorCode.SCHEMA_VIOLATION)
        _check_text(value)
        ttl: timedelta | None = None
        if "ttl_hours" in item:
            hours = item["ttl_hours"]
            if isinstance(hours, bool) or not isinstance(hours, int) or hours < 1:
                raise FeedError(FeedErrorCode.SCHEMA_VIOLATION)
            ttl = min(timedelta(hours=hours), config.ttl)
        out.append((IndicatorType.DOMAIN, _canon(IndicatorType.DOMAIN, value), ttl))
    return out


def parse_payload(config: FeedConfig, payload: bytes) -> tuple[list[ParsedEntry], int]:
    """Parse and bound a payload. Returns ``(entries, duplicates_collapsed)``.

    Size is checked before any decoding. Entries are canonical, de-duplicated
    (the shortest requested TTL wins so conflicts resolve conservatively) and
    sorted for determinism. Raises :class:`FeedError` only.
    """
    if not isinstance(payload, bytes) or len(payload) > config.max_payload_bytes:
        raise FeedError(FeedErrorCode.PAYLOAD_TOO_LARGE)
    text = _decode(payload)
    if config.format is FeedFormat.DOMAIN_LINES:
        raw = _parse_lines(text, config)
    else:
        raw = _parse_json(text, config)
    merged: dict[tuple[IndicatorType, str], timedelta | None] = {}
    duplicates = 0
    for itype, value, ttl in raw:
        key = (itype, value)
        if key in merged:
            duplicates += 1
            prev = merged[key]
            if prev is None or (ttl is not None and ttl < prev):
                merged[key] = ttl if ttl is not None else prev
            continue
        merged[key] = ttl
    entries = [
        ParsedEntry(t, v, ttl)
        for (t, v), ttl in sorted(merged.items(), key=lambda kv: (kv[0][0].value, kv[0][1]))
    ]
    return entries, duplicates


def check_protected(config: FeedConfig, entries: Iterable[ParsedEntry]) -> None:
    protected = PROTECTED_DOMAINS | {d.lower().rstrip(".") for d in config.extra_protected_domains}
    for entry in entries:
        if entry.indicator_type is IndicatorType.DOMAIN and entry.value in protected:
            raise FeedError(FeedErrorCode.PROTECTED_DOMAIN)


# -- orchestration ------------------------------------------------------------


def _result_for(
    config: FeedConfig, status: RunStatus, code: FeedErrorCode, **kw: Any
) -> FeedRunResult:
    _log.warning(
        "mail_security.feed_run", source=config.source, status=status.value, code=code.value
    )
    return FeedRunResult(config.source, status, code, **kw)


async def ingest_feed(
    config: FeedConfig,
    fetcher: FeedFetcher,
    repo: Any,
    *,
    now: Callable[[], Any] | None = None,
    env: Mapping[str, str] | None = None,
    dry_run: bool = False,
    approved_feed_version: str | None = None,
    initial_load_approval_ref: str | None = None,
) -> FeedRunResult:
    """Fetch (via the injected fetcher), verify, parse, guard and promote one feed.

    Explicit call only. Never raises for feed problems: every failure is a
    typed :class:`FeedRunResult` and the previous live set is untouched.
    ``approved_feed_version`` is the owner's explicit approval of one exact
    feed version after reviewing a ``NEEDS_OWNER_REVIEW`` dry-run.
    ``initial_load_approval_ref`` is an opaque owner approval reference
    (validated like override approval refs) required when the source has no
    live indicators; without it the load is ``REJECTED``. ``now`` is the single
    clock for the run; when omitted the repository clock is used.
    """
    if not mail_security_enabled(dict(env) if env is not None else None):
        return FeedRunResult(config.source, RunStatus.REFUSED, FeedErrorCode.MASTER_FLAG_OFF)
    if not config.enabled:
        return FeedRunResult(config.source, RunStatus.REFUSED, FeedErrorCode.FEED_DISABLED)
    try:
        validate_config(config)
    except FeedError as exc:
        return _result_for(config, RunStatus.REFUSED, exc.code)

    try:
        fetched = await asyncio.wait_for(fetcher(config.source), timeout=config.fetch_timeout_s)
    except TimeoutError:
        return _result_for(config, RunStatus.FAILED, FeedErrorCode.FETCH_TIMEOUT)
    except Exception:  # noqa: BLE001 - fetchers are untrusted; never echo the error
        return _result_for(config, RunStatus.FAILED, FeedErrorCode.FETCH_FAILED)

    try:
        if not isinstance(fetched, FetchedFeed):
            raise FeedError(FeedErrorCode.FETCH_FAILED)
        if config.require_tls and not fetched.tls_verified:
            raise FeedError(FeedErrorCode.TRANSPORT_INSECURE)
        if len(fetched.payload) > config.max_payload_bytes:
            raise FeedError(FeedErrorCode.PAYLOAD_TOO_LARGE)
        verify_integrity(config, fetched.payload, fetched.signature_hex)
        entries, duplicates = parse_payload(config, fetched.payload)
        check_protected(config, entries)
    except FeedError as exc:
        status = RunStatus.FAILED if exc.code is FeedErrorCode.FETCH_FAILED else RunStatus.REJECTED
        return _result_for(config, status, exc.code)

    feed_version = "feed-" + hashlib.sha256(fetched.payload).hexdigest()[:32]
    clock_now = aware(now()) if now is not None else aware(repo.now())
    records = []
    try:
        for entry in entries:
            ttl = entry.ttl if entry.ttl is not None else config.ttl
            records.append(
                validate_record(
                    {
                        "indicator_type": entry.indicator_type.value,
                        "value": entry.value,
                        "source": config.source,
                        "confidence_type": config.confidence_type.value,
                        "license": config.license,
                        "provenance": f"feed:{config.source}:{feed_version}"[:200],
                        "feed_version": feed_version,
                        "first_seen": clock_now,
                        "last_seen": clock_now,
                        "expires_at": clock_now + ttl,
                    },
                    now=clock_now,
                )
            )
    except IntelError:
        return _result_for(
            config, RunStatus.REJECTED, FeedErrorCode.RECORD_INVALID, feed_version=feed_version
        )

    live_state = await repo.live_state(config.source, now=clock_now)
    live = set(live_state)
    incoming = {(r.indicator_type.value, r.canonical_indicator): r for r in records}
    added = len(set(incoming) - live)
    removed = len(live - set(incoming))
    expired_early = sum(
        1
        for key, (old_expiry, old_version) in live_state.items()
        if key in incoming
        and old_version != feed_version
        and incoming[key].expires_at < old_expiry
    )
    ratio = (added / len(live)) if live else 0.0
    removal_ratio = ((removed + expired_early) / len(live)) if live else 0.0
    counts: dict[str, Any] = {
        "feed_version": feed_version,
        "accepted": len(records),
        "duplicates": duplicates,
        "change_ratio": ratio,
        "removed": removed,
        "expired_early": expired_early,
        "removal_ratio": removal_ratio,
    }

    if not live and not is_valid_approval_ref(initial_load_approval_ref):
        return _result_for(
            config,
            RunStatus.REJECTED,
            FeedErrorCode.INITIAL_LOAD_APPROVAL_REQUIRED,
            feed_version=feed_version,
        )

    review_code: FeedErrorCode | None = None
    if live and ratio > config.max_change_ratio:
        review_code = FeedErrorCode.CHANGE_RATIO_EXCEEDED
    elif live and removal_ratio > config.max_removal_ratio:
        review_code = FeedErrorCode.REMOVAL_RATIO_EXCEEDED

    try:
        if review_code is not None and approved_feed_version != feed_version:
            plan = await repo.promote(records, dry_run=True, now=clock_now)
            return _result_for(
                config, RunStatus.NEEDS_OWNER_REVIEW, review_code, plan=plan, **counts
            )
        plan = await repo.promote(records, dry_run=dry_run, now=clock_now)
    except IntelError:
        return _result_for(
            config, RunStatus.REJECTED, FeedErrorCode.INGEST_REJECTED, feed_version=feed_version
        )

    _log.info("mail_security.feed_run", source=config.source, status="OK", accepted=len(records))
    return FeedRunResult(
        config.source,
        RunStatus.DRY_RUN if dry_run else RunStatus.INGESTED,
        None,
        feed_version=feed_version,
        accepted=len(records),
        duplicates=duplicates,
        change_ratio=ratio,
        removed=removed,
        expired_early=expired_early,
        removal_ratio=removal_ratio,
        plan=plan,
    )


__all__ = [
    "ALLOWED_LICENSES",
    "FEED_SCHEMA",
    "PROTECTED_DOMAINS",
    "FeedConfig",
    "FeedError",
    "FeedErrorCode",
    "FeedFetcher",
    "FeedFormat",
    "FeedRegistry",
    "FeedRunResult",
    "FetchedFeed",
    "IntegrityPin",
    "IntegrityScheme",
    "RunStatus",
    "ingest_feed",
    "parse_payload",
    "validate_config",
    "verify_integrity",
]
