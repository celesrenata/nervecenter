"""NerveShield Phase 2 pipeline wiring tests (Item 1: flag-gated runner).

Proves the explicitly-started, flag-gated multi-scope runner that finally
drives the Task 1.9 worker and the Task 2.1 sweeper in a deployment:

1. FLAG OFF -> no runner object, no asyncio task, no worker construction
   (R-001, I-01).
2. FLAG ON, end to end -> a real ``MailIngestWorker`` + the 1.8 seam schedule a
   PENDING job; the runner (injected clock/sleep) drains it to a persisted
   assessment without any test calling the worker directly.
3. ISOLATION OF FAILURE -> one scope whose worker blows up never stops the
   other scopes or the loop; failures are typed and counted.
4. CLEAN SHUTDOWN -> ``stop()`` cancels the loop and does not touch rows it
   does not own (a foreign live lease stays RUNNING).
5. SCOPE ENUMERATION IS RUNNER-ONLY -> the single deliberate cross-scope read
   returns scope strings only and no API layer module references it (R-023).
6. HEARTBEAT -> the Task 2.6 sweeper heartbeat advances on every pass.
"""

from __future__ import annotations

import asyncio
import pathlib
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import _mail_security_harness_helpers as H
import pytest

from backend.api.models import MailConnector  # noqa: F401 - import-order guard
from backend.mail_security import telemetry
from backend.mail_security.ingestion_seam import (
    DEFAULT_ANALYSIS_PROFILE_VERSION,
    ENV_MAIL_SECURITY_ENABLED,
    JOB_STATE_PENDING,
)
from backend.mail_security.pipeline_runner import (
    MailSecurityPipelineRunner,
    RunnerFailure,
)
from backend.mail_security.queue import JOB_STATE_DONE, JOB_STATE_RUNNING

_ON = {ENV_MAIL_SECURITY_ENABLED: "1"}
_OFF: dict[str, str] = {}
_NOW = H.CLOCK


@pytest.fixture
async def harness(tmp_path):
    h = await H.make_harness(tmp_path / "wiring.sqlite")
    telemetry.reset_sweeper_heartbeat()
    yield h
    telemetry.reset_sweeper_heartbeat()
    await H.teardown_harness(h)


def _factory(harness: Any, built: list[str] | None = None):
    def make(scope: str):
        if built is not None:
            built.append(scope)
        return H.security_worker(harness) if scope == harness.scope else _other(
            harness, scope
        )

    return make


def _other(harness: Any, scope: str):
    from backend.mail_security.worker import (
        MailSecurityWorker,
        ObjectStoreContentResolver,
    )

    return MailSecurityWorker(
        repository=harness.registry.mail_security,
        content_resolver=ObjectStoreContentResolver(object_store=harness.store),
        account_scope=scope,
        analysis_profile_version=DEFAULT_ANALYSIS_PROFILE_VERSION,
        clock=lambda: _NOW,
        env=_ON,
    )


async def _no_sleep(_: float) -> None:
    await asyncio.sleep(0)


def _runner(harness: Any, *, env=_ON, factory=None, sleep=_no_sleep, **kw: Any):
    return MailSecurityPipelineRunner(
        repository=harness.registry.mail_security,
        worker_factory=factory or _factory(harness),
        interval_seconds=5.0,
        clock=lambda: _NOW,
        sleep=sleep,
        env=env,
        **kw,
    )


async def _seed_job(harness: Any, scope: str, key: str, *, state: str = "PENDING"):
    return await harness.registry.mail_security.upsert_job(
        account_scope=scope,
        outbox_job_key=key,
        source_key=f"src-{key}",
        analysis_profile_version=DEFAULT_ANALYSIS_PROFILE_VERSION,
        state=state,
        meta={"requested": True},
    )


