"""NerveShield telemetry primitives (Phase 2, Task 2.6).

A dependency-free *leaf* module (imports only the flag helper) so the queue,
worker and observability layers can all use it without import cycles.

- :func:`safe_trace_id` -- a deterministic, opaque correlation id for log lines.
- :class:`LatencyRecorder` -- bounded, enum-keyed, numbers-only stage latency.
- :func:`note_sweep` / :func:`sweeper_age_seconds` -- a sweeper heartbeat
  (one timestamp; nothing else).

Privacy (T-14, R-017.2, R-021.4): only fixed enums and numbers are ever stored
or emitted here. No account id, connection id, message id, sender, subject, URL,
host, filename or free text can enter any structure in this module.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from backend.mail_security.ingestion_seam import mail_security_enabled

# --------------------------------------------------------------------------- #
# safe trace id
# --------------------------------------------------------------------------- #

#: Domain-separation tag. It is a fixed, public constant, NOT a secret salt:
#: the scheme is salt-free by design so the same job/assessment id yields the
#: same trace id in every process and every stage (cross-stage correlation).
_TRACE_DOMAIN = b"nervshield.trace.v1\x00"

#: Prefix + 24 hex chars (96 bits of the SHA-256 digest).
TRACE_ID_PREFIX = "tr_"
_TRACE_HEX_LEN = 24
NO_TRACE_ID = "tr_none"


def safe_trace_id(identifier: object) -> str:
    """Return an opaque, deterministic trace id for a job/assessment id.

    ``tr_`` + the first 24 hex chars of ``SHA-256(domain || identifier)``. A
    one-way digest truncated to 96 bits: it contains no substring of the input
    and cannot be reversed to message or account data. The inputs used by the
    pipeline are already opaque row ids (random UUID job ids / deterministic
    hashed assessment ids), never a message-id, address or subject. An empty or
    non-string input maps to the constant :data:`NO_TRACE_ID`.
    """
    if not isinstance(identifier, str) or not identifier:
        return NO_TRACE_ID
    digest = hashlib.sha256(_TRACE_DOMAIN + identifier.encode("utf-8")).hexdigest()
    return TRACE_ID_PREFIX + digest[:_TRACE_HEX_LEN]


# --------------------------------------------------------------------------- #
# per-stage latency (numbers only)
# --------------------------------------------------------------------------- #


class LatencyStage(StrEnum):
    """Fixed worker stages. The only permitted latency keys."""

    RESOLVE = "RESOLVE"
    SCAN = "SCAN"
    ASSESS = "ASSESS"
    PERSIST = "PERSIST"
    TOTAL = "TOTAL"


#: Fixed bucket upper bounds in seconds (le-style, plus an implicit +Inf).
LATENCY_BUCKETS: tuple[float, ...] = (0.01, 0.05, 0.25, 1.0, 5.0, 30.0)


@dataclass(frozen=True)
class LatencyStats:
    """Numbers only: counts, sums, max and fixed-bucket counts."""

    count: int
    sum_seconds: float
    max_seconds: float
    buckets: tuple[int, ...]  # len == len(LATENCY_BUCKETS) + 1 (last is +Inf)


class LatencyRecorder:
    """Bounded in-process latency aggregation keyed ONLY by :class:`LatencyStage`.

    :meth:`observe` accepts a stage and a duration; a non-enum stage or a
    non-finite / negative / non-numeric duration is silently dropped so a bug
    can never smuggle text or unbounded keys into the aggregate.
    """

    def __init__(self) -> None:
        width = len(LATENCY_BUCKETS) + 1
        self._count: dict[LatencyStage, int] = dict.fromkeys(LatencyStage, 0)
        self._sum: dict[LatencyStage, float] = dict.fromkeys(LatencyStage, 0.0)
        self._max: dict[LatencyStage, float] = dict.fromkeys(LatencyStage, 0.0)
        self._buckets: dict[LatencyStage, list[int]] = {
            s: [0] * width for s in LatencyStage
        }

    def observe(self, stage: object, seconds: object) -> None:
        if not isinstance(stage, LatencyStage):
            try:
                stage = LatencyStage(stage)  # type: ignore[arg-type]
            except ValueError:
                return
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
            return
        value = float(seconds)
        if not math.isfinite(value) or value < 0:
            return
        self._count[stage] += 1
        self._sum[stage] += value
        self._max[stage] = max(self._max[stage], value)
        for i, bound in enumerate(LATENCY_BUCKETS):
            if value <= bound:
                self._buckets[stage][i] += 1
                break
        else:
            self._buckets[stage][-1] += 1

    def snapshot(self) -> dict[str, LatencyStats]:
        return {
            s.value: LatencyStats(
                count=self._count[s],
                sum_seconds=self._sum[s],
                max_seconds=self._max[s],
                buckets=tuple(self._buckets[s]),
            )
            for s in LatencyStage
        }


#: Process-level recorder (aggregate numbers only; no account dimension).
LATENCY = LatencyRecorder()


# --------------------------------------------------------------------------- #
# sweeper heartbeat
# --------------------------------------------------------------------------- #

_last_sweep: datetime | None = None


def note_sweep(now: datetime) -> None:
    """Record that a sweep ran at ``now``. A no-op while the flag is OFF."""
    global _last_sweep
    if not mail_security_enabled():
        return
    _last_sweep = now if now.tzinfo is not None else now.replace(tzinfo=UTC)


def sweeper_age_seconds(now: datetime) -> float | None:
    """Seconds since the last recorded sweep, or ``None`` if none was recorded."""
    if _last_sweep is None:
        return None
    ref = now if now.tzinfo is not None else now.replace(tzinfo=UTC)
    return max(0.0, (ref - _last_sweep).total_seconds())


def reset_sweeper_heartbeat() -> None:
    """Clear the heartbeat (tests / explicit operator reset)."""
    global _last_sweep
    _last_sweep = None


__all__ = [
    "LATENCY",
    "LATENCY_BUCKETS",
    "NO_TRACE_ID",
    "TRACE_ID_PREFIX",
    "LatencyRecorder",
    "LatencyStage",
    "LatencyStats",
    "note_sweep",
    "reset_sweeper_heartbeat",
    "safe_trace_id",
    "sweeper_age_seconds",
]
