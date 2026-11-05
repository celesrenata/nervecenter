"""Postgres concurrency evidence for the job claim and verdict fence (G2 gap, Item 3).

R-018, I-02, I-11, T-16, T-17, T-19. The atomic claim and the fenced verdict
write were previously proven on scratch SQLite only. This module re-runs the
guarantees on a real Postgres (READ COMMITTED, row-level locks, true
concurrent connections).

It is skipped unless ``NERVE_TEST_POSTGRES_URL`` points at a SCRATCH database
(``postgresql+asyncpg://user:pass@host:port/dbname``). The database is
migrated 016/017 up and down by the test itself and left at revision 015.
Never point it at a database holding real data.
"""

from __future__ import annotations

import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Any

import _mail_security_harness_helpers as H
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from backend.mail_security.ingestion_seam import (
    DEFAULT_ANALYSIS_PROFILE_VERSION,
    ENV_MAIL_SECURITY_ENABLED,
)
from backend.mail_security.queue import (
    JOB_STATE_DONE,
    JOB_STATE_PENDING,
    JOB_STATE_RUNNING,
    QueuePolicy,
    claim_next_job,
)
from backend.mail_security.worker import MailSecurityWorker, ObjectStoreContentResolver
from backend.persistence.connection import make_session_factory
from backend.persistence.repositories.mail_security_repo import (
    MailSecurityRepository,
    VerdictWriteStatus,
)

ENV_URL = "NERVE_TEST_POSTGRES_URL"
PG_URL = os.environ.get(ENV_URL, "")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not PG_URL.startswith("postgresql"),
        reason=f"{ENV_URL} not set to a scratch Postgres URL",
    ),
]

_T0 = datetime(2026, 10, 27, 10, 0, 0, tzinfo=UTC)
_POLICY = QueuePolicy(max_attempts=3, lease_seconds=60, base_delay_seconds=30.0)
_SCOPE = "conn:pg-scope"
_MIGRATED_TABLES = (
    "mail_security_jobs",
    "mail_evidence",
    "mail_security_assessments",
    "mail_security_findings",
)
_INTEL_REV = "017_mail_intel"
_BASE_REV = "015_inference_providers"


def _alembic_config() -> Config:
    from backend.persistence import migrate

    config = Config()
    config.set_main_option("script_location", str(migrate._script_location()))
    config.set_main_option("sqlalchemy.url", PG_URL)
    return config


def _alembic(action: Any, config: Config, revision: str) -> None:
    """Run an Alembic command on its own thread.

    env.py drives asyncpg URLs with ``asyncio.run``, which cannot nest inside
    the running pytest-asyncio loop.
    """
    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(action, config, revision).result()


