"""NerveShield reliable job queue, sweeper and runner (Phase 2, Task 2.1).

Hardens the Task 1.8 / 1.9 job plane (R-018) on the columns that already exist
on ``mail_security_jobs`` (``state``, ``attempts``, ``lease_owner``,
``lease_expires_at``, ``last_error_code``, ``meta``) -- no schema change.

Design
------
- **Lease + fencing.** A claim is ONE atomic conditional ``UPDATE`` (see
  ``MailSecurityRepository.mark_job_state`` ``if_*`` guards). It bumps
  ``attempts`` and stamps ``lease_owner`` / ``lease_expires_at``. The pair
  ``(lease_owner, attempts)`` is the *fencing token*: every later write by the
  worker (re-assert, ack, fail) is guarded on it, so a stale worker whose lease
  was reclaimed matches zero rows and cannot overwrite a newer result.
- **Claimable** (pure, injected clock): ``PENDING``; ``ERROR_RETRYABLE`` whose
  backoff "not before" time (kept in ``lease_expires_at`` while no owner holds
  it) has passed; ``RUNNING`` whose lease has expired -- each only while
  ``attempts`` is below the ceiling.
- **Bounded retry.** Backoff reuses :class:`backend.events.dlq.RetryPolicy`
  (deterministic exponential, no jitter, no wall clock). Reaching the attempt
  ceiling moves the job to ``DEAD_LETTER`` with a typed safe reason code.
- **Manual replay only.** :func:`replay_dead_letter` is an explicit call; it
  resets ``DEAD_LETTER -> PENDING`` and keeps lineage (see the repository).
- **Sweeper.** :func:`sweep_once` (a) reclaims expired leases, (b) re-creates
  missing ``PENDING`` jobs via :func:`scan_missing_jobs` (idempotent on the
  Task 1.2 outbox key). It creates no assessments, events or attention items.
- **Runner.** :class:`MailSecurityQueueRunner` is explicit-start and gated on
  ``NERVE_MAIL_SECURITY_ENABLED`` (default OFF). It is NOT started by app
  startup.

Events: this task emits none (see the Task 2.1 report): job state lives in
the durable table, which is the source of truth, and the existing
``backend/events`` bus has no mail-security subject or in-memory publisher.
Only its pure :class:`RetryPolicy` is reused.

Privacy (T-14): only ids, counts and safe codes appear in rows and logs.
Account isolation (R-023): every call takes one ``account_scope``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog

from backend.events.dlq import RetryPolicy
from backend.mail_security.ingestion_seam import (
    DEFAULT_ANALYSIS_PROFILE_VERSION,
    mail_security_enabled,
    scan_missing_jobs,
)
from backend.mail_security.telemetry import note_sweep, safe_trace_id

log = structlog.get_logger(__name__)

# -- job lifecycle states (shared with the worker; stored as capped strings) --
JOB_STATE_PENDING = "PENDING"
JOB_STATE_RUNNING = "RUNNING"
JOB_STATE_DONE = "DONE"
JOB_STATE_ERROR_RETRYABLE = "ERROR_RETRYABLE"
JOB_STATE_DEAD_LETTER = "DEAD_LETTER"

#: Safe reason code stamped when a lease lapsed mid-processing.
CODE_LEASE_EXPIRED = "LEASE_EXPIRED"

DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_LEASE_SECONDS = 300

#: Async callable returning committed-source descriptors for one account scope
#: (``provider`` / ``provider_uid`` / ``rfc_message_id`` / ``folder`` plus safe
#: ``content_ref`` / ``flags``). Injected because ``communications`` carries no
#: account column (R-023 gap), so the queue cannot derive scoped sources itself.
SourcesProvider = Callable[[str], Awaitable[list[dict[str, Any]]]]

#: Async callable that records an honest INCOMPLETE assessment for a job whose
#: lease expired (never clean, I-02). Supplied by the worker.
IncompleteRecorder = Callable[[Any, str], Awaitable[None]]


@dataclass(frozen=True)
class QueuePolicy:
    """Bounded queue parameters: attempt ceiling, lease length, backoff."""

    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    lease_seconds: int = DEFAULT_LEASE_SECONDS
    base_delay_seconds: float = 30.0
    backoff_factor: float = 2.0
    max_delay_seconds: float = 3600.0

    def backoff_delay(self, attempts: int) -> float:
        """Deterministic delay (seconds) after failure number ``attempts``."""
        retry = RetryPolicy(
            max_attempts=max(1, int(self.max_attempts)),
            base_delay_seconds=self.base_delay_seconds,
            backoff_factor=self.backoff_factor,
            max_delay_seconds=self.max_delay_seconds,
        )
        return retry.delay_for_attempt(max(0, int(attempts) - 1))

    def not_before(self, now: datetime, attempts: int) -> datetime:
        """Earliest time a failed job with ``attempts`` may be retried."""
        return now + timedelta(seconds=self.backoff_delay(attempts))


# =============================================================================
# pure helpers
# =============================================================================


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def lease_expired(job: Any, now: datetime) -> bool:
    """``True`` when the job holds no live lease at ``now`` (pure)."""
    expires = _aware(getattr(job, "lease_expires_at", None))
    if not getattr(job, "lease_owner", None) or expires is None:
        return True
    return expires <= now


def is_claimable(job: Any, now: datetime, policy: QueuePolicy) -> bool:
    """Whether ``job`` may be claimed at ``now`` (pure; injected clock)."""
    state = getattr(job, "state", None)
    attempts = int(getattr(job, "attempts", 0) or 0)
    if state == JOB_STATE_PENDING:
        return lease_expired(job, now)
    if attempts >= policy.max_attempts:
        return False
    if state == JOB_STATE_ERROR_RETRYABLE:
        not_before = _aware(getattr(job, "lease_expires_at", None))
        return not_before is None or not_before <= now
    if state == JOB_STATE_RUNNING:
        return lease_expired(job, now)
    return False


# =============================================================================
# claim / reclaim / replay
# =============================================================================


async def claim_next_job(
    repository: Any,
    account_scope: str,
    *,
    worker_id: str,
    now: datetime,
    policy: QueuePolicy,
) -> Any | None:
    """Atomically lease the next claimable job for one account scope.

    Each candidate is claimed with a compare-and-set on its observed
    ``(state, attempts)``; if another worker got there first the guard matches
    nothing and the next candidate is tried, so exactly one worker holds a live
    lease (R-018, T-17).
    """
    for state in (
        JOB_STATE_PENDING,
        JOB_STATE_ERROR_RETRYABLE,
        JOB_STATE_RUNNING,
    ):
        for candidate in await repository.list_jobs(account_scope, state=state):
            if not is_claimable(candidate, now, policy):
                continue
            seen_attempts = int(getattr(candidate, "attempts", 0) or 0)
            claimed = await repository.mark_job_state(
                account_scope,
                candidate.id,
                state=JOB_STATE_RUNNING,
                attempts=seen_attempts + 1,
                lease_owner=worker_id,
                lease_expires_at=now + timedelta(seconds=policy.lease_seconds),
                last_error_code=None,
                if_state=candidate.state,
                if_attempts=seen_attempts,
            )
            if claimed is not None:
                return claimed
    return None


async def replay_dead_letter(
    repository: Any, account_scope: str, job_id: str, *, now: datetime
) -> Any | None:
    """Explicitly replay one ``DEAD_LETTER`` job (never automatic).

    Returns the reset ``PENDING`` job, or ``None`` if it is not dead-lettered
    in this scope. Lineage (row id, outbox key, assessment id, replay history)
    is preserved by the repository.
    """
    return await repository.replay_dead_letter(
        account_scope, job_id, replayed_at=now
    )


@dataclass
class SweepResult:
    """Outcome of one sweep (counts and ids only; nothing raw)."""

    reclaimed: list[str] = field(default_factory=list)
    dead_lettered: list[str] = field(default_factory=list)
    created: list[str] = field(default_factory=list)


async def reclaim_expired_leases(
    repository: Any,
    account_scope: str,
    *,
    now: datetime,
    policy: QueuePolicy,
    incomplete_recorder: IncompleteRecorder | None = None,
) -> SweepResult:
    """Move ``RUNNING`` jobs with an expired lease to retry or dead letter.

    Guarded on the observed fencing token, so a worker that renewed/finished
    meanwhile is never clobbered. An honest INCOMPLETE assessment is recorded
    through ``incomplete_recorder`` (never clean, I-02).
    """
    result = SweepResult()
    for job in await repository.list_jobs(account_scope, state=JOB_STATE_RUNNING):
        if not lease_expired(job, now):
            continue
        attempts = int(job.attempts or 0)
        exhausted = attempts >= policy.max_attempts
        moved = await repository.mark_job_state(
            account_scope,
            job.id,
            state=JOB_STATE_DEAD_LETTER if exhausted else JOB_STATE_ERROR_RETRYABLE,
            attempts=attempts,
            lease_owner=None,
            lease_expires_at=None if exhausted else policy.not_before(now, attempts),
            last_error_code=CODE_LEASE_EXPIRED,
            if_state=JOB_STATE_RUNNING,
            if_attempts=attempts,
        )
        if moved is None:
            continue
        (result.dead_lettered if exhausted else result.reclaimed).append(job.id)
        if incomplete_recorder is not None:
            await incomplete_recorder(moved, CODE_LEASE_EXPIRED)
        log.warning(
            "mail_security.lease_reclaimed",
            trace_id=safe_trace_id(job.id),
            attempts=attempts,
            dead_letter=exhausted,
        )
    return result


async def sweep_once(
    repository: Any,
    account_scope: str,
    *,
    now: datetime,
    policy: QueuePolicy | None = None,
    sources_provider: SourcesProvider | None = None,
    incomplete_recorder: IncompleteRecorder | None = None,
    analysis_profile_version: str = DEFAULT_ANALYSIS_PROFILE_VERSION,
) -> SweepResult:
    """One sweep for ONE account scope: reclaim leases, repair missing jobs.

    Idempotent: a second sweep over unchanged state changes nothing. Creates
    only ``PENDING`` job rows (keyed on the Task 1.2 outbox key); never an
    assessment, event or attention item.
    """
    pol = policy or QueuePolicy()
    result = await reclaim_expired_leases(
        repository,
        account_scope,
        now=now,
        policy=pol,
        incomplete_recorder=incomplete_recorder,
    )
    if sources_provider is not None:
        existing = {j.outbox_job_key for j in await repository.list_jobs(account_scope)}
        sources = await sources_provider(account_scope)
        rows = await scan_missing_jobs(
            repository=repository,
            account_scope=account_scope,
            sources=sources,
            analysis_profile_version=analysis_profile_version,
        )
        result.created = [r.id for r in rows if r.outbox_job_key not in existing]
    note_sweep(now)  # heartbeat timestamp only (flag-gated, Task 2.6)
    return result


# =============================================================================
# runner (explicit start, flag-gated, default OFF)
# =============================================================================


class MailSecurityQueueRunner:
    """Flag-gated runner: sweep, then drain the worker, for one account scope.

    Inert (does nothing, starts no task) unless ``NERVE_MAIL_SECURITY_ENABLED``
    is on. Never started by app startup; an operator/startup hook must call
    :meth:`start` explicitly.
    """

    def __init__(
        self,
        *,
        repository: Any,
        account_scope: str,
        worker: Any | None = None,
        policy: QueuePolicy | None = None,
        sources_provider: SourcesProvider | None = None,
        interval_seconds: float = 60.0,
        clock: Callable[[], datetime] | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        analysis_profile_version: str = DEFAULT_ANALYSIS_PROFILE_VERSION,
        env: dict[str, str] | None = None,
    ) -> None:
        self._repo = repository
        self._scope = account_scope
        self._worker = worker
        self._policy = policy or QueuePolicy()
        self._sources = sources_provider
        self._interval = interval_seconds
        self._clock = clock if clock is not None else lambda: datetime.now(tz=UTC)
        self._sleep = sleep if sleep is not None else asyncio.sleep
        self._profile = analysis_profile_version
        self._env = dict(env) if env is not None else None
        self._task: asyncio.Task[None] | None = None

    @property
    def enabled(self) -> bool:
        return mail_security_enabled(self._env)

    async def run_once(self) -> SweepResult | None:
        """One sweep + worker drain. ``None`` (no work) when the flag is OFF."""
        if not self.enabled or self._repo is None:
            return None
        recorder = (
            getattr(self._worker, "record_incomplete", None)
            if self._worker is not None
            else None
        )
        result = await sweep_once(
            self._repo,
            self._scope,
            now=self._clock(),
            policy=self._policy,
            sources_provider=self._sources,
            incomplete_recorder=recorder,
            analysis_profile_version=self._profile,
        )
        if self._worker is not None:
            await self._worker.process_available()
        return result

    def start(self) -> asyncio.Task[None] | None:
        """Start the loop explicitly. Returns ``None`` (nothing started) when
        the flag is OFF; idempotent while running."""
        if not self.enabled:
            return None
        if self._task is None or self._task.done():
            self._task = asyncio.ensure_future(self._loop())
        return self._task

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def _loop(self) -> None:
        while True:
            try:
                await self.run_once()
            except Exception as exc:  # noqa: BLE001 - loop must survive a bad pass
                log.warning(
                    "mail_security.runner_pass_failed",
                    error_type=type(exc).__name__,
                )
            await self._sleep(self._interval)


__all__ = [
    "CODE_LEASE_EXPIRED",
    "DEFAULT_LEASE_SECONDS",
    "DEFAULT_MAX_ATTEMPTS",
    "JOB_STATE_DEAD_LETTER",
    "JOB_STATE_DONE",
    "JOB_STATE_ERROR_RETRYABLE",
    "JOB_STATE_PENDING",
    "JOB_STATE_RUNNING",
    "MailSecurityQueueRunner",
    "QueuePolicy",
    "SweepResult",
    "claim_next_job",
    "is_claimable",
    "lease_expired",
    "reclaim_expired_leases",
    "replay_dead_letter",
    "sweep_once",
]