# --------------------------------------------------------------------------
# 1. flag off
# --------------------------------------------------------------------------
class TestFlagOff:
    async def test_runner_inert_and_starts_no_task(self, harness) -> None:
        built: list[str] = []
        runner = _runner(harness, env=_OFF, factory=_factory(harness, built))
        await _seed_job(harness, harness.scope, "k-off")
        assert runner.enabled is False
        assert runner.start() is None
        assert await runner.run_once() is None
        assert built == []
        rows = await harness.registry.mail_security.list_jobs(harness.scope)
        assert [r.state for r in rows] == [JOB_STATE_PENDING]
        assert telemetry.sweeper_age_seconds(_NOW) is None

    async def test_wiring_flag_off_builds_nothing(self, monkeypatch) -> None:
        from backend.api import wiring

        monkeypatch.delenv(ENV_MAIL_SECURITY_ENABLED, raising=False)
        called: list[str] = []
        monkeypatch.setattr(
            wiring, "_mail_security_object_store", lambda: called.append("store")
        )
        target = SimpleNamespace(
            state=SimpleNamespace(repository_registry=object())
        )
        before = set(asyncio.all_tasks())
        assert wiring.start_mail_security_runner(target) is None
        assert set(asyncio.all_tasks()) == before
        assert called == []
        assert getattr(target.state, "mail_security_runner", None) is None
        await wiring.stop_mail_security_runner(target)  # no-op, never raises

    async def test_wiring_flag_on_without_store_or_repo_starts_nothing(
        self, monkeypatch
    ) -> None:
        from backend.api import wiring

        monkeypatch.setenv(ENV_MAIL_SECURITY_ENABLED, "1")
        monkeypatch.setattr(wiring, "_mail_security_object_store", lambda: None)
        target = SimpleNamespace(
            state=SimpleNamespace(
                repository_registry=SimpleNamespace(mail_security=object())
            )
        )
        assert wiring.start_mail_security_runner(target) is None
        target2 = SimpleNamespace(state=SimpleNamespace(repository_registry=None))
        assert wiring.start_mail_security_runner(target2) is None


    async def test_wiring_flag_on_starts_and_stops_runner(
        self, harness, monkeypatch
    ) -> None:
        from backend.api import wiring

        monkeypatch.setenv(ENV_MAIL_SECURITY_ENABLED, "1")
        monkeypatch.setattr(wiring, "_mail_security_object_store", lambda: harness.store)
        target = SimpleNamespace(
            state=SimpleNamespace(repository_registry=harness.registry)
        )
        task = wiring.start_mail_security_runner(target)
        assert task is not None and not task.done()
        assert target.state.mail_security_runner is not None
        await wiring.stop_mail_security_runner(target)
        assert task.done()
        assert target.state.mail_security_runner is None


# --------------------------------------------------------------------------
# 2. flag on, end to end
# --------------------------------------------------------------------------
class TestEndToEndDrain:
    async def test_ingest_seam_runner_to_persisted_assessment(
        self, harness, monkeypatch
    ) -> None:
        monkeypatch.setenv(ENV_MAIL_SECURITY_ENABLED, "1")
        await H.ingest(
            harness,
            monkeypatch,
            H.mailitem("<wire-1@example-news.com>", H.benign_mime()),
        )
        repo = harness.registry.mail_security
        jobs = await repo.list_jobs(harness.scope)
        assert [j.state for j in jobs] == [JOB_STATE_PENDING]
        assert await repo.list_assessments(harness.scope) == []

        done = asyncio.Event()
        passes = 0

        async def sleep(_: float) -> None:
            nonlocal passes
            passes += 1
            if passes >= 2:
                done.set()
            await asyncio.sleep(0)

        runner = _runner(harness, sleep=sleep)
        task = runner.start()
        assert task is not None
        await asyncio.wait_for(done.wait(), timeout=10)
        await runner.stop()

        jobs = await repo.list_jobs(harness.scope)
        assert [j.state for j in jobs] == [JOB_STATE_DONE]
        assessments = await repo.list_assessments(harness.scope)
        assert len(assessments) == 1
        # Task 2.6 heartbeat advanced by the runner's sweeps.
        assert telemetry.sweeper_age_seconds(_NOW) == 0.0

    async def test_start_is_idempotent_and_stop_is_reentrant(self, harness) -> None:
        runner = _runner(harness)
        t1 = runner.start()
        t2 = runner.start()
        assert t1 is t2
        await runner.stop()
        await runner.stop()


