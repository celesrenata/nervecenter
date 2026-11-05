"""Fenced verdict-write path (G2 gap, Item 1; R-018, I-02, I-11, T-16, T-17).

The fencing token ``(lease_owner, attempts)`` is compared INSIDE the same
database transaction as the evidence/assessment/findings writes and the job
transition (``MailSecurityRepository.commit_verdict_fenced``). These tests
drive THAT write path directly (not the pre-check that used to precede it):
claim by A, lease expiry, reclaim by B, then A attempts the write -- both
while B still holds the lease and after B finished.

Proven on SQLite only (single-writer serialization). Not proven here:
Postgres READ COMMITTED row-lock interleaving (see the commit message).
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import _mail_security_harness_helpers as H
import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import create_async_engine

from backend.mail_security.ingestion_seam import (
    DEFAULT_ANALYSIS_PROFILE_VERSION,
    ENV_MAIL_SECURITY_ENABLED,
)
from backend.mail_security.models import ProcessingState
from backend.mail_security.queue import (
    JOB_STATE_DONE,
    JOB_STATE_ERROR_RETRYABLE,
    JOB_STATE_PENDING,
    JOB_STATE_RUNNING,
    QueuePolicy,
    claim_next_job,
)
from backend.mail_security.worker import MailSecurityWorker, ObjectStoreContentResolver
from backend.persistence.connection import make_session_factory
from backend.persistence.models.mail_security import (
    MailEvidenceRow,
    MailSecurityAssessmentRow,
    MailSecurityFindingRow,
)
from backend.persistence.repositories.mail_security_repo import (
    MailSecurityRepository,
    VerdictWriteStatus,
)

_T0 = datetime(2026, 10, 27, 10, 0, 0, tzinfo=UTC)
_ON = {ENV_MAIL_SECURITY_ENABLED: "1"}
_POLICY = QueuePolicy(max_attempts=3, lease_seconds=60, base_delay_seconds=30.0)


class Clock:
    def __init__(self) -> None:
        self.now = _T0

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)


@pytest.fixture
async def harness(tmp_path: Path):
    h = await H.make_harness(tmp_path / "fence.sqlite")
    yield h
    await H.teardown_harness(h)


def _worker(harness, clock: Clock, worker_id: str) -> MailSecurityWorker:
    return MailSecurityWorker(
        repository=harness.registry.mail_security,
        content_resolver=ObjectStoreContentResolver(object_store=harness.store),
        account_scope=harness.scope,
        analysis_profile_version=DEFAULT_ANALYSIS_PROFILE_VERSION,
        clock=clock,
        env=_ON,
        worker_id=worker_id,
        max_attempts=_POLICY.max_attempts,
        lease_seconds=_POLICY.lease_seconds,
    )


async def _counts(harness) -> tuple[int, int, int]:
    async with make_session_factory(harness.engine)() as session:
        out = []
        for model in (MailEvidenceRow, MailSecurityAssessmentRow, MailSecurityFindingRow):
            out.append(len((await session.execute(select(model))).scalars().all()))
    return out[0], out[1], out[2]


async def _scenario(harness, monkeypatch):
    """A claims, lease expires, B reclaims. Returns (clock, a, job_a, b, job_b)."""
    await H.ingest(harness, monkeypatch, H.mailitem("<f1@corp>", H.benign_mime()))
    repo = harness.registry.mail_security
    clock = Clock()
    a, b = _worker(harness, clock, "A"), _worker(harness, clock, "B")
    job_a = await claim_next_job(
        repo, harness.scope, worker_id="A", now=clock(), policy=_POLICY
    )
    assert job_a is not None and job_a.attempts == 1
    clock.advance(_POLICY.lease_seconds + 1)
    job_b = await claim_next_job(
        repo, harness.scope, worker_id="B", now=clock(), policy=_POLICY
    )
    assert job_b is not None and job_b.attempts == 2
    return clock, a, job_a, b, job_b


def _complete(worker: MailSecurityWorker, job: Any):
    ref = str((job.meta or {}).get("content_ref") or "") or None
    evidence = worker._scan(H.benign_mime())
    assessment = worker._assess(
        account_scope=job.account_scope,
        source_key=job.source_key,
        content_ref=ref,
        evidence=evidence,
        flags={},
    )
    return ref, evidence, assessment


class TestStaleWriterRejectedAtomically:
    async def test_a_writes_after_b_reclaimed_is_rejected_nothing_written(
        self, harness, monkeypatch
    ) -> None:
        _, a, job_a, _b, job_b = await _scenario(harness, monkeypatch)
        ref, evidence, assessment = _complete(a, job_a)

        out = await a._commit_verdict(
            job_a, content_ref=ref, evidence=evidence, assessment=assessment,
            state=JOB_STATE_DONE, lease_expires_at=None, last_error_code=None,
        )

        assert out.status is VerdictWriteStatus.STALE_FENCE and out.job is None
        assert await _counts(harness) == (0, 0, 0)  # no partial rows
        held = await harness.registry.mail_security.get_job(harness.scope, job_b.id)
        assert held.state == JOB_STATE_RUNNING
        assert held.lease_owner == "B" and held.attempts == 2  # no forged completion

    async def test_a_failure_transition_after_reclaim_is_rejected(
        self, harness, monkeypatch
    ) -> None:
        _, a, job_a, _b, job_b = await _scenario(harness, monkeypatch)
        ref, evidence, assessment = _complete(a, job_a)
        incomplete = a._assess_incomplete(
            account_scope=job_a.account_scope, source_key=job_a.source_key,
            content_ref=ref, evidence=None, code="SCAN_FAILED",
        )
        out = await a._commit_verdict(
            job_a, content_ref=ref, evidence=None, assessment=incomplete,
            state=JOB_STATE_ERROR_RETRYABLE, lease_expires_at=None,
            last_error_code="SCAN_FAILED",
        )
        assert not out.committed
        assert await _counts(harness) == (0, 0, 0)
        held = await harness.registry.mail_security.get_job(harness.scope, job_b.id)
        assert held.state == JOB_STATE_RUNNING and held.lease_owner == "B"
        assert held.last_error_code is None

    async def test_a_writes_after_b_finished_is_rejected_and_b_result_stands(
        self, harness, monkeypatch
    ) -> None:
        _, a, job_a, b, job_b = await _scenario(harness, monkeypatch)
        ref, evidence_b, assessment_b = _complete(b, job_b)
        done = await b._commit_verdict(
            job_b, content_ref=ref, evidence=evidence_b, assessment=assessment_b,
            state=JOB_STATE_DONE, lease_expires_at=None, last_error_code=None,
        )
        assert done.committed and done.job.state == JOB_STATE_DONE
        before = await _counts(harness)
        assert before[0] == 1 and before[1] == 1
        repo = harness.registry.mail_security
        stored = await repo.get_assessment(harness.scope, assessment_b.assessment_id)
        snapshot = dict(stored.assessment)

        # A: a complete verdict write, then an INCOMPLETE one, both stale.
        _, evidence_a, assessment_a = _complete(a, job_a)
        incomplete = a._assess_incomplete(
            account_scope=job_a.account_scope, source_key=job_a.source_key,
            content_ref=ref, evidence=None, code="LEASE_EXPIRED",
        )
        for asm, ev, state in (
            (assessment_a, evidence_a, JOB_STATE_DONE),
            (incomplete, None, JOB_STATE_ERROR_RETRYABLE),
        ):
            out = await a._commit_verdict(
                job_a, content_ref=ref, evidence=ev, assessment=asm,
                state=state, lease_expires_at=None, last_error_code="X",
            )
            assert out.status is VerdictWriteStatus.STALE_FENCE

        assert await _counts(harness) == before  # no duplicate / partial rows
        again = await repo.get_assessment(harness.scope, assessment_b.assessment_id)
        assert again.assessment == snapshot  # B's result untouched
        assert again.processing_state == ProcessingState.ASSESSED.value
        final = await repo.get_job(harness.scope, job_b.id)
        assert final.state == JOB_STATE_DONE and final.last_error_code is None

    async def test_process_job_by_stale_worker_writes_nothing(
        self, harness, monkeypatch
    ) -> None:
        """End to end through the worker: the stale run returns its local view
        and the verdict path leaves the database untouched."""
        _, a, job_a, _b, job_b = await _scenario(harness, monkeypatch)
        out = await a._process_job(job_a)
        assert out.state == JOB_STATE_RUNNING
        assert await _counts(harness) == (0, 0, 0)
        held = await harness.registry.mail_security.get_job(harness.scope, job_b.id)
        assert held.lease_owner == "B" and held.state == JOB_STATE_RUNNING

    async def test_live_owner_commits_verdict_and_ack_together(
        self, harness, monkeypatch
    ) -> None:
        _, _a, _job_a, b, job_b = await _scenario(harness, monkeypatch)
        ref, evidence, assessment = _complete(b, job_b)
        out = await b._commit_verdict(
            job_b, content_ref=ref, evidence=evidence, assessment=assessment,
            state=JOB_STATE_DONE, lease_expires_at=None, last_error_code=None,
        )
        assert out.committed and out.job.state == JOB_STATE_DONE
        assert out.job.lease_owner is None
        ev, asm, fnd = await _counts(harness)
        assert (ev, asm) == (1, 1) and fnd == len(assessment.findings)

    async def test_idempotent_replay_and_no_incomplete_downgrade(
        self, harness, monkeypatch
    ) -> None:
        _, _a, _job_a, b, job_b = await _scenario(harness, monkeypatch)
        repo = harness.registry.mail_security
        ref, evidence, assessment = _complete(b, job_b)
        # Same owner writes the complete verdict, then (still a valid token is
        # impossible after DONE) replays through a fresh claim on the same row.
        assert (await b._commit_verdict(
            job_b, content_ref=ref, evidence=evidence, assessment=assessment,
            state=JOB_STATE_DONE, lease_expires_at=None, last_error_code=None,
        )).committed
        await repo.mark_job_state(
            harness.scope, job_b.id, state=JOB_STATE_RUNNING, attempts=3,
            lease_owner="B", lease_expires_at=_T0 + timedelta(days=1),
        )
        replay_job = await repo.get_job(harness.scope, job_b.id)
        incomplete = b._assess_incomplete(
            account_scope=replay_job.account_scope, source_key=replay_job.source_key,
            content_ref=ref, evidence=None, code="SCAN_FAILED",
        )
        out = await b._commit_verdict(
            replay_job, content_ref=ref, evidence=None, assessment=incomplete,
            state=JOB_STATE_ERROR_RETRYABLE, lease_expires_at=None,
            last_error_code="SCAN_FAILED",
        )
        assert out.committed  # live owner: job moves, but the verdict holds
        rows = await repo.list_assessments(harness.scope)
        assert len(rows) == 1
        assert rows[0].processing_state == ProcessingState.ASSESSED.value


class TestTransactionAtomicity:
    async def test_failure_after_fence_rolls_back_every_write(
        self, harness, monkeypatch
    ) -> None:
        """If anything fails after the fence passes (here the assessment
        upsert), the evidence row and job transition roll back too."""
        _, _a, _job_a, b, job_b = await _scenario(harness, monkeypatch)
        repo = harness.registry.mail_security
        ref, evidence, assessment = _complete(b, job_b)

        async def boom(*_a: Any, **_k: Any) -> Any:
            raise RuntimeError("injected")

        monkeypatch.setattr(MailSecurityRepository, "_upsert_assessment_in", boom)
        with pytest.raises(RuntimeError):
            await b._commit_verdict(
                job_b, content_ref=ref, evidence=evidence, assessment=assessment,
                state=JOB_STATE_DONE, lease_expires_at=None, last_error_code=None,
            )
        assert await _counts(harness) == (0, 0, 0)
        held = await repo.get_job(harness.scope, job_b.id)
        assert held.state == JOB_STATE_RUNNING and held.lease_owner == "B"

    async def test_cross_scope_verdict_is_refused(self, harness, monkeypatch) -> None:
        _, _a, _job_a, b, job_b = await _scenario(harness, monkeypatch)
        ref, evidence, assessment = _complete(b, job_b)
        manifest = b._build_manifest(
            account_scope=job_b.account_scope, source_key=job_b.source_key,
            content_ref=ref, evidence=evidence,
            evidence_fidelity=assessment.evidence_fidelity,
        )
        with pytest.raises(ValueError):
            await harness.registry.mail_security.commit_verdict_fenced(
                "conn:other-scope", job_b.id, fence_owner="B", fence_attempts=2,
                manifest=manifest, assessment=assessment, state=JOB_STATE_DONE,
            )
        assert await _counts(harness) == (0, 0, 0)

    async def test_verdict_statements_precede_ack_in_one_transaction(
        self, harness, monkeypatch
    ) -> None:
        """Statement order inside the single transaction: fence assert, then
        evidence + assessment writes, then the DONE transition, one COMMIT."""
        _, _a, _job_a, b, job_b = await _scenario(harness, monkeypatch)
        ref, evidence, assessment = _complete(b, job_b)
        seen: list[str] = []

        def on_exec(_c, _cur, statement, *_rest) -> None:
            head = statement.strip().split("(")[0].split(" WHERE")[0].lower()
            seen.append(head[:60])

        sync_engine = harness.engine.sync_engine
        committed: list[int] = []
        event.listen(sync_engine, "before_cursor_execute", on_exec)
        event.listen(sync_engine, "commit", lambda _c: committed.append(len(seen)))
        try:
            out = await b._commit_verdict(
                job_b, content_ref=ref, evidence=evidence, assessment=assessment,
                state=JOB_STATE_DONE, lease_expires_at=None, last_error_code=None,
            )
        finally:
            event.remove(sync_engine, "before_cursor_execute", on_exec)
        assert out.committed
        writes = [s for s in seen if s.startswith(("insert", "update"))]
        verdict_idx = max(i for i, s in enumerate(writes) if s.startswith("insert"))
        job_updates = [i for i, s in enumerate(writes) if s.startswith("update")]
        assert job_updates[0] < min(
            i for i, s in enumerate(writes) if s.startswith("insert")
        )  # fence first
        assert job_updates[-1] > verdict_idx  # ack after the verdict writes
        assert len(committed) == 1  # exactly one commit covers all of it


class TestRacingWriters:
    async def test_concurrent_reclaim_and_stale_commit_never_both_win(
        self, tmp_path: Path
    ) -> None:
        """A's fenced commit races B's reclaim on REAL separate SQLite
        connections (the harness' StaticPool shares one connection and so
        cannot interleave). SQLite serializes the conditional UPDATEs; exactly
        one outcome is consistent, in either order."""
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'race.sqlite'}",
            connect_args={"timeout": 30},
        )
        async with engine.begin() as conn:
            await conn.run_sync(
                lambda sc: H.TABLES[0].metadata.create_all(sc, tables=H.TABLES)
            )
        repo = MailSecurityRepository(make_session_factory(engine))
        scope = "conn:race-scope"
        await repo.upsert_job(
            account_scope=scope, outbox_job_key="k-race", source_key="src:race:1",
            analysis_profile_version=DEFAULT_ANALYSIS_PROFILE_VERSION,
            state=JOB_STATE_PENDING, meta={"content_ref": "content://race"},
        )
        clock = Clock()
        a = MailSecurityWorker(
            repository=repo,
            content_resolver=ObjectStoreContentResolver(object_store=None),
            account_scope=scope,
            analysis_profile_version=DEFAULT_ANALYSIS_PROFILE_VERSION,
            clock=clock, env=_ON, worker_id="A",
            max_attempts=_POLICY.max_attempts, lease_seconds=_POLICY.lease_seconds,
        )
        try:
            job_a = await claim_next_job(
                repo, scope, worker_id="A", now=clock(), policy=_POLICY
            )
            assert job_a is not None
            ref, evidence, assessment = _complete(a, job_a)
            clock.advance(_POLICY.lease_seconds + 1)  # A's lease is reclaimable

            out, claimed = await asyncio.gather(
                a._commit_verdict(
                    job_a, content_ref=ref, evidence=evidence, assessment=assessment,
                    state=JOB_STATE_DONE, lease_expires_at=None, last_error_code=None,
                ),
                claim_next_job(repo, scope, worker_id="B", now=clock(), policy=_POLICY),
            )
            final = await repo.get_job(scope, job_a.id)
            n_asm = len(await repo.list_assessments(scope))
            if out.committed:  # A won: job done, nothing left for B to claim
                assert claimed is None
                assert final.state == JOB_STATE_DONE and n_asm == 1
            else:  # B won: A's write left no trace; B holds the new lease
                assert claimed is not None and claimed.attempts == 2
                assert final.state == JOB_STATE_RUNNING and final.lease_owner == "B"
                assert n_asm == 0
        finally:
            await engine.dispose()