async def _table_names() -> set[str]:
    engine = create_async_engine(PG_URL, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            return set(await conn.run_sync(lambda sc: inspect(sc).get_table_names()))
    finally:
        await engine.dispose()


@pytest.fixture(scope="module")
def migrated() -> Any:
    """Up to 017, hand the DB to the tests, then downgrade back to 015."""
    config = _alembic_config()
    _alembic(command.upgrade, config, _INTEL_REV)
    try:
        yield config
    finally:
        _alembic(command.downgrade, config, _BASE_REV)


class TestMigrations:
    async def test_016_017_apply_and_downgrade_cleanly(self) -> None:
        config = _alembic_config()
        _alembic(command.upgrade, config, _INTEL_REV)
        assert set(_MIGRATED_TABLES) <= await _table_names()
        _alembic(command.downgrade, config, _BASE_REV)
        remaining = await _table_names()
        assert not set(_MIGRATED_TABLES) & remaining
        assert not {t for t in remaining if t.startswith("mail_intel")}
        _alembic(command.upgrade, config, _INTEL_REV)  # re-apply: idempotent round trip
        assert set(_MIGRATED_TABLES) <= await _table_names()
        _alembic(command.downgrade, config, _BASE_REV)


@pytest.fixture
async def repo(migrated: Any):
    engine = create_async_engine(PG_URL, pool_size=16, max_overflow=0)
    repository = MailSecurityRepository(make_session_factory(engine))
    try:
        yield repository
    finally:
        async with engine.begin() as conn:
            for table in (
                "mail_security_findings",
                "mail_security_assessments",
                "mail_evidence",
                "mail_security_jobs",
            ):
                await conn.execute(
                    text(f"DELETE FROM {table} WHERE account_scope = :s"),  # noqa: S608
                    {"s": _SCOPE},
                )
        await engine.dispose()


async def _pending_job(repo: MailSecurityRepository, key: str):
    return await repo.upsert_job(
        account_scope=_SCOPE,
        outbox_job_key=key,
        source_key=f"src:{key}",
        analysis_profile_version=DEFAULT_ANALYSIS_PROFILE_VERSION,
        state=JOB_STATE_PENDING,
        meta={"content_ref": f"content://{key}"},
    )


def _worker(repo: MailSecurityRepository, worker_id: str, clock: Any) -> MailSecurityWorker:
    return MailSecurityWorker(
        repository=repo,
        content_resolver=ObjectStoreContentResolver(object_store=None),
        account_scope=_SCOPE,
        analysis_profile_version=DEFAULT_ANALYSIS_PROFILE_VERSION,
        clock=clock,
        env={ENV_MAIL_SECURITY_ENABLED: "1"},
        worker_id=worker_id,
        max_attempts=_POLICY.max_attempts,
        lease_seconds=_POLICY.lease_seconds,
    )


class _Clock:
    def __init__(self) -> None:
        self.now = _T0

    def __call__(self) -> datetime:
        return self.now


def _verdict(worker: MailSecurityWorker, job: Any):
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


class TestAtomicClaim:
    @pytest.mark.parametrize("workers", [2, 8, 16])
    async def test_exactly_one_winner_across_separate_connections(
        self, repo: MailSecurityRepository, workers: int
    ) -> None:
        job = await _pending_job(repo, f"k-claim-{workers}")
        results = await asyncio.gather(
            *(
                claim_next_job(
                    repo, _SCOPE, worker_id=f"W{i}", now=_T0, policy=_POLICY
                )
                for i in range(workers)
            )
        )
        winners = [r for r in results if r is not None]
        assert len(winners) == 1
        held = await repo.get_job(_SCOPE, job.id)
        assert held.state == JOB_STATE_RUNNING and held.attempts == 1
        assert held.lease_owner == winners[0].lease_owner

    async def test_direct_conditional_update_has_one_winner(
        self, repo: MailSecurityRepository
    ) -> None:
        """The CAS itself (no candidate listing): N identical guarded UPDATEs."""
        job = await _pending_job(repo, "k-cas")
        results = await asyncio.gather(
            *(
                repo.mark_job_state(
                    _SCOPE, job.id, state=JOB_STATE_RUNNING, attempts=1,
                    lease_owner=f"W{i}", lease_expires_at=_T0 + timedelta(seconds=60),
                    if_state=JOB_STATE_PENDING, if_attempts=0,
                )
                for i in range(12)
            )
        )
        assert sum(r is not None for r in results) == 1


class TestStaleWorkerVerdict:
    async def _claim_ab(self, repo: MailSecurityRepository, key: str):
        await _pending_job(repo, key)
        clock = _Clock()
        job_a = await claim_next_job(
            repo, _SCOPE, worker_id="A", now=clock(), policy=_POLICY
        )
        assert job_a is not None and job_a.attempts == 1
        clock.now += timedelta(seconds=_POLICY.lease_seconds + 1)
        job_b = await claim_next_job(
            repo, _SCOPE, worker_id="B", now=clock(), policy=_POLICY
        )
        assert job_b is not None and job_b.attempts == 2
        return clock, job_a, job_b

    async def test_stale_a_rejected_then_b_commits(
        self, repo: MailSecurityRepository
    ) -> None:
        clock, job_a, job_b = await self._claim_ab(repo, "k-stale")
        a, b = _worker(repo, "A", clock), _worker(repo, "B", clock)
        ref, evidence, assessment = _verdict(a, job_a)
        out = await a._commit_verdict(
            job_a, content_ref=ref, evidence=evidence, assessment=assessment,
            state=JOB_STATE_DONE, lease_expires_at=None, last_error_code=None,
        )
        assert out.status is VerdictWriteStatus.STALE_FENCE and out.job is None
        assert await repo.list_assessments(_SCOPE) == []
        held = await repo.get_job(_SCOPE, job_b.id)
        assert held.state == JOB_STATE_RUNNING and held.lease_owner == "B"
        ref, evidence, assessment = _verdict(b, job_b)
        done = await b._commit_verdict(
            job_b, content_ref=ref, evidence=evidence, assessment=assessment,
            state=JOB_STATE_DONE, lease_expires_at=None, last_error_code=None,
        )
        assert done.committed
        # A, still stale after B finished: rejected, B's single result stands.
        again = await a._commit_verdict(
            job_a, content_ref=ref, evidence=evidence, assessment=assessment,
            state=JOB_STATE_DONE, lease_expires_at=None, last_error_code=None,
        )
        assert again.status is VerdictWriteStatus.STALE_FENCE
        assert len(await repo.list_assessments(_SCOPE)) == 1

    async def test_concurrent_stale_commit_vs_reclaim_never_both_win(
        self, repo: MailSecurityRepository
    ) -> None:
        for i in range(5):
            await _pending_job(repo, f"k-race-{i}")
            clock = _Clock()
            a = _worker(repo, "A", clock)
            job_a = await claim_next_job(
                repo, _SCOPE, worker_id="A", now=clock(), policy=_POLICY
            )
            assert job_a is not None
            ref, evidence, assessment = _verdict(a, job_a)
            clock.now += timedelta(seconds=_POLICY.lease_seconds + 1)
            before = len(await repo.list_assessments(_SCOPE))
            out, claimed = await asyncio.gather(
                a._commit_verdict(
                    job_a, content_ref=ref, evidence=evidence, assessment=assessment,
                    state=JOB_STATE_DONE, lease_expires_at=None, last_error_code=None,
                ),
                claim_next_job(
                    repo, _SCOPE, worker_id="B", now=clock(), policy=_POLICY
                ),
            )
            final = await repo.get_job(_SCOPE, job_a.id)
            after = len(await repo.list_assessments(_SCOPE))
            if out.committed:
                assert final.state == JOB_STATE_DONE and after == before + 1
                assert claimed is None or claimed.id != job_a.id
            else:
                assert claimed is not None and claimed.id == job_a.id
                assert final.state == JOB_STATE_RUNNING and final.lease_owner == "B"
                assert after == before
            # Reset for the next iteration's claim selection.
            await repo.mark_job_state(
                _SCOPE, job_a.id, state=JOB_STATE_DONE, lease_owner=None
            )