# --------------------------------------------------------------------------
# 3. failure isolation + typed counted failures
# --------------------------------------------------------------------------
class TestFailureIsolation:
    async def test_one_failing_scope_does_not_stop_others(self, harness) -> None:
        good, bad = harness.scope, "conn:bad-scope"
        await _seed_job(harness, bad, "k-bad")
        await _seed_job(harness, good, "k-good")

        def factory(scope: str):
            if scope == bad:
                raise RuntimeError("boom CANARY-secret")
            return H.security_worker(harness)

        runner = _runner(harness, factory=factory)
        await runner.run_once()
        await runner.run_once()  # loop survives; bad scope retried each pass
        assert runner.failure_counts[RunnerFailure.WORKER_BUILD_FAILED] == 2
        assert runner.failure_counts[RunnerFailure.SCOPE_DRAIN_FAILED] == 0
        # The good scope was attempted despite the bad one (job leaves PENDING;
        # content is absent so it records INCOMPLETE rather than staying idle).
        good_jobs = await harness.registry.mail_security.list_jobs(good)
        assert good_jobs[0].state != JOB_STATE_PENDING
        bad_jobs = await harness.registry.mail_security.list_jobs(bad)
        assert bad_jobs[0].state == JOB_STATE_PENDING

    async def test_drain_exception_is_typed_counted_and_logged_safely(
        self, harness, capsys
    ) -> None:
        await _seed_job(harness, harness.scope, "k-1")
        await _seed_job(harness, "conn:other", "k-2")

        class Boom:
            async def process_available(self, **_: Any) -> list[Any]:
                raise ValueError("CANARY-secret-body")

            async def record_incomplete(self, *_: Any) -> None:
                return None

        def factory(scope: str):
            return Boom() if scope == "conn:other" else H.security_worker(harness)

        runner = _runner(harness, factory=factory)
        await runner.run_once()
        assert runner.failure_counts[RunnerFailure.SCOPE_DRAIN_FAILED] == 1
        good = await harness.registry.mail_security.list_jobs(harness.scope)
        assert good[0].state != JOB_STATE_PENDING
        out = capsys.readouterr()
        assert "CANARY" not in out.out + out.err

    async def test_enumeration_failure_does_not_kill_loop(self, harness) -> None:
        calls = 0

        class Repo:
            async def list_scopes_with_runnable_jobs(self) -> list[str]:
                nonlocal calls
                calls += 1
                raise OSError("db down")

        runner = MailSecurityPipelineRunner(
            repository=Repo(),
            worker_factory=_factory(harness),
            interval_seconds=1.0,
            clock=lambda: _NOW,
            sleep=_no_sleep,
            env=_ON,
        )
        await runner.run_once()
        await runner.run_once()
        assert calls == 2
        assert runner.failure_counts[RunnerFailure.SCOPE_ENUMERATION_FAILED] == 2

    async def test_failing_enumeration_does_not_advance_heartbeat(
        self, harness
    ) -> None:
        class Repo:
            async def list_scopes_with_runnable_jobs(self) -> list[str]:
                raise OSError("db down")

        runner = MailSecurityPipelineRunner(
            repository=Repo(),
            worker_factory=_factory(harness),
            interval_seconds=1.0,
            clock=lambda: _NOW,
            sleep=_no_sleep,
            env=_ON,
        )
        await runner.run_once()
        # Nothing swept: the stale-sweeper signal must stay un-fed (None = never).
        assert telemetry.sweeper_age_seconds(_NOW) is None

    async def test_loop_survives_failing_pass(self, harness) -> None:
        passes = 0
        done = asyncio.Event()

        class Repo:
            async def list_scopes_with_runnable_jobs(self) -> list[str]:
                nonlocal passes
                passes += 1
                if passes >= 3:
                    done.set()
                raise OSError("down")

        runner = MailSecurityPipelineRunner(
            repository=Repo(),
            worker_factory=_factory(harness),
            interval_seconds=1.0,
            clock=lambda: _NOW,
            sleep=_no_sleep,
            env=_ON,
        )
        runner.start()
        await asyncio.wait_for(done.wait(), timeout=10)
        await runner.stop()

    def test_failure_codes_are_a_closed_typed_set(self) -> None:
        assert {f.value for f in RunnerFailure} == {
            "SCOPE_ENUMERATION_FAILED",
            "WORKER_BUILD_FAILED",
            "SCOPE_SWEEP_FAILED",
            "SCOPE_DRAIN_FAILED",
        }


# --------------------------------------------------------------------------
# 4. clean shutdown
# --------------------------------------------------------------------------
class TestShutdown:
    async def test_stop_cancels_loop_and_releases_nothing_foreign(
        self, harness
    ) -> None:
        # A job leased by ANOTHER worker with a live lease.
        row = await _seed_job(harness, harness.scope, "k-foreign")
        future = _NOW + timedelta(hours=1)
        await harness.registry.mail_security.mark_job_state(
            harness.scope,
            row.id,
            state=JOB_STATE_RUNNING,
            attempts=1,
            lease_owner="other-worker",
            lease_expires_at=future,
        )
        entered = asyncio.Event()

        async def blocking_sleep(_: float) -> None:
            entered.set()
            await asyncio.sleep(3600)

        runner = _runner(harness, sleep=blocking_sleep)
        task = runner.start()
        assert task is not None
        await asyncio.wait_for(entered.wait(), timeout=10)
        await runner.stop()
        assert task.done()
        assert task.cancelled() or task.exception() is None
        jobs = await harness.registry.mail_security.list_jobs(harness.scope)
        assert jobs[0].state == JOB_STATE_RUNNING
        assert jobs[0].lease_owner == "other-worker"
        assert not [t for t in asyncio.all_tasks() if t is not asyncio.current_task()
                    and "_loop" in repr(t.get_coro())]

    async def test_cancellation_is_not_swallowed_by_scope_handler(
        self, harness
    ) -> None:
        await _seed_job(harness, harness.scope, "k-cancel")
        gate = asyncio.Event()

        class Slow:
            async def process_available(self, **_: Any) -> list[Any]:
                gate.set()
                await asyncio.sleep(3600)
                return []

            async def record_incomplete(self, *_: Any) -> None:
                return None

        runner = _runner(harness, factory=lambda scope: Slow())
        task = runner.start()
        await asyncio.wait_for(gate.wait(), timeout=10)
        await runner.stop()
        assert task is not None and task.done()
        assert sum(runner.failure_counts.values()) == 0


