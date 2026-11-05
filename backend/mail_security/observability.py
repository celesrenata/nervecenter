"""NerveShield observability: health, aggregates and alert evaluation (Task 2.6).

Extends the mechanisms the repo already has -- ``structlog`` for logs and the
``prometheus_client`` default registry used by :mod:`backend.observability` for
process-level metrics -- without adding a dependency. The repo has NO alerting /
notification mechanism and keeps NO Prometheus alert-rules file, so
:func:`evaluate_alerts` is a pure function only; delivery is an open owner item.

Everything here is a *read-only, deterministic* computation over repository
state with an injected clock, producing bounded, enum-keyed aggregates:

- queue depth by state, oldest-pending age, attempts distribution (bucketed);
- per-stage latency (numbers only, from :mod:`telemetry`);
- assessment source-fidelity and scan-completeness distributions;
- threat-intel feed freshness / version / live-indicator counts (feed id from
  config only);
- egress decision counts by fixed reason code;
- per-stage health (OK / DEGRADED / FAILING / DISABLED / UNKNOWN);
- typed alerts with safe codes.

Privacy (T-14, R-017.2, R-021.4, R-022.4): metric names, labels, health fields
and alerts carry ONLY fixed enums, numbers, a config feed id and a content-hash
feed version. Never an account / connection / message / source id, sender,
subject, URL, host, filename or free text. Every label is validated against its
enum before use (:func:`_label`), so a bug cannot create an unbounded or
sensitive label.

Failure semantics (I-02): a stage with no data is UNKNOWN (or DISABLED when the
feature is off), never OK by default.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from backend.mail_security.ingestion_seam import mail_security_enabled
from backend.mail_security.models import EvidenceFidelity
from backend.mail_security.queue import (
    JOB_STATE_DEAD_LETTER,
    JOB_STATE_DONE,
    JOB_STATE_ERROR_RETRYABLE,
    JOB_STATE_PENDING,
    JOB_STATE_RUNNING,
    QueuePolicy,
)
from backend.mail_security.telemetry import (
    LATENCY,
    LatencyRecorder,
    LatencyStats,
    sweeper_age_seconds,
)

# --------------------------------------------------------------------------- #
# fixed enums (the ONLY permitted metric label / key domains)
# --------------------------------------------------------------------------- #

#: Queue states reported (RUNNING == claimed / leased). Anything else -> OTHER.
QUEUE_STATES: tuple[str, ...] = (
    JOB_STATE_PENDING,
    JOB_STATE_RUNNING,
    JOB_STATE_ERROR_RETRYABLE,
    JOB_STATE_DEAD_LETTER,
    JOB_STATE_DONE,
)
STATE_OTHER = "OTHER"


class HealthStatus(StrEnum):
    OK = "OK"
    DEGRADED = "DEGRADED"
    FAILING = "FAILING"
    DISABLED = "DISABLED"
    UNKNOWN = "UNKNOWN"


class HealthStage(StrEnum):
    QUEUE = "QUEUE"
    EVIDENCE_FIDELITY = "EVIDENCE_FIDELITY"
    SCAN_COMPLETENESS = "SCAN_COMPLETENESS"
    INTEL_FEEDS = "INTEL_FEEDS"
    SWEEPER = "SWEEPER"
    EGRESS = "EGRESS"


class CompletenessStatus(StrEnum):
    COMPLETE = "COMPLETE"
    INSPECTION_INCOMPLETE = "INSPECTION_INCOMPLETE"
    ATTACHMENT_SCAN_INCOMPLETE = "ATTACHMENT_SCAN_INCOMPLETE"
    NOT_RUN = "NOT_RUN"


class AttemptBucket(StrEnum):
    ZERO = "0"
    ONE = "1"
    TWO = "2"
    THREE_FOUR = "3_4"
    FIVE_PLUS = "5_PLUS"


class AlertCode(StrEnum):
    STUCK_QUEUE = "STUCK_QUEUE"
    DEAD_LETTER_PRESENT = "DEAD_LETTER_PRESENT"
    STALE_FEED = "STALE_FEED"
    SCAN_COMPLETENESS_REGRESSION = "SCAN_COMPLETENESS_REGRESSION"
    SWEEPER_SILENT = "SWEEPER_SILENT"


class AlertSeverity(StrEnum):
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


#: Safe reason codes attached to a stage health (never free text).
class HealthReason(StrEnum):
    MODULE_DISABLED = "MODULE_DISABLED"
    NO_DATA = "NO_DATA"
    DEAD_LETTER_PRESENT = "DEAD_LETTER_PRESENT"
    DEAD_LETTER_MANY = "DEAD_LETTER_MANY"
    QUEUE_OLDEST_PENDING_STALE = "QUEUE_OLDEST_PENDING_STALE"
    QUEUE_OLDEST_PENDING_CRITICAL = "QUEUE_OLDEST_PENDING_CRITICAL"
    ATTEMPT_CEILING_REACHED = "ATTEMPT_CEILING_REACHED"
    FIDELITY_LOW = "FIDELITY_LOW"
    COMPLETENESS_REGRESSION = "COMPLETENESS_REGRESSION"
    COMPLETENESS_FAILING = "COMPLETENESS_FAILING"
    FEED_STALE = "FEED_STALE"
    FEED_VERY_STALE = "FEED_VERY_STALE"
    FEED_NO_LIVE_INDICATORS = "FEED_NO_LIVE_INDICATORS"
    SWEEPER_NEVER_RUN = "SWEEPER_NEVER_RUN"
    SWEEPER_SILENT = "SWEEPER_SILENT"
    SWEEPER_VERY_SILENT = "SWEEPER_VERY_SILENT"
    EGRESS_FAILURES = "EGRESS_FAILURES"
    SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"


_FIDELITIES: tuple[str, ...] = tuple(f.value for f in EvidenceFidelity)
_LOW_FIDELITY = frozenset(
    {EvidenceFidelity.METADATA_ONLY.value, EvidenceFidelity.UNKNOWN.value}
)
_INCOMPLETE_STATUSES = frozenset(
    {
        CompletenessStatus.INSPECTION_INCOMPLETE.value,
        CompletenessStatus.ATTACHMENT_SCAN_INCOMPLETE.value,
        CompletenessStatus.NOT_RUN.value,
    }
)

#: Rule id of the typed "scanner did not finish" finding (attachments.py).
_RULE_ATTACHMENT_SCAN_INCOMPLETE = "ATTACHMENT_SCAN_INCOMPLETE"
_RULE_INSPECTION_INCOMPLETE = "mime.inspection.incomplete"
_REASON_INSPECTION_INCOMPLETE = "INSPECTION_INCOMPLETE"

FEED_OTHER = "OTHER"
MAX_FEEDS = 16
_FEED_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
EGRESS_OTHER = "OTHER"
#: Egress reason codes are fixed upper-snake enum values (``EgressReason``).
#: ``egress.py`` is deliberately not imported here (its wiring-guard test keeps
#: it unreferenced), so reasons are validated by shape and cardinality instead.
_REASON_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,39}$")
MAX_EGRESS_REASONS = 64


def _label(value: object, allowed: Iterable[str], fallback: str) -> str:
    """Validate a metric label against its fixed domain (privacy + cardinality).

    Anything outside ``allowed`` collapses to ``fallback``; the original value
    is never emitted.
    """
    text = value.value if isinstance(value, StrEnum) else value
    return text if isinstance(text, str) and text in set(allowed) else fallback


# --------------------------------------------------------------------------- #
# thresholds (documented defaults; injected; NOT tuned on real load)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class HealthThresholds:
    """Health / alert thresholds. Defaults are conservative starting points."""

    #: Oldest PENDING job older than this -> DEGRADED / STUCK_QUEUE (15 min).
    stuck_pending_seconds: float = 900.0
    #: ... older than this -> FAILING / CRITICAL (1 h).
    stuck_pending_failing_seconds: float = 3600.0
    #: DEAD_LETTER count >= this -> DEGRADED (any dead letter is visible).
    dead_letter_degraded: int = 1
    #: DEAD_LETTER count >= this -> FAILING.
    dead_letter_failing: int = 10
    #: Jobs (non-terminal) that reached the attempt ceiling >= this -> DEGRADED.
    attempt_ceiling_degraded: int = 1
    #: Feed older than this (last_seen) -> DEGRADED / STALE_FEED (2 days).
    feed_stale_seconds: float = 172_800.0
    #: ... older than this -> FAILING (6 days).
    feed_failing_seconds: float = 518_400.0
    #: Sweeper silent longer than this -> DEGRADED / SWEEPER_SILENT (10 min).
    sweeper_silent_seconds: float = 600.0
    #: ... longer than this -> FAILING (30 min).
    sweeper_failing_seconds: float = 1800.0
    #: Incomplete-assessment share needed to judge a regression.
    completeness_min_sample: int = 5
    #: Incomplete share >= this -> DEGRADED / regression alert.
    completeness_degraded_ratio: float = 0.25
    #: Incomplete share >= this -> FAILING.
    completeness_failing_ratio: float = 0.5
    #: Low-fidelity (METADATA_ONLY/UNKNOWN) share >= this -> DEGRADED.
    fidelity_low_ratio: float = 0.5
    #: Egress "fail" decisions >= this -> DEGRADED.
    egress_failures_degraded: int = 5


# --------------------------------------------------------------------------- #
# snapshot model (bounded, enum-keyed; no identifiers)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class QueueSnapshot:
    depth: Mapping[str, int]
    oldest_pending_age_seconds: float | None
    attempts: Mapping[str, int]
    at_ceiling: int
    total: int


@dataclass(frozen=True)
class FeedStats:
    feed: str  # config feed id, validated, or "OTHER"
    feed_version: str | None
    live_indicators: int
    total_indicators: int
    age_seconds: float | None  # since the freshest last_seen


@dataclass(frozen=True)
class ObservabilitySnapshot:
    captured_at: datetime
    enabled: bool
    queue: QueueSnapshot
    fidelity: Mapping[str, int]
    completeness: Mapping[str, int]
    assessments_total: int
    #: ``None`` => intel could not be read (UNKNOWN); ``()`` => no feed data.
    feeds: tuple[FeedStats, ...] | None
    #: ``None`` => no egress audit supplied (UNKNOWN).
    egress_reasons: Mapping[str, int] | None
    egress_decisions: Mapping[str, int] | None
    latency: Mapping[str, LatencyStats]
    sweeper_age_seconds: float | None


@dataclass(frozen=True)
class StageHealth:
    stage: str
    status: HealthStatus
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class Alert:
    code: AlertCode
    stage: HealthStage
    severity: AlertSeverity
    observed: float
    threshold: float


# --------------------------------------------------------------------------- #
# pure builders
# --------------------------------------------------------------------------- #


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def attempt_bucket(attempts: int) -> AttemptBucket:
    """Bucket a non-negative attempt count into a fixed enum."""
    n = max(0, int(attempts or 0))
    if n == 0:
        return AttemptBucket.ZERO
    if n == 1:
        return AttemptBucket.ONE
    if n == 2:
        return AttemptBucket.TWO
    if n <= 4:
        return AttemptBucket.THREE_FOUR
    return AttemptBucket.FIVE_PLUS


def build_queue_snapshot(
    jobs: Iterable[Any], *, now: datetime, policy: QueuePolicy | None = None
) -> QueueSnapshot:
    """Aggregate job rows into depth / oldest-pending age / attempt buckets.

    Reads only ``state``, ``attempts``, ``created_at`` (and the lease fields);
    no identifier, key or error text is touched.
    """
    pol = policy or QueuePolicy()
    now = _aware(now) or datetime.now(tz=UTC)
    depth: Counter[str] = Counter({s: 0 for s in QUEUE_STATES})
    buckets: Counter[str] = Counter({b.value: 0 for b in AttemptBucket})
    oldest: float | None = None
    at_ceiling = 0
    total = 0
    for job in jobs:
        total += 1
        state = _label(getattr(job, "state", None), QUEUE_STATES, STATE_OTHER)
        depth[state] += 1
        attempts = int(getattr(job, "attempts", 0) or 0)
        buckets[attempt_bucket(attempts).value] += 1
        if state in (JOB_STATE_ERROR_RETRYABLE, JOB_STATE_RUNNING) and (
            attempts >= pol.max_attempts
        ):
            at_ceiling += 1
        if state == JOB_STATE_PENDING:
            created = _aware(getattr(job, "created_at", None))
            if created is not None:
                age = max(0.0, (now - created).total_seconds())
                oldest = age if oldest is None else max(oldest, age)
    return QueueSnapshot(
        depth=dict(depth),
        oldest_pending_age_seconds=oldest,
        attempts=dict(buckets),
        at_ceiling=at_ceiling,
        total=total,
    )


def classify_completeness(row: Any) -> CompletenessStatus:
    """Classify one stored assessment row's scan completeness (pure).

    Precedence (documented): NOT_RUN (no evidence was ever scanned: INCOMPLETE
    with UNKNOWN fidelity) > ATTACHMENT_SCAN_INCOMPLETE > INSPECTION_INCOMPLETE
    > COMPLETE. Reads only enum-like fields of the stored metadata payload.
    """
    state = getattr(row, "processing_state", None)
    fidelity = getattr(row, "evidence_fidelity", None)
    payload = getattr(row, "assessment", None) or {}
    findings = payload.get("findings") or []
    rule_ids = {f.get("rule_id") for f in findings if isinstance(f, dict)}
    policy = payload.get("policy") or {}
    reasons = set(policy.get("reason_codes") or [])
    incomplete_state = state == "INCOMPLETE"
    if incomplete_state and fidelity == EvidenceFidelity.UNKNOWN.value:
        return CompletenessStatus.NOT_RUN
    if _RULE_ATTACHMENT_SCAN_INCOMPLETE in rule_ids:
        return CompletenessStatus.ATTACHMENT_SCAN_INCOMPLETE
    if (
        incomplete_state
        or _REASON_INSPECTION_INCOMPLETE in reasons
        or _RULE_INSPECTION_INCOMPLETE in rule_ids
    ):
        return CompletenessStatus.INSPECTION_INCOMPLETE
    return CompletenessStatus.COMPLETE


def build_assessment_distributions(
    rows: Iterable[Any],
) -> tuple[dict[str, int], dict[str, int], int]:
    """Return (fidelity counts, completeness counts, total) for assessments."""
    fidelity: Counter[str] = Counter({f: 0 for f in _FIDELITIES})
    completeness: Counter[str] = Counter({c.value: 0 for c in CompletenessStatus})
    total = 0
    for row in rows:
        total += 1
        fidelity[
            _label(
                getattr(row, "evidence_fidelity", None),
                _FIDELITIES,
                EvidenceFidelity.UNKNOWN.value,
            )
        ] += 1
        completeness[classify_completeness(row).value] += 1
    return dict(fidelity), dict(completeness), total


def build_egress_counts(
    records: Iterable[Any] | None,
) -> tuple[dict[str, int], dict[str, int]] | None:
    """Count egress audit records by fixed reason code and decision.

    Only the ``reason`` / ``decision`` enum-shaped fields are read -- never the host
    hash, purpose text, timestamp or account scope on the record.
    """
    if records is None:
        return None
    decisions_allowed = ("allow", "deny", "fail")
    reasons: Counter[str] = Counter()
    decisions: Counter[str] = Counter()
    for rec in records:
        raw = getattr(rec, "reason", None)
        raw = raw.value if isinstance(raw, StrEnum) else raw
        reason = EGRESS_OTHER
        if isinstance(raw, str) and _REASON_CODE.match(raw):
            if raw in reasons or len(reasons) < MAX_EGRESS_REASONS:
                reason = raw
        reasons[reason] += 1
        decisions[_label(getattr(rec, "decision", None), decisions_allowed, EGRESS_OTHER)] += 1
    return dict(reasons), dict(decisions)


def build_feed_stats(rows: Iterable[Any], *, now: datetime) -> tuple[FeedStats, ...]:
    """Aggregate intel indicator rows per config feed id (bounded to MAX_FEEDS)."""
    from backend.mail_security.intel import is_live

    now = _aware(now) or datetime.now(tz=UTC)
    acc: dict[str, dict[str, Any]] = {}
    for row in rows:
        source = getattr(row, "source", None)
        feed = source if isinstance(source, str) and _FEED_ID.match(source) else FEED_OTHER
        if feed not in acc and len(acc) >= MAX_FEEDS:
            feed = FEED_OTHER
        slot = acc.setdefault(
            feed, {"live": 0, "total": 0, "seen": None, "version": None}
        )
        slot["total"] += 1
        expires = _aware(getattr(row, "expires_at", None))
        if expires is not None and is_live(
            enabled=bool(getattr(row, "enabled", True)),
            rolled_back=bool(getattr(row, "rolled_back", False)),
            superseded=getattr(row, "superseded_in_version", None) is not None,
            expires_at=expires,
            now=now,
        ):
            slot["live"] += 1
        seen = _aware(getattr(row, "last_seen", None))
        if seen is not None and (slot["seen"] is None or seen > slot["seen"]):
            slot["seen"] = seen
            version = getattr(row, "feed_version", None)
            slot["version"] = version[:64] if isinstance(version, str) else None
    return tuple(
        FeedStats(
            feed=feed,
            feed_version=slot["version"],
            live_indicators=slot["live"],
            total_indicators=slot["total"],
            age_seconds=(
                max(0.0, (now - slot["seen"]).total_seconds())
                if slot["seen"] is not None
                else None
            ),
        )
        for feed, slot in sorted(acc.items())
    )


def build_snapshot(
    *,
    now: datetime,
    jobs: Iterable[Any],
    assessments: Iterable[Any],
    feed_rows: Iterable[Any] | None = None,
    egress_records: Iterable[Any] | None = None,
    policy: QueuePolicy | None = None,
    latency: LatencyRecorder | None = None,
    sweeper_age: float | None = None,
    enabled: bool = True,
) -> ObservabilitySnapshot:
    """Assemble a snapshot from already-loaded rows (pure; injected clock)."""
    now = _aware(now) or datetime.now(tz=UTC)
    fidelity, completeness, total = build_assessment_distributions(assessments)
    egress = build_egress_counts(egress_records)
    return ObservabilitySnapshot(
        captured_at=now,
        enabled=enabled,
        queue=build_queue_snapshot(jobs, now=now, policy=policy),
        fidelity=fidelity,
        completeness=completeness,
        assessments_total=total,
        feeds=build_feed_stats(feed_rows, now=now) if feed_rows is not None else None,
        egress_reasons=egress[0] if egress is not None else None,
        egress_decisions=egress[1] if egress is not None else None,
        latency=(latency if latency is not None else LATENCY).snapshot(),
        sweeper_age_seconds=sweeper_age,
    )


def disabled_snapshot(now: datetime) -> ObservabilitySnapshot:
    """The snapshot reported while the feature flag is OFF (no reads happened)."""
    return build_snapshot(
        now=now, jobs=(), assessments=(), latency=LatencyRecorder(), enabled=False
    )


# --------------------------------------------------------------------------- #
# async collection (read-only repository access)
# --------------------------------------------------------------------------- #


async def collect_feed_rows(session_factory: Any) -> list[Any] | None:
    """Read intel indicator rows, or ``None`` if the store is unreadable."""
    from sqlalchemy import select

    from backend.persistence.models.mail_security import MailIntelIndicatorRow

    try:
        async with session_factory() as session:
            result = await session.execute(select(MailIntelIndicatorRow))
            return list(result.scalars().all())
    except Exception:  # noqa: BLE001 - unreadable intel => UNKNOWN, never OK
        return None


async def collect_snapshot(
    repository: Any,
    account_scopes: Iterable[str],
    *,
    now: datetime,
    policy: QueuePolicy | None = None,
    feed_rows: Iterable[Any] | None = None,
    egress_records: Iterable[Any] | None = None,
    latency: LatencyRecorder | None = None,
    env: dict[str, str] | None = None,
) -> ObservabilitySnapshot:
    """Collect a snapshot for the given (already authorized) account scopes.

    Flag OFF => returns :func:`disabled_snapshot` after doing NO repository
    reads. The scopes are used only to query; they never appear in the result.
    """
    if not mail_security_enabled(env):
        return disabled_snapshot(now)
    jobs: list[Any] = []
    assessments: list[Any] = []
    if repository is not None:
        for scope in account_scopes:
            jobs.extend(await repository.list_jobs(scope))
            assessments.extend(await repository.list_assessments(scope))
    return build_snapshot(
        now=now,
        jobs=jobs,
        assessments=assessments,
        feed_rows=feed_rows,
        egress_records=egress_records,
        policy=policy,
        latency=latency,
        sweeper_age=sweeper_age_seconds(now),
        enabled=True,
    )


# --------------------------------------------------------------------------- #
# health
# --------------------------------------------------------------------------- #


def _worst(*statuses: HealthStatus) -> HealthStatus:
    order = (HealthStatus.FAILING, HealthStatus.DEGRADED, HealthStatus.OK)
    for candidate in order:
        if candidate in statuses:
            return candidate
    return HealthStatus.OK


def _queue_health(snap: ObservabilitySnapshot, th: HealthThresholds) -> StageHealth:
    q = snap.queue
    if q.total == 0:
        return StageHealth(HealthStage.QUEUE, HealthStatus.UNKNOWN, (HealthReason.NO_DATA,))
    status = HealthStatus.OK
    reasons: list[str] = []
    dead = q.depth.get(JOB_STATE_DEAD_LETTER, 0)
    if dead >= th.dead_letter_failing:
        status = HealthStatus.FAILING
        reasons.append(HealthReason.DEAD_LETTER_MANY)
    elif dead >= th.dead_letter_degraded:
        status = _worst(status, HealthStatus.DEGRADED)
        reasons.append(HealthReason.DEAD_LETTER_PRESENT)
    age = q.oldest_pending_age_seconds
    if age is not None and age >= th.stuck_pending_failing_seconds:
        status = HealthStatus.FAILING
        reasons.append(HealthReason.QUEUE_OLDEST_PENDING_CRITICAL)
    elif age is not None and age >= th.stuck_pending_seconds:
        status = _worst(status, HealthStatus.DEGRADED)
        reasons.append(HealthReason.QUEUE_OLDEST_PENDING_STALE)
    if q.at_ceiling >= th.attempt_ceiling_degraded:
        status = _worst(status, HealthStatus.DEGRADED)
        reasons.append(HealthReason.ATTEMPT_CEILING_REACHED)
    return StageHealth(HealthStage.QUEUE, status, tuple(reasons))


def _incomplete_ratio(snap: ObservabilitySnapshot) -> float:
    if snap.assessments_total <= 0:
        return 0.0
    bad = sum(snap.completeness.get(c, 0) for c in _INCOMPLETE_STATUSES)
    return bad / snap.assessments_total


def _completeness_health(snap: ObservabilitySnapshot, th: HealthThresholds) -> StageHealth:
    stage = HealthStage.SCAN_COMPLETENESS
    if snap.assessments_total == 0:
        return StageHealth(stage, HealthStatus.UNKNOWN, (HealthReason.NO_DATA,))
    if snap.assessments_total >= th.completeness_min_sample:
        ratio = _incomplete_ratio(snap)
        if ratio >= th.completeness_failing_ratio:
            return StageHealth(
                stage, HealthStatus.FAILING, (HealthReason.COMPLETENESS_FAILING,)
            )
        if ratio >= th.completeness_degraded_ratio:
            return StageHealth(
                stage, HealthStatus.DEGRADED, (HealthReason.COMPLETENESS_REGRESSION,)
            )
    return StageHealth(stage, HealthStatus.OK)


def _fidelity_health(snap: ObservabilitySnapshot, th: HealthThresholds) -> StageHealth:
    stage = HealthStage.EVIDENCE_FIDELITY
    if snap.assessments_total == 0:
        return StageHealth(stage, HealthStatus.UNKNOWN, (HealthReason.NO_DATA,))
    low = sum(snap.fidelity.get(f, 0) for f in _LOW_FIDELITY)
    if (
        snap.assessments_total >= th.completeness_min_sample
        and low / snap.assessments_total >= th.fidelity_low_ratio
    ):
        return StageHealth(stage, HealthStatus.DEGRADED, (HealthReason.FIDELITY_LOW,))
    return StageHealth(stage, HealthStatus.OK)


def _feeds_health(snap: ObservabilitySnapshot, th: HealthThresholds) -> StageHealth:
    stage = HealthStage.INTEL_FEEDS
    if snap.feeds is None:
        return StageHealth(stage, HealthStatus.UNKNOWN, (HealthReason.SOURCE_UNAVAILABLE,))
    if not snap.feeds:
        # Feeds are disabled by default: no data is DISABLED, never OK.
        return StageHealth(stage, HealthStatus.DISABLED, (HealthReason.NO_DATA,))
    status = HealthStatus.OK
    reasons: list[str] = []
    for feed in snap.feeds:
        if feed.age_seconds is None or feed.age_seconds >= th.feed_failing_seconds:
            status = HealthStatus.FAILING
            reasons.append(HealthReason.FEED_VERY_STALE)
        elif feed.age_seconds >= th.feed_stale_seconds:
            status = _worst(status, HealthStatus.DEGRADED)
            reasons.append(HealthReason.FEED_STALE)
        if feed.live_indicators == 0:
            status = _worst(status, HealthStatus.DEGRADED)
            reasons.append(HealthReason.FEED_NO_LIVE_INDICATORS)
    return StageHealth(stage, status, tuple(dict.fromkeys(reasons)))


def _sweeper_health(snap: ObservabilitySnapshot, th: HealthThresholds) -> StageHealth:
    stage = HealthStage.SWEEPER
    age = snap.sweeper_age_seconds
    if age is None:
        return StageHealth(stage, HealthStatus.UNKNOWN, (HealthReason.SWEEPER_NEVER_RUN,))
    if age >= th.sweeper_failing_seconds:
        return StageHealth(
            stage, HealthStatus.FAILING, (HealthReason.SWEEPER_VERY_SILENT,)
        )
    if age >= th.sweeper_silent_seconds:
        return StageHealth(stage, HealthStatus.DEGRADED, (HealthReason.SWEEPER_SILENT,))
    return StageHealth(stage, HealthStatus.OK)


def _egress_health(snap: ObservabilitySnapshot, th: HealthThresholds) -> StageHealth:
    stage = HealthStage.EGRESS
    if snap.egress_decisions is None:
        return StageHealth(stage, HealthStatus.UNKNOWN, (HealthReason.NO_DATA,))
    if sum(snap.egress_decisions.values()) == 0:
        # Egress is disabled by default; no decisions is DISABLED, not OK.
        return StageHealth(stage, HealthStatus.DISABLED, (HealthReason.NO_DATA,))
    if snap.egress_decisions.get("fail", 0) >= th.egress_failures_degraded:
        return StageHealth(
            stage, HealthStatus.DEGRADED, (HealthReason.EGRESS_FAILURES,)
        )
    return StageHealth(stage, HealthStatus.OK)


def compute_health(
    snapshot: ObservabilitySnapshot, thresholds: HealthThresholds | None = None
) -> tuple[StageHealth, ...]:
    """Per-stage health. Flag OFF => every stage DISABLED. No data => never OK."""
    th = thresholds or HealthThresholds()
    if not snapshot.enabled:
        return tuple(
            StageHealth(s, HealthStatus.DISABLED, (HealthReason.MODULE_DISABLED,))
            for s in HealthStage
        )
    return (
        _queue_health(snapshot, th),
        _fidelity_health(snapshot, th),
        _completeness_health(snapshot, th),
        _feeds_health(snapshot, th),
        _sweeper_health(snapshot, th),
        _egress_health(snapshot, th),
    )


# --------------------------------------------------------------------------- #
# alerts (pure; delivery mechanism does NOT exist in this repo)
# --------------------------------------------------------------------------- #


def evaluate_alerts(
    snapshot: ObservabilitySnapshot, thresholds: HealthThresholds | None = None
) -> list[Alert]:
    """Evaluate typed alerts from a snapshot. Pure and deterministic.

    A disabled snapshot fires nothing. Delivery is intentionally out of scope:
    the repo has no alerting / notification mechanism (see module docstring).
    """
    th = thresholds or HealthThresholds()
    if not snapshot.enabled:
        return []
    alerts: list[Alert] = []
    q = snapshot.queue

    age = q.oldest_pending_age_seconds
    if age is not None and age >= th.stuck_pending_seconds:
        crit = age >= th.stuck_pending_failing_seconds
        alerts.append(
            Alert(
                AlertCode.STUCK_QUEUE,
                HealthStage.QUEUE,
                AlertSeverity.CRITICAL if crit else AlertSeverity.WARNING,
                float(age),
                th.stuck_pending_failing_seconds if crit else th.stuck_pending_seconds,
            )
        )

    dead = q.depth.get(JOB_STATE_DEAD_LETTER, 0)
    if dead >= max(1, th.dead_letter_degraded):
        alerts.append(
            Alert(
                AlertCode.DEAD_LETTER_PRESENT,
                HealthStage.QUEUE,
                AlertSeverity.CRITICAL
                if dead >= th.dead_letter_failing
                else AlertSeverity.WARNING,
                float(dead),
                float(th.dead_letter_degraded),
            )
        )

    for feed in snapshot.feeds or ():
        if (
            feed.age_seconds is None
            or feed.age_seconds >= th.feed_stale_seconds
            or feed.live_indicators == 0
        ):
            very = feed.age_seconds is None or feed.age_seconds >= th.feed_failing_seconds
            alerts.append(
                Alert(
                    AlertCode.STALE_FEED,
                    HealthStage.INTEL_FEEDS,
                    AlertSeverity.CRITICAL if very else AlertSeverity.WARNING,
                    float(feed.age_seconds) if feed.age_seconds is not None else -1.0,
                    th.feed_stale_seconds,
                )
            )
            break  # one alert per code; per-feed detail lives in the snapshot

    if snapshot.assessments_total >= th.completeness_min_sample:
        ratio = _incomplete_ratio(snapshot)
        if ratio >= th.completeness_degraded_ratio:
            alerts.append(
                Alert(
                    AlertCode.SCAN_COMPLETENESS_REGRESSION,
                    HealthStage.SCAN_COMPLETENESS,
                    AlertSeverity.CRITICAL
                    if ratio >= th.completeness_failing_ratio
                    else AlertSeverity.WARNING,
                    float(ratio),
                    th.completeness_degraded_ratio,
                )
            )

    sweep = snapshot.sweeper_age_seconds
    if sweep is not None and sweep >= th.sweeper_silent_seconds:
        alerts.append(
            Alert(
                AlertCode.SWEEPER_SILENT,
                HealthStage.SWEEPER,
                AlertSeverity.CRITICAL
                if sweep >= th.sweeper_failing_seconds
                else AlertSeverity.WARNING,
                float(sweep),
                th.sweeper_silent_seconds,
            )
        )
    elif sweep is None and q.total > 0:
        # Work exists but the sweeper has never reported a run.
        alerts.append(
            Alert(
                AlertCode.SWEEPER_SILENT,
                HealthStage.SWEEPER,
                AlertSeverity.WARNING,
                -1.0,
                th.sweeper_silent_seconds,
            )
        )
    return alerts


# --------------------------------------------------------------------------- #
# redacted dict view (explicit allow-list; used by the API and tests)
# --------------------------------------------------------------------------- #


def health_view(
    snapshot: ObservabilitySnapshot,
    thresholds: HealthThresholds | None = None,
) -> dict[str, Any]:
    """The allow-listed, identifier-free health summary."""
    th = thresholds or HealthThresholds()
    q = snapshot.queue
    return {
        "captured_at": snapshot.captured_at.isoformat(),
        "enabled": snapshot.enabled,
        "stages": [
            {
                "stage": str(h.stage),
                "status": h.status.value,
                "reasons": [str(r) for r in h.reasons],
            }
            for h in compute_health(snapshot, th)
        ],
        "queue": {
            "depth": dict(q.depth),
            "oldest_pending_age_seconds": q.oldest_pending_age_seconds,
            "attempts": dict(q.attempts),
            "at_attempt_ceiling": q.at_ceiling,
        },
        "fidelity": dict(snapshot.fidelity),
        "completeness": dict(snapshot.completeness),
        "intel_feeds": [
            {
                "feed": f.feed,
                "feed_version": f.feed_version,
                "live_indicators": f.live_indicators,
                "age_seconds": f.age_seconds,
            }
            for f in (snapshot.feeds or ())
        ],
        "egress_reasons": dict(snapshot.egress_reasons or {}),
        "latency": {
            stage: {
                "count": s.count,
                "sum_seconds": s.sum_seconds,
                "max_seconds": s.max_seconds,
            }
            for stage, s in snapshot.latency.items()
        },
        "sweeper_age_seconds": snapshot.sweeper_age_seconds,
        "alerts": [
            {"code": a.code.value, "stage": a.stage.value, "severity": a.severity.value}
            for a in evaluate_alerts(snapshot, th)
        ],
    }


# --------------------------------------------------------------------------- #
# Prometheus publication (existing mechanism: prometheus_client registry)
# --------------------------------------------------------------------------- #

_PROM_GAUGES: dict[int, dict[str, Any]] = {}


def _gauges(registry: Any) -> dict[str, Any]:
    from prometheus_client import REGISTRY, Gauge

    reg = registry if registry is not None else REGISTRY
    cached = _PROM_GAUGES.get(id(reg))
    if cached is not None and cached.get("_reg") is reg:
        return cached
    p = "nervcenter_mail_security_"
    g: dict[str, Any] = {
        "_reg": reg,
        "depth": Gauge(p + "queue_depth", "Jobs by state", ["state"], registry=reg),
        "oldest": Gauge(p + "oldest_pending_age_seconds", "Oldest pending age", registry=reg),
        "attempts": Gauge(p + "job_attempts", "Jobs by attempt bucket", ["bucket"], registry=reg),
        "fidelity": Gauge(
            p + "assessments_by_fidelity", "Assessments by fidelity", ["fidelity"], registry=reg,
        ),
        "completeness": Gauge(
            p + "assessments_by_completeness",
            "Assessments by completeness",
            ["status"],
            registry=reg,
        ),
        "feed_live": Gauge(
            p + "feed_live_indicators", "Live indicators per feed", ["feed"], registry=reg,
        ),
        "feed_age": Gauge(p + "feed_age_seconds", "Feed freshness", ["feed"], registry=reg),
        "egress": Gauge(
            p + "egress_decisions", "Egress decisions by reason", ["reason"], registry=reg,
        ),
        "lat_count": Gauge(p + "stage_latency_count", "Latency samples", ["stage"], registry=reg),
        "lat_sum": Gauge(p + "stage_latency_sum_seconds", "Latency sum", ["stage"], registry=reg),
        "health": Gauge(
            p + "stage_health", "1-hot stage health", ["stage", "status"], registry=reg,
        ),
        "alert": Gauge(p + "alert_active", "Active alert (1/0)", ["code"], registry=reg),
    }
    _PROM_GAUGES[id(reg)] = g
    return g


def publish_metrics(
    snapshot: ObservabilitySnapshot,
    thresholds: HealthThresholds | None = None,
    *,
    registry: Any = None,
) -> bool:
    """Publish a (global, unscoped) snapshot to the prometheus_client registry.

    No-op returning ``False`` while the flag is OFF (snapshot disabled). Every
    label is validated against its fixed enum; there is no account dimension.
    """
    if not snapshot.enabled:
        return False
    g = _gauges(registry)
    for state in (*QUEUE_STATES, STATE_OTHER):
        g["depth"].labels(_label(state, (*QUEUE_STATES, STATE_OTHER), STATE_OTHER)).set(
            snapshot.queue.depth.get(state, 0)
        )
    g["oldest"].set(snapshot.queue.oldest_pending_age_seconds or 0.0)
    for b in AttemptBucket:
        g["attempts"].labels(b.value).set(snapshot.queue.attempts.get(b.value, 0))
    for f in _FIDELITIES:
        g["fidelity"].labels(f).set(snapshot.fidelity.get(f, 0))
    for c in CompletenessStatus:
        g["completeness"].labels(c.value).set(snapshot.completeness.get(c.value, 0))
    for feed in snapshot.feeds or ():
        name = feed.feed if _FEED_ID.match(feed.feed) else FEED_OTHER
        g["feed_live"].labels(name).set(feed.live_indicators)
        g["feed_age"].labels(name).set(feed.age_seconds if feed.age_seconds is not None else -1.0)
    for reason, n in (snapshot.egress_reasons or {}).items():
        g["egress"].labels(reason).set(n)
    for stage, s in snapshot.latency.items():
        g["lat_count"].labels(stage).set(s.count)
        g["lat_sum"].labels(stage).set(s.sum_seconds)
    current = {h.stage: h.status for h in compute_health(snapshot, thresholds)}
    for stage in HealthStage:
        for status in HealthStatus:
            g["health"].labels(stage.value, status.value).set(
                1 if current.get(stage) == status else 0
            )
    firing = {a.code for a in evaluate_alerts(snapshot, thresholds)}
    for code in AlertCode:
        g["alert"].labels(code.value).set(1 if code in firing else 0)
    return True


__all__ = [
    "Alert",
    "AlertCode",
    "AlertSeverity",
    "AttemptBucket",
    "CompletenessStatus",
    "FeedStats",
    "HealthReason",
    "HealthStage",
    "HealthStatus",
    "HealthThresholds",
    "ObservabilitySnapshot",
    "QueueSnapshot",
    "StageHealth",
    "attempt_bucket",
    "build_assessment_distributions",
    "build_egress_counts",
    "build_feed_stats",
    "build_queue_snapshot",
    "build_snapshot",
    "classify_completeness",
    "collect_feed_rows",
    "collect_snapshot",
    "compute_health",
    "disabled_snapshot",
    "evaluate_alerts",
    "health_view",
    "publish_metrics",
]
