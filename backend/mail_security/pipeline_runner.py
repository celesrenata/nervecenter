"""NerveShield Phase 2 pipeline runner (multi-scope, flag-gated, explicit start).

Today :class:`~backend.mail_security.worker.MailSecurityWorker` and the Task 2.1
sweeper exist, but nothing in a deployment runs them. This runner does, and
only when ``NERVE_MAIL_SECURITY_ENABLED`` is on (default OFF; flag off means no
task is ever started and nothing is constructed).

Each pass (configurable interval, injected clock/sleep for tests):

1. Enumerate the scopes that have runnable work via the repository's
   runner-only :meth:`list_scopes_with_runnable_jobs` -- the single deliberate
   cross-scope read. It returns scope strings only.
2. For each scope, build a worker bound to exactly ONE scope through the
   injected ``worker_factory``, run the sweeper for that scope (reclaim expired
   leases, create missing jobs when a ``sources_provider`` is supplied), then
   drain that scope's claimable jobs.

Failure isolation: an exception in one scope is caught, typed
(:class:`RunnerFailure`), counted and logged by code only, and never stops the
other scopes or the loop. ``asyncio.CancelledError`` is a ``BaseException`` and
is deliberately not caught, so shutdown cancels promptly. Shutdown cancels only
the loop task; it never rewrites job rows (a lease it does not own expires and
is reclaimed by the sweeper). asyncio tasks only; no threads.

Observability: every pass advances the Task 2.6 sweeper heartbeat
(:func:`~backend.mail_security.telemetry.note_sweep`).

Privacy (T-14): logs carry fixed codes and exception type names only.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

import structlog

from backend.mail_security.ingestion_seam import (
    DEFAULT_ANALYSIS_PROFILE_VERSION,
    mail_security_enabled,
)
from backend.mail_security.queue import QueuePolicy, SourcesProvider, sweep_once
from backend.mail_security.telemetry import note_sweep

log = structlog.get_logger(__name__)

DEFAULT_INTERVAL_SECONDS = 30.0


class RunnerFailure(StrEnum):
    """Closed set of typed runner failure codes (counted, never free text)."""

    SCOPE_ENUMERATION_FAILED = "SCOPE_ENUMERATION_FAILED"
    WORKER_BUILD_FAILED = "WORKER_BUILD_FAILED"
    SCOPE_SWEEP_FAILED = "SCOPE_SWEEP_FAILED"
    SCOPE_DRAIN_FAILED = "SCOPE_DRAIN_FAILED"


WorkerFactory = Callable[[str], Any]


class MailSecurityPipelineRunner:
    """Flag-gated, explicitly started multi-scope sweep + drain loop."""

    def __init__(
        self,
        *,
        repository: Any,
        worker_factory: WorkerFactory,
        policy: QueuePolicy | None = None,
        sources_provider: SourcesProvider | None = None,
        interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
        clock: Callable[[], datetime] | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        analysis_profile_version: str = DEFAULT_ANALYSIS_PROFILE_VERSION,
        env: dict[str, str] | None = None,
    ) -> None:
        self._repo = repository
        self._factory = worker_factory
        self._policy = policy or QueuePolicy()
        self._sources = sources_provider
        self._interval = interval_seconds
        self._clock = clock if clock is not None else lambda: datetime.now(tz=UTC)
        self._sleep = sleep if sleep is not None else asyncio.sleep
        self._profile = analysis_profile_version
        self._env = dict(env) if env is not None else None
        self._task: asyncio.Task[None] | None = None
        self._failures: dict[RunnerFailure, int] = dict.fromkeys(RunnerFailure, 0)

    @property
    def enabled(self) -> bool:
        return mail_security_enabled(self._env)

    @property
    def failure_counts(self) -> dict[RunnerFailure, int]:
        """Copy of the per-code failure counters."""
        return dict(self._failures)

    def _fail(self, code: RunnerFailure, exc: Exception) -> None:
        self._failures[code] += 1
        log.warning(
            "mail_security.runner_failure",
            code=code.value,
            error_type=type(exc).__name__,
            count=self._failures[code],
        )

    async def run_once(self) -> list[str] | None:
        """One pass over every scope with work. ``None`` when the flag is OFF,
        else the list of scopes attempted."""
        if not self.enabled or self._repo is None:
            return None
        now = self._clock()
        try:
            scopes = await self._repo.list_scopes_with_runnable_jobs()
        except Exception as exc:  # noqa: BLE001 - one bad read never kills the loop
            self._fail(RunnerFailure.SCOPE_ENUMERATION_FAILED, exc)
            scopes = []
        for scope in scopes:
            await self._run_scope(scope, now)
        note_sweep(now)  # Task 2.6 heartbeat, once per pass
        return list(scopes)

    async def _run_scope(self, scope: str, now: datetime) -> None:
        try:
            worker = self._factory(scope)
        except Exception as exc:  # noqa: BLE001 - isolate per scope
            self._fail(RunnerFailure.WORKER_BUILD_FAILED, exc)
            return
        recorder = getattr(worker, "record_incomplete", None)
        try:
            await sweep_once(
                self._repo,
                scope,
                now=now,
                policy=self._policy,
                sources_provider=self._sources,
                incomplete_recorder=recorder,
                analysis_profile_version=self._profile,
            )
        except Exception as exc:  # noqa: BLE001 - still try to drain
            self._fail(RunnerFailure.SCOPE_SWEEP_FAILED, exc)
        try:
            await worker.process_available()
        except Exception as exc:  # noqa: BLE001 - isolate per scope
            self._fail(RunnerFailure.SCOPE_DRAIN_FAILED, exc)

    def start(self) -> asyncio.Task[None] | None:
        """Start the loop explicitly. ``None`` (nothing started) when the flag
        is OFF; idempotent while running."""
        if not self.enabled:
            return None
        if self._task is None or self._task.done():
            self._task = asyncio.ensure_future(self._loop())
        return self._task

    async def stop(self) -> None:
        """Cancel the loop. Touches no job row."""
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
    "DEFAULT_INTERVAL_SECONDS",
    "MailSecurityPipelineRunner",
    "RunnerFailure",
]
