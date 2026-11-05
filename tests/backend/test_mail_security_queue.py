"""NerveShield reliable queue / lease / dead-letter / sweeper tests (Task 2.1).

Done criterion (tasks.md 2.1): "crash, timeout, double-delivery and replay
tests." Requirement R-018 (reliable idempotent processing), with I-02 (never
clean on failure), I-07 (scheduling never perturbs ingest), I-11 (idempotent
assessments), T-16/T-17/T-18 and R-023 (account isolation).

All tests use an injected, manually advanced clock (no sleeps, no wall clock)
and the SQLite scratch-DB ingest harness from the Task 1.9/1.12 suites.
SQLite proves the *logic* of the atomic claim (a single conditional UPDATE);
it does not prove Postgres row-lock concurrency (see the Task 2.1 report).
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import _mail_security_harness_helpers as H
import pytest
import structlog
from sqlalchemy import select

from backend.mail_security.ingestion_seam import (
    DEFAULT_ANALYSIS_PROFILE_VERSION,
    ENV_MAIL_SECURITY_ENABLED,
)
from backend.mail_security.models import Disposition, ProcessingState
from backend.mail_security.queue import (
    CODE_LEASE_EXPIRED,
    JOB_STATE_DEAD_LETTER,
    JOB_STATE_DONE,
    JOB_STATE_ERROR_RETRYABLE,
    JOB_STATE_PENDING,
    JOB_STATE_RUNNING,
    MailSecurityQueueRunner,
    QueuePolicy,
    claim_next_job,
    is_claimable,
    replay_dead_letter,
    sweep_once,
)
from backend.mail_security.worker import (
    MailSecurityWorker,
    ObjectStoreContentResolver,
)
from backend.persistence.models.personal import AttentionItemRow

_T0 = datetime(2026, 10, 27, 10, 0, 0, tzinfo=UTC)
_ON = {ENV_MAIL_SECURITY_ENABLED: "1"}
_POLICY = QueuePolicy(max_attempts=3, lease_seconds=60, base_delay_seconds=30.0)


class Clock:
    """A manually advanced clock (deterministic; no sleeps)."""

    def __init__(self) -> None:
        self.now = _T0

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)


@pytest.fixture
async def harness(tmp_path: Path):
    h = await H.make_harness(tmp_path / "queue.sqlite")
    yield h
    await H.teardown_harness(h)


def _worker(harness, clock: Clock, *, worker_id: str, **kwargs: Any) -> MailSecurityWorker:
    return MailSecurityWorker(
        repository=harness.registry.mail_security,
        content_resolver=kwargs.pop(
            "content_resolver", ObjectStoreContentResolver(object_store=harness.store)
        ),
        account_scope=harness.scope,
        analysis_profile_version=DEFAULT_ANALYSIS_PROFILE_VERSION,
        clock=clock,
        env=_ON,
        worker_id=worker_id,
        max_attempts=_POLICY.max_attempts,
        lease_seconds=_POLICY.lease_seconds,
        **kwargs,
    )


async def _one_job(harness, monkeypatch, mid: str = "<q1@corp>") -> Any:
    await H.ingest(harness, monkeypatch, H.mailitem(mid, H.benign_mime()))
    jobs = await harness.registry.mail_security.list_jobs(harness.scope)
    assert len(jobs) == 1 and jobs[0].state == JOB_STATE_PENDING
    return jobs[0]


async def _job(harness, job_id: str) -> Any:
    return await harness.registry.mail_security.get_job(harness.scope, job_id)


async def _assessments(harness) -> list[Any]:
    return await harness.registry.mail_security.list_assessments(harness.scope)


# ---------------------------------------------------------------------------
# pure functions
# ---------------------------------------------------------------------------


class TestPureQueueLogic:
    def test_backoff_is_deterministic_exponential_and_capped(self) -> None:
        p = QueuePolicy(base_delay_seconds=30.0, backoff_factor=2.0, max_delay_seconds=100.0)
        assert [p.backoff_delay(n) for n in (1, 2, 3, 4)] == [30.0, 60.0, 100.0, 100.0]
        assert p.not_before(_T0, 2) == _T0 + timedelta(seconds=60)

    def test_is_claimable_rules(self) -> None:
        from types import SimpleNamespace as NS

        live = _T0 + timedelta(seconds=10)
        gone = _T0 - timedelta(seconds=10)

        def can(state: str, attempts: int, owner: str | None, exp: Any) -> bool:
            job = NS(state=state, attempts=attempts, lease_owner=owner, lease_expires_at=exp)
            return is_claimable(job, _T0, _POLICY)

        assert can("PENDING", 0, None, None)
        assert not can("RUNNING", 1, "w", live)
        assert can("RUNNING", 1, "w", gone)
        assert not can("RUNNING", 3, "w", gone)  # at the ceiling
        assert not can("ERROR_RETRYABLE", 1, None, live)  # backoff not elapsed
        assert can("ERROR_RETRYABLE", 1, None, gone)
        assert not can("DEAD_LETTER", 3, None, None)
        assert not can("DONE", 1, None, None)

    def test_queue_module_has_no_network_or_model_imports(self) -> None:
        import backend.mail_security.queue as q

        src = Path(q.__file__).read_text(encoding="utf-8")
        forbidden_terms = (
            "import socket", "import http", "import urllib", "import requests", "omniroute",
        )
        for forbidden in forbidden_terms:
            assert forbidden not in src


# ---------------------------------------------------------------------------
# crash / reclaim (R-018, T-17)
# ---------------------------------------------------------------------------


class TestCrashRecovery:
    async def test_crash_after_claim_reclaimed_after_expiry_one_assessment(
        self, harness, monkeypatch
    ) -> None:
        job = await _one_job(harness, monkeypatch)
        clock = Clock()
        repo = harness.registry.mail_security
        # Worker A claims, then "crashes" before producing any verdict.
        claimed = await claim_next_job(
            repo, harness.scope, worker_id="A", now=clock(), policy=_POLICY
        )
        assert claimed is not None and claimed.state == JOB_STATE_RUNNING
        b = _worker(harness, clock, worker_id="B")
        # Lease still live: B cannot take it.
        assert await b.process_one() is None
        clock.advance(_POLICY.lease_seconds + 1)
        done = await b.process_one()
        assert done is not None and done.state == JOB_STATE_DONE
        assert done.attempts == 2
        assert len(await _assessments(harness)) == 1
        assert (await _job(harness, job.id)).state == JOB_STATE_DONE

    async def test_crash_after_verdict_before_ack_redelivery_one_revision(
        self, harness, monkeypatch
    ) -> None:
        job = await _one_job(harness, monkeypatch)
        clock = Clock()
        repo = harness.registry.mail_security
        a = _worker(harness, clock, worker_id="A")
        first = await a.process_one()
        assert first is not None and first.state == JOB_STATE_DONE
        before = (await _assessments(harness))[0].assessment_id
        # Simulate the crash window: verdict committed, ack lost -> still RUNNING.
        await repo.mark_job_state(
            harness.scope, job.id, state=JOB_STATE_RUNNING, attempts=1,
            lease_owner="A", lease_expires_at=clock(),
        )
        clock.advance(1)
        again = await _worker(harness, clock, worker_id="B").process_one()
        assert again is not None and again.state == JOB_STATE_DONE
        rows = await _assessments(harness)
        assert len(rows) == 1 and rows[0].assessment_id == before
        findings = await repo.get_findings(harness.scope, before)
        assert len({f.finding_key for f in findings}) == len(findings)

    async def test_sweeper_reclaims_expired_lease_and_never_clean(
        self, harness, monkeypatch
    ) -> None:
        job = await _one_job(harness, monkeypatch)
        clock = Clock()
        repo = harness.registry.mail_security
        await claim_next_job(repo, harness.scope, worker_id="A", now=clock(), policy=_POLICY)
        w = _worker(harness, clock, worker_id="B")
        clock.advance(_POLICY.lease_seconds + 1)
        res = await sweep_once(
            repo, harness.scope, now=clock(), policy=_POLICY,
            incomplete_recorder=w.record_incomplete,
        )
        assert res.reclaimed == [job.id]
        moved = await _job(harness, job.id)
        assert moved.state == JOB_STATE_ERROR_RETRYABLE
        assert moved.last_error_code == CODE_LEASE_EXPIRED
        assert moved.lease_owner is None
        # Honest INCOMPLETE assessment recorded: never clean (I-02).
        row = (await _assessments(harness))[0]
        assert row.processing_state == ProcessingState.INCOMPLETE.value
        assert row.disposition != Disposition.SAFE_TO_PROCESS.value
        # Not due until backoff elapses.
        assert await w.process_one() is None
        clock.advance(_POLICY.backoff_delay(1) + 1)
        done = await w.process_one()
        assert done is not None and done.state == JOB_STATE_DONE
        rows = await _assessments(harness)
        assert len(rows) == 1  # same id, INCOMPLETE placeholder upgraded in place
        assert rows[0].processing_state == ProcessingState.ASSESSED.value

    async def test_sweeper_second_pass_is_noop(self, harness, monkeypatch) -> None:
        await _one_job(harness, monkeypatch)
        clock = Clock()
        repo = harness.registry.mail_security
        await claim_next_job(repo, harness.scope, worker_id="A", now=clock(), policy=_POLICY)
        clock.advance(_POLICY.lease_seconds + 1)
        first = await sweep_once(repo, harness.scope, now=clock(), policy=_POLICY)
        second = await sweep_once(repo, harness.scope, now=clock(), policy=_POLICY)
        assert len(first.reclaimed) == 1
        assert second.reclaimed == [] and second.dead_lettered == [] and second.created == []


# ---------------------------------------------------------------------------
# double delivery + fencing (R-018, T-17)
# ---------------------------------------------------------------------------


class TestDoubleDeliveryAndFencing:
    async def test_two_concurrent_claims_exactly_one_winner(
        self, harness, monkeypatch, tmp_path
    ) -> None:
        """Two claimers on SEPARATE SQLite connections (the harness engine is
        a single shared connection, which cannot model concurrency)."""
        from sqlalchemy.ext.asyncio import create_async_engine

        from backend.persistence.connection import make_session_factory
        from backend.persistence.repositories.mail_security_repo import (
            MailSecurityRepository,
        )

        await _one_job(harness, monkeypatch)
        second = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'queue.sqlite'}")
        try:
            repo_a = harness.registry.mail_security
            repo_b = MailSecurityRepository(make_session_factory(second))
            results = await asyncio.gather(
                claim_next_job(repo_a, harness.scope, worker_id="A", now=_T0, policy=_POLICY),
                claim_next_job(repo_b, harness.scope, worker_id="B", now=_T0, policy=_POLICY),
            )
        finally:
            await second.dispose()
        winners = [r for r in results if r is not None]
        assert len(winners) == 1
        assert winners[0].attempts == 1
        held = (await harness.registry.mail_security.list_jobs(harness.scope))[0]
        assert held.state == JOB_STATE_RUNNING and held.attempts == 1
        assert held.lease_owner == winners[0].lease_owner

    async def test_sequential_double_claim_second_gets_nothing(
        self, harness, monkeypatch
    ) -> None:
        await _one_job(harness, monkeypatch)
        repo = harness.registry.mail_security
        first = await claim_next_job(repo, harness.scope, worker_id="A", now=_T0, policy=_POLICY)
        second = await claim_next_job(repo, harness.scope, worker_id="B", now=_T0, policy=_POLICY)
        assert first is not None and second is None

    async def test_guarded_write_with_stale_token_matches_nothing(
        self, harness, monkeypatch
    ) -> None:
        job = await _one_job(harness, monkeypatch)
        repo = harness.registry.mail_security
        a = await claim_next_job(repo, harness.scope, worker_id="A", now=_T0, policy=_POLICY)
        later = _T0 + timedelta(seconds=_POLICY.lease_seconds + 1)
        b = await claim_next_job(repo, harness.scope, worker_id="B", now=later, policy=_POLICY)
        assert a is not None and b is not None and b.attempts == 2
        stale = await repo.mark_job_state(
            harness.scope, job.id, state=JOB_STATE_DONE, attempts=a.attempts,
            lease_owner=None, lease_expires_at=None,
            if_state=JOB_STATE_RUNNING, if_attempts=a.attempts, if_lease_owner="A",
        )
        assert stale is None
        current = await _job(harness, job.id)
        assert current.state == JOB_STATE_RUNNING and current.lease_owner == "B"

    async def test_stale_worker_cannot_overwrite_newer_result(
        self, harness, monkeypatch
    ) -> None:
        job = await _one_job(harness, monkeypatch)
        clock = Clock()
        repo = harness.registry.mail_security
        worker_a = _worker(harness, clock, worker_id="A")
        job_a = await claim_next_job(
            repo, harness.scope, worker_id="A", now=clock(), policy=_POLICY
        )
        assert job_a is not None
        clock.advance(_POLICY.lease_seconds + 1)
        # B reclaims and finishes with the real verdict.
        done_b = await _worker(harness, clock, worker_id="B").process_one()
        assert done_b is not None and done_b.state == JOB_STATE_DONE
        verdict = (await _assessments(harness))[0]
        # Stale A (old fencing token) finally runs: it must write nothing.
        out = await worker_a._process_job(job_a)
        assert out.state == JOB_STATE_RUNNING  # unchanged local view, no write
        final = await _job(harness, job.id)
        assert final.state == JOB_STATE_DONE and final.lease_owner is None
        rows = await _assessments(harness)
        assert len(rows) == 1
        assert rows[0].processing_state == verdict.processing_state
        assert rows[0].disposition == verdict.disposition

    async def test_stale_incomplete_cannot_downgrade_complete_verdict(
        self, harness, monkeypatch
    ) -> None:
        job = await _one_job(harness, monkeypatch)
        clock = Clock()
        w = _worker(harness, clock, worker_id="A")
        await w.process_one()
        assert (await _assessments(harness))[0].processing_state == ProcessingState.ASSESSED.value
        await w.record_incomplete(await _job(harness, job.id), CODE_LEASE_EXPIRED)
        rows = await _assessments(harness)
        assert len(rows) == 1 and rows[0].processing_state == ProcessingState.ASSESSED.value


# ---------------------------------------------------------------------------
# timeout (I-02, T-18)
# ---------------------------------------------------------------------------


class TestTimeout:
    async def test_lease_expiring_mid_processing_is_retryable_and_incomplete(
        self, harness, monkeypatch
    ) -> None:
        job = await _one_job(harness, monkeypatch)
        clock = Clock()
        inner = ObjectStoreContentResolver(object_store=harness.store)

        class _SlowResolver:
            async def resolve(self, *, account_scope: str, content_ref: str) -> bytes | None:
                clock.advance(_POLICY.lease_seconds + 5)  # lease lapses mid-job
                return await inner.resolve(account_scope=account_scope, content_ref=content_ref)

        w = _worker(harness, clock, worker_id="A", content_resolver=_SlowResolver())
        out = await w.process_one()
        assert out is not None
        assert out.state == JOB_STATE_ERROR_RETRYABLE
        assert out.last_error_code == CODE_LEASE_EXPIRED
        row = (await _assessments(harness))[0]
        assert row.processing_state == ProcessingState.INCOMPLETE.value
        assert row.disposition in (
            Disposition.INCOMPLETE.value, Disposition.REVIEW.value, Disposition.RESTRICTED.value
        )
        assert (await _job(harness, job.id)).state != JOB_STATE_DONE


# ---------------------------------------------------------------------------
# bounded retry, dead letter, manual replay (R-018, T-18)
# ---------------------------------------------------------------------------


class TestRetryDeadLetterReplay:
    async def _dead_letter(self, harness, monkeypatch, clock: Clock) -> tuple[Any, dict]:
        job = await _one_job(harness, monkeypatch)
        saved = dict(harness.store._objects)
        harness.store._objects.clear()
        w = _worker(harness, clock, worker_id="A")
        first = await w.process_one()
        assert first is not None and first.state == JOB_STATE_ERROR_RETRYABLE
        assert await w.process_one() is None  # backoff not elapsed
        clock.advance(_POLICY.backoff_delay(1) + 1)
        second = await w.process_one()
        assert second is not None and second.state == JOB_STATE_ERROR_RETRYABLE
        clock.advance(_POLICY.backoff_delay(2) + 1)
        third = await w.process_one()
        assert third is not None and third.attempts == 3
        return job, saved

    async def test_ceiling_exceeded_dead_letters_with_safe_code(
        self, harness, monkeypatch
    ) -> None:
        clock = Clock()
        job, _ = await self._dead_letter(harness, monkeypatch, clock)
        dead = await _job(harness, job.id)
        assert dead.state == JOB_STATE_DEAD_LETTER
        assert dead.last_error_code == "CONTENT_UNRESOLVED"
        assert dead.last_error_code.isupper() or "_" in dead.last_error_code
        row = (await _assessments(harness))[0]
        assert row.processing_state == ProcessingState.INCOMPLETE.value
        assert row.disposition != Disposition.SAFE_TO_PROCESS.value
        # Never retried automatically.
        clock.advance(10_000)
        assert await _worker(harness, clock, worker_id="Z").process_one() is None

    async def test_manual_replay_resets_and_reprocesses_once_keeping_lineage(
        self, harness, monkeypatch
    ) -> None:
        clock = Clock()
        job, saved = await self._dead_letter(harness, monkeypatch, clock)
        repo = harness.registry.mail_security
        aid = (await _assessments(harness))[0].assessment_id
        harness.store._objects.update(saved)  # content available again

        assert await replay_dead_letter(repo, "conn:other", job.id, now=clock()) is None
        replayed = await replay_dead_letter(repo, harness.scope, job.id, now=clock())
        assert replayed is not None
        assert replayed.id == job.id and replayed.outbox_job_key == job.outbox_job_key
        assert replayed.state == JOB_STATE_PENDING and replayed.attempts == 0
        assert replayed.meta["replay_count"] == 1
        hist = replayed.meta["replays"][0]
        assert hist["prior_attempts"] == 3 and hist["prior_error_code"] == "CONTENT_UNRESOLVED"
        # Replay only works from DEAD_LETTER.
        assert await replay_dead_letter(repo, harness.scope, job.id, now=clock()) is None

        done = await _worker(harness, clock, worker_id="R").process_one()
        assert done is not None and done.state == JOB_STATE_DONE
        rows = await _assessments(harness)
        assert len(rows) == 1 and rows[0].assessment_id == aid
        assert rows[0].processing_state == ProcessingState.ASSESSED.value
        assert (await _job(harness, job.id)).meta["replay_count"] == 1
        assert await _worker(harness, clock, worker_id="R").process_one() is None


# ---------------------------------------------------------------------------
# sweeper: missing jobs, isolation, attention (R-018, R-023, I-07)
# ---------------------------------------------------------------------------

async def _sweep(repo: Any, scope: str, provider: Any) -> Any:
    return await sweep_once(
        repo, scope, now=_T0, policy=_POLICY, sources_provider=provider
    )


_SRC = {
    "provider": "imap",
    "provider_uid": "m-1",
    "rfc_message_id": "<m-1@corp>",
    "folder": "INBOX",
    "content_ref": "content://obj-1",
    "flags": {"bulk": False},
}


class TestSweeper:
    async def test_creates_missing_job_once_and_second_sweep_creates_nothing(
        self, harness
    ) -> None:
        repo = harness.registry.mail_security
        calls: list[str] = []

        async def provider(scope: str) -> list[dict[str, Any]]:
            calls.append(scope)
            return [dict(_SRC)]

        first = await _sweep(repo, harness.scope, provider)
        second = await _sweep(repo, harness.scope, provider)
        assert len(first.created) == 1 and second.created == []
        jobs = await repo.list_jobs(harness.scope)
        assert len(jobs) == 1 and jobs[0].state == JOB_STATE_PENDING
        assert calls == [harness.scope, harness.scope]
        # No side effects beyond the job row.
        assert await _assessments(harness) == []
        from backend.persistence.connection import make_session_factory

        async with make_session_factory(harness.engine)() as s:
            assert list((await s.execute(select(AttentionItemRow))).scalars().all()) == []

    async def test_sweeper_does_not_duplicate_an_ingested_job(
        self, harness, monkeypatch
    ) -> None:
        await H.ingest(harness, monkeypatch, H.mailitem("<dup@corp>", H.benign_mime()))
        repo = harness.registry.mail_security
        existing = await repo.list_jobs(harness.scope)
        assert len(existing) == 1
        src = {
            "provider": "imap", "provider_uid": "<dup@corp>",
            "rfc_message_id": "<dup@corp>", "folder": "INBOX",
        }

        async def provider(_scope: str) -> list[dict[str, Any]]:
            return [src]

        res = await _sweep(repo, harness.scope, provider)
        assert len(await repo.list_jobs(harness.scope)) == 1
        assert res.created == []

    async def test_sweeper_never_crosses_account_scopes(self, harness) -> None:
        repo = harness.registry.mail_security
        other = "conn:other-account"
        stuck = await repo.upsert_job(
            account_scope=other, outbox_job_key="k-other", source_key="s-other",
            analysis_profile_version=DEFAULT_ANALYSIS_PROFILE_VERSION,
        )
        await repo.mark_job_state(
            other, stuck.id, state=JOB_STATE_RUNNING, attempts=1,
            lease_owner="X", lease_expires_at=_T0 - timedelta(seconds=999),
        )

        async def provider(_scope: str) -> list[dict[str, Any]]:
            return [dict(_SRC)]

        res = await _sweep(repo, harness.scope, provider)
        assert res.reclaimed == []
        untouched = await repo.get_job(other, stuck.id)
        assert untouched.state == JOB_STATE_RUNNING and untouched.lease_owner == "X"
        assert len(await repo.list_jobs(other)) == 1  # no job created there
        assert len(await repo.list_jobs(harness.scope)) == 1
        # And the scoped claim never leases another scope's job.
        assert await claim_next_job(
            repo, harness.scope, worker_id="A", now=_T0 + timedelta(days=1), policy=_POLICY
        ) is not None
        assert (await repo.get_job(other, stuck.id)).lease_owner == "X"


# ---------------------------------------------------------------------------
# runner flag gating (default OFF)
# ---------------------------------------------------------------------------


class TestRunnerFlag:
    async def test_flag_off_runner_does_nothing(self, harness, monkeypatch) -> None:
        job = await _one_job(harness, monkeypatch)
        monkeypatch.delenv(ENV_MAIL_SECURITY_ENABLED, raising=False)
        repo = harness.registry.mail_security
        clock = Clock()
        await claim_next_job(repo, harness.scope, worker_id="A", now=clock(), policy=_POLICY)
        clock.advance(_POLICY.lease_seconds + 1)

        async def provider(_s: str) -> list[dict[str, Any]]:
            return [dict(_SRC)]

        runner = MailSecurityQueueRunner(
            repository=repo, account_scope=harness.scope, policy=_POLICY,
            sources_provider=provider, clock=clock, env={},
            worker=_worker(harness, clock, worker_id="W"),
        )
        assert runner.enabled is False
        assert await runner.run_once() is None
        assert runner.start() is None
        state = await _job(harness, job.id)
        assert state.state == JOB_STATE_RUNNING and state.lease_owner == "A"
        assert len(await repo.list_jobs(harness.scope)) == 1
        assert await _assessments(harness) == []

    async def test_flag_on_runner_sweeps_then_drains(self, harness, monkeypatch) -> None:
        job = await _one_job(harness, monkeypatch)
        repo = harness.registry.mail_security
        clock = Clock()
        await claim_next_job(repo, harness.scope, worker_id="A", now=clock(), policy=_POLICY)
        clock.advance(_POLICY.lease_seconds + 1)
        w = _worker(harness, clock, worker_id="W")
        runner = MailSecurityQueueRunner(
            repository=repo, account_scope=harness.scope, policy=_POLICY,
            clock=clock, env=_ON, worker=w,
        )
        res = await runner.run_once()
        assert res is not None and res.reclaimed == [job.id]
        clock.advance(_POLICY.backoff_delay(1) + 1)
        await runner.run_once()
        assert (await _job(harness, job.id)).state == JOB_STATE_DONE
        assert len(await _assessments(harness)) == 1


# ---------------------------------------------------------------------------
# privacy (T-14): canary absent from job rows, events and logs
# ---------------------------------------------------------------------------


class TestPrivacy:
    async def test_canary_absent_from_jobs_and_logs(self, harness, monkeypatch) -> None:
        with structlog.testing.capture_logs() as logs:
            job = await _one_job(harness, monkeypatch, "<canary@corp>")
            clock = Clock()
            repo = harness.registry.mail_security
            saved = dict(harness.store._objects)
            harness.store._objects.clear()
            w = _worker(harness, clock, worker_id="A")
            await w.process_one()  # failure path
            harness.store._objects.update(saved)
            await claim_next_job(
                repo, harness.scope, worker_id="C", now=clock() + timedelta(days=1),
                policy=_POLICY,
            )
            clock.advance(10_000_000)
            await sweep_once(
                repo, harness.scope, now=clock(), policy=_POLICY,
                incomplete_recorder=w.record_incomplete,
            )
            clock.advance(100_000)
            await w.process_one()
            await replay_dead_letter(repo, harness.scope, job.id, now=clock())
        rows = await repo.list_jobs(harness.scope)
        blob = repr(
            [
                (r.state, r.last_error_code, r.lease_owner, r.meta, r.source_key,
                 r.outbox_job_key)
                for r in rows
            ]
        ) + repr(logs)
        assert H.CANARY not in blob
        assert H.ACCOUNT not in blob