# --------------------------------------------------------------------------
# 5. scope enumeration is runner-only
# --------------------------------------------------------------------------
class TestScopeEnumerationRunnerOnly:
    async def test_returns_scope_strings_only_for_runnable_states(
        self, harness
    ) -> None:
        repo = harness.registry.mail_security
        await _seed_job(harness, "conn:a", "ka", state="PENDING")
        await _seed_job(harness, "conn:a", "ka2", state="PENDING")
        await _seed_job(harness, "conn:b", "kb", state="ERROR_RETRYABLE")
        await _seed_job(harness, "conn:c", "kc", state="DONE")
        await _seed_job(harness, "conn:d", "kd", state="DEAD_LETTER")
        scopes = await repo.list_scopes_with_runnable_jobs()
        assert scopes == ["conn:a", "conn:b"]
        assert all(type(s) is str for s in scopes)

    async def test_includes_running_scope_so_expired_leases_get_swept(
        self, harness
    ) -> None:
        await _seed_job(harness, "conn:r", "kr", state="RUNNING")
        assert await harness.registry.mail_security.list_scopes_with_runnable_jobs() == [
            "conn:r"
        ]

    def test_api_layer_does_not_use_it(self) -> None:
        root = pathlib.Path(__file__).resolve().parents[2] / "backend"
        offenders: list[str] = []
        for path in (root / "api").rglob("*.py"):
            if "list_scopes_with_runnable_jobs" in path.read_text(encoding="utf-8"):
                offenders.append(str(path.relative_to(root)))
        assert offenders == []
        # Only the repository (definition) and the runner module may name it.
        users = sorted(
            str(p.relative_to(root))
            for p in root.rglob("*.py")
            if "list_scopes_with_runnable_jobs" in p.read_text(encoding="utf-8")
        )
        assert users == [
            "mail_security/pipeline_runner.py",
            "persistence/repositories/mail_security_repo.py",
        ]

    async def test_each_worker_bound_to_exactly_one_scope(self, harness) -> None:
        built: list[str] = []
        await _seed_job(harness, "conn:x", "kx")
        await _seed_job(harness, "conn:y", "ky")
        runner = _runner(harness, factory=_factory(harness, built))
        await runner.run_once()
        assert sorted(built) == ["conn:x", "conn:y"]
        assert len(built) == len(set(built))


# --------------------------------------------------------------------------
# 6. sweeper work
# --------------------------------------------------------------------------
class TestSweeperRuns:
    async def test_expired_lease_reclaimed_by_runner(self, harness) -> None:
        row = await _seed_job(harness, harness.scope, "k-exp")
        await harness.registry.mail_security.mark_job_state(
            harness.scope,
            row.id,
            state=JOB_STATE_RUNNING,
            attempts=1,
            lease_owner="dead-worker",
            lease_expires_at=_NOW - timedelta(minutes=5),
        )
        runner = _runner(harness)
        await runner.run_once()
        jobs = await harness.registry.mail_security.list_jobs(harness.scope)
        assert jobs[0].lease_owner != "dead-worker"
        assert jobs[0].last_error_code is not None or jobs[0].state != JOB_STATE_RUNNING

    async def test_sources_provider_creates_missing_jobs(self, harness) -> None:
        await _seed_job(harness, harness.scope, "k-seed")

        async def sources(scope: str) -> list[dict[str, Any]]:
            return [{"provider": "imap", "provider_uid": "77", "folder": "INBOX"}]

        runner = _runner(harness, sources_provider=sources)
        await runner.run_once()
        jobs = await harness.registry.mail_security.list_jobs(harness.scope)
        assert len(jobs) == 2
