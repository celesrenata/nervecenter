"""NerveShield opt-in ingestion seam tests (Phase 1, Task 1.8).

Proves the Done criterion ("synthetic message triggers one job; retry/restart
produces no duplicate; existing attention unaffected in SHADOW") plus the
safety invariants the task fixes. The seam is an **opt-in, flag-gated** security
-job scheduler bolted beside the EXISTING worker / API ingest paths; with the
flag OFF it is a provably-inert no-op, and when ON a scheduling failure must
never perturb cursor advancement or the existing attention path.

Reuses the house durable harness (SQLite scratch DB + ``InMemoryObjectStore`` +
a real :class:`MailIngestWorker`, as in ``test_mail_ingest.py`` /
``test_mail_persistence.py``) — no real accounts, no network.

Cases
-----
1. FLAG OFF → seam is a no-op: a synthetic worker ingest produces NO
   ``mail_security_jobs`` row and the existing communication/attention outcome
   is identical to the flag-off baseline (R-001.4, T-29, I-03).
2. FLAG ON, one synthetic message → exactly ONE PENDING job with the correct
   account scope + deterministic Task 1.2 outbox key.
3. RETRY / RESTART / REPLAY → re-ingesting the same message yields NO duplicate
   job (idempotent on the outbox key — R-018.2, T-16).
4. EXISTING ATTENTION UNAFFECTED IN SHADOW → with the flag ON the durable
   ``attention_items`` mint is byte-identical to the flag-off baseline; the
   security job creates / alters / suppresses no attention item (T-26, R-001.4).
5. SCHEDULING FAILURE ISOLATION → if the job write raises, the ingest still
   completes and the cursor still advances exactly as in baseline; the message
   is left recoverable, never marked safe (R-001.3, I-07).
6. NO-SECRET / NO-RAW-BODY → a canary in the body / sender never appears in the
   persisted job row or the scheduler's log lines (T-14).
7. API path parity → the ``_schedule_security_job`` endpoint helper schedules
   one PENDING job when ON and nothing when OFF.
8. Reconcilable scanner → :func:`scan_missing_jobs` recreates a missing job
   from already-committed source identity and is idempotent.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import StaticPool

from backend.api.models import MailConnector  # noqa: F401 - import-order guard
from backend.communications import reset_communications_service
from backend.connectors.mail.mail_transport import MailItem, StubMailTransport
from backend.ingest.mail_worker import (
    DefaultIngestSeam,
    MailIngestWorker,
    set_communications_store,
)
from backend.mail_security.identifiers import logical_source_key, outbox_job_key
from backend.mail_security.ingestion_seam import (
    DEFAULT_ANALYSIS_PROFILE_VERSION,
    ENV_MAIL_SECURITY_ENABLED,
    JOB_STATE_PENDING,
    MailSecurityIngestionSeam,
    mail_security_enabled,
    scan_missing_jobs,
    set_mail_security_repository,
)
from backend.persistence.connection import make_session_factory
from backend.persistence.models.mail_security import (
    MailEvidenceRow,
    MailSecurityAssessmentRow,
    MailSecurityFindingRow,
    MailSecurityJobRow,
)
from backend.persistence.models.personal import (
    AttentionItemRow,
    CommunicationRow,
    ConnectionRow,
    ConversationRow,
    ImportJobRow,
    IngestCursorRow,
    PersonIdentityRow,
    PersonRow,
    SecretRow,
)
from backend.persistence.repositories import RepositoryRegistry
from backend.persistence.repositories.connections_repo import IdentitySecretCodec
from backend.persistence.repositories.mail_security_repo import (
    MailSecurityRepository,
)
from backend.storage.memory import InMemoryObjectStore

_ACCOUNT = "user@example.com"
_SECRET = "super-secret-password"
_CANARY = "CANARY-SECRET-dont-persist-me-9f3b2a"
_TS = datetime(2026, 10, 27, 9, 0, 0, tzinfo=UTC)

# The personal-plane tables the worker touches + the four security tables.
_TABLES = [
    ConnectionRow.__table__,
    SecretRow.__table__,
    IngestCursorRow.__table__,
    ImportJobRow.__table__,
    PersonRow.__table__,
    PersonIdentityRow.__table__,
    CommunicationRow.__table__,
    ConversationRow.__table__,
    AttentionItemRow.__table__,
    MailSecurityJobRow.__table__,
    MailEvidenceRow.__table__,
    MailSecurityAssessmentRow.__table__,
    MailSecurityFindingRow.__table__,
]


def _make_item(message_id: str, *, sender: str = "Boss <boss@corp.com>") -> MailItem:
    """A synthetic message whose body carries the canary (T-14)."""
    return MailItem(
        message_id=message_id,
        account=_ACCOUNT,
        subject=f"Please review {_CANARY}",
        content_bytes=f"Body with {_CANARY} inside\n".encode(),
        timestamp=_TS,
        headers={
            "From": sender,
            "Message-ID": message_id,
            "Date": "Mon, 27 Oct 2026 09:00:00 +0000",
        },
    )


async def _make_engine(db_path: Path) -> AsyncEngine:
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{db_path}",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(lambda sc: _TABLES[0].metadata.create_all(sc, tables=_TABLES))
    return engine


@pytest.fixture
async def harness(tmp_path: Path):
    """Durable SQLite registry + object store + one imap connection, with the
    durable communications store bound through the production seam (so the
    attention mirror reaches ``attention_items``)."""
    reset_communications_service()
    engine = await _make_engine(tmp_path / "seam.sqlite")
    registry = RepositoryRegistry(
        make_session_factory(engine), secret_codec=IdentitySecretCodec()
    )
    store = InMemoryObjectStore()
    conn = await registry.connections.create_connection(
        kind="imap",
        label="Work",
        config={"account": _ACCOUNT, "imap_host": "127.0.0.1", "mailbox": "INBOX"},
        auth_status="authenticated",
        operational_status="never_synced",
        enabled=True,
    )
    await registry.connections.upsert_secret(
        kind="app_password",
        owner_kind="connection",
        owner_id=conn.id,
        plaintext=_SECRET,
    )
    set_communications_store(registry.communications)
    yield SimpleNamespace(engine=engine, registry=registry, store=store, conn=conn)
    set_communications_store(None)
    set_mail_security_repository(None)
    reset_communications_service()
    await engine.dispose()


def _worker(harness, *, transport: Any, seam: Any | None = None) -> MailIngestWorker:
    return MailIngestWorker(
        connection_id=harness.conn.id,
        cursor_kind="imap_seq",
        connections=harness.registry.connections,
        imports=harness.registry.imports,
        object_store=harness.store,
        transport=transport,
        seam=seam
        if seam is not None
        else DefaultIngestSeam(communications_store=harness.registry.communications),
        poll_interval_s=30.0,
    )


def _enable_flag(monkeypatch) -> None:
    monkeypatch.setenv(ENV_MAIL_SECURITY_ENABLED, "1")


def _disable_flag(monkeypatch) -> None:
    monkeypatch.setenv(ENV_MAIL_SECURITY_ENABLED, "0")


async def _security_jobs(harness) -> list[MailSecurityJobRow]:
    return await harness.registry.mail_security.list_jobs(
        f"conn:{harness.conn.id}"
    )


async def _attention_rows(harness) -> list[AttentionItemRow]:
    async with harness.registry.session_factory() as session:
        from sqlalchemy import select

        result = await session.execute(select(AttentionItemRow))
        return list(result.scalars().all())


# ---------------------------------------------------------------------------
# flag helper
# ---------------------------------------------------------------------------


class TestFlag:
    def test_defaults_off(self) -> None:
        assert mail_security_enabled({}) is False
        assert mail_security_enabled({ENV_MAIL_SECURITY_ENABLED: ""}) is False
        assert mail_security_enabled({ENV_MAIL_SECURITY_ENABLED: "0"}) is False

    def test_on_when_set(self) -> None:
        assert mail_security_enabled({ENV_MAIL_SECURITY_ENABLED: "1"}) is True
        assert mail_security_enabled({ENV_MAIL_SECURITY_ENABLED: "true"}) is True


# ---------------------------------------------------------------------------
# 1. FLAG OFF → no-op (T-29, I-03, R-001.4)
# ---------------------------------------------------------------------------


class TestFlagOffIsNoOp:
    async def test_no_job_and_identical_outcome(self, harness, monkeypatch) -> None:
        _disable_flag(monkeypatch)
        # Even if a repo IS bound, the flag OFF keeps the seam inert.
        set_mail_security_repository(harness.registry.mail_security)
        transport = StubMailTransport(items=[_make_item("<1@corp>")], next_cursor={"uid": 5})
        worker = _worker(harness, transport=transport)
        await worker.poll_once()
        assert worker.status()["last"]["status"] == "ok"

        # No security job at all.
        assert await _security_jobs(harness) == []
        # The existing communication + attention outcome is intact.
        comms = await harness.registry.communications.list_communications()
        assert len(comms) == 1
        # Cursor advanced exactly as baseline.
        cursor = await harness.registry.imports.get_cursor(harness.conn.id, "imap_seq")
        assert cursor.cursor_value["uid"] == 5


# ---------------------------------------------------------------------------
# 2. FLAG ON → exactly one PENDING job with the right scope + deterministic key
# ---------------------------------------------------------------------------


class TestFlagOnSchedulesOne:
    async def test_single_pending_job(self, harness, monkeypatch) -> None:
        _enable_flag(monkeypatch)
        set_mail_security_repository(harness.registry.mail_security)
        transport = StubMailTransport(items=[_make_item("<1@corp>")], next_cursor={"uid": 5})
        worker = _worker(harness, transport=transport)
        await worker.poll_once()

        jobs = await _security_jobs(harness)
        assert len(jobs) == 1
        job = jobs[0]
        assert job.state == JOB_STATE_PENDING
        assert job.account_scope == f"conn:{harness.conn.id}"
        assert job.analysis_profile_version == DEFAULT_ANALYSIS_PROFILE_VERSION

        # The outbox key matches the deterministic Task 1.2 derivation.
        source_key = logical_source_key(
            f"conn:{harness.conn.id}",
            provider="imap",
            provider_uid="<1@corp>",
            rfc_message_id="<1@corp>",
            folder="INBOX",
        )
        expected_key = outbox_job_key(
            f"conn:{harness.conn.id}", source_key, DEFAULT_ANALYSIS_PROFILE_VERSION
        )
        assert job.outbox_job_key == expected_key
        assert job.source_key == source_key


# ---------------------------------------------------------------------------
# 3. RETRY / RESTART / REPLAY → no duplicate (R-018.2, T-16)
# ---------------------------------------------------------------------------


class TestIdempotentOnReplay:
    async def test_replay_no_duplicate(self, harness, monkeypatch) -> None:
        _enable_flag(monkeypatch)
        set_mail_security_repository(harness.registry.mail_security)
        # ReplayTransport-style: the same item is delivered on every poll.
        item = _make_item("<1@corp>")
        transport = StubMailTransport(items=[item], next_cursor={"uid": 5})

        # Poll three times (simulating retry / restart / replay).
        for _ in range(3):
            worker = _worker(harness, transport=transport)
            await worker.poll_once()

        jobs = await _security_jobs(harness)
        assert len(jobs) == 1  # stable identity, no duplicate


# ---------------------------------------------------------------------------
# 4. EXISTING ATTENTION UNAFFECTED IN SHADOW (T-26, R-001.4)
# ---------------------------------------------------------------------------


class TestAttentionUnaffected:
    async def _run_and_capture_attention(
        self, tmp_path: Path, monkeypatch, *, flag_on: bool
    ) -> tuple[list[dict[str, Any]], int]:
        reset_communications_service()
        engine = await _make_engine(tmp_path)
        registry = RepositoryRegistry(
            make_session_factory(engine), secret_codec=IdentitySecretCodec()
        )
        store = InMemoryObjectStore()
        conn = await registry.connections.create_connection(
            kind="imap",
            label="Work",
            config={"account": _ACCOUNT, "imap_host": "127.0.0.1", "mailbox": "INBOX"},
            auth_status="authenticated",
            operational_status="never_synced",
            enabled=True,
        )
        await registry.connections.upsert_secret(
            kind="app_password", owner_kind="connection", owner_id=conn.id, plaintext=_SECRET
        )
        set_communications_store(registry.communications)
        if flag_on:
            monkeypatch.setenv(ENV_MAIL_SECURITY_ENABLED, "1")
            set_mail_security_repository(registry.mail_security)
        else:
            monkeypatch.setenv(ENV_MAIL_SECURITY_ENABLED, "0")

        # A body that mints an attention item (an incoming request).
        item = MailItem(
            message_id="<attn@corp>",
            account=_ACCOUNT,
            subject="Can you send me the report?",
            content_bytes=b"Hi, can you please send me the quarterly report by Friday?\n",
            timestamp=_TS,
            headers={"From": "Boss <boss@corp.com>", "Message-ID": "<attn@corp>"},
        )
        transport = StubMailTransport(items=[item], next_cursor={"uid": 9})
        worker = MailIngestWorker(
            connection_id=conn.id,
            cursor_kind="imap_seq",
            connections=registry.connections,
            imports=registry.imports,
            object_store=store,
            transport=transport,
            seam=DefaultIngestSeam(communications_store=registry.communications),
            poll_interval_s=30.0,
        )
        # poll_once awaits the worker's P0-D commit gate (flush_durable), so
        # the durable attention mirror is already settled on return.
        await worker.poll_once()

        from sqlalchemy import select

        async with make_session_factory(engine)() as session:
            rows = list(
                (await session.execute(select(AttentionItemRow))).scalars().all()
            )
        sec_jobs = await registry.mail_security.list_jobs(f"conn:{conn.id}")

        snapshot = sorted(
            (
                {
                    "id": r.id,
                    "title": r.title,
                    "state": r.state,
                    "importance": r.importance,
                }
                for r in rows
            ),
            key=lambda d: d["id"],
        )
        set_communications_store(None)
        set_mail_security_repository(None)
        reset_communications_service()
        await engine.dispose()
        return snapshot, len(sec_jobs)

    async def test_attention_identical_with_flag_on_vs_off(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        off_snapshot, off_jobs = await self._run_and_capture_attention(
            tmp_path / "off.sqlite", monkeypatch, flag_on=False
        )
        on_snapshot, on_jobs = await self._run_and_capture_attention(
            tmp_path / "on.sqlite", monkeypatch, flag_on=True
        )
        # Flag ON scheduled a security job; flag OFF did not.
        assert off_jobs == 0
        assert on_jobs == 1
        # The durable attention mint is byte-identical regardless of the flag:
        # the security job neither creates, alters, nor suppresses any item.
        assert on_snapshot == off_snapshot


# ---------------------------------------------------------------------------
# 5. SCHEDULING FAILURE ISOLATION (R-001.3, I-07)
# ---------------------------------------------------------------------------


class _ExplodingRepo:
    """A mail-security repo whose job write always raises — the scheduling
    failure the ingest must absorb without touching the cursor."""

    async def upsert_job(self, **_kwargs: Any) -> Any:
        raise RuntimeError(f"boom {_CANARY}")  # carries a canary too


class TestSchedulingFailureIsolation:
    async def test_failure_does_not_block_cursor(self, harness, monkeypatch) -> None:
        _enable_flag(monkeypatch)
        set_mail_security_repository(_ExplodingRepo())
        transport = StubMailTransport(items=[_make_item("<1@corp>")], next_cursor={"uid": 5})
        worker = _worker(harness, transport=transport)
        await worker.poll_once()

        # The ingest still completed cleanly and the cursor advanced exactly
        # as the baseline — a scheduling failure never gates the cursor.
        assert worker.status()["last"]["status"] == "ok"
        cursor = await harness.registry.imports.get_cursor(harness.conn.id, "imap_seq")
        assert cursor.cursor_value["uid"] == 5
        comms = await harness.registry.communications.list_communications()
        assert len(comms) == 1
        # No forged "safe" job row was written (the write raised).
        assert await _security_jobs(harness) == []


# ---------------------------------------------------------------------------
# 6. NO-SECRET / NO-RAW-BODY (T-14)
# ---------------------------------------------------------------------------


class TestNoCanaryLeak:
    async def test_canary_absent_from_job_row(self, harness, monkeypatch) -> None:
        _enable_flag(monkeypatch)
        set_mail_security_repository(harness.registry.mail_security)
        transport = StubMailTransport(items=[_make_item("<1@corp>")], next_cursor={"uid": 5})
        worker = _worker(harness, transport=transport)
        await worker.poll_once()

        jobs = await _security_jobs(harness)
        assert len(jobs) == 1
        blob = " ".join(
            str(x)
            for x in (
                jobs[0].account_scope,
                jobs[0].outbox_job_key,
                jobs[0].source_key,
                jobs[0].analysis_profile_version,
                jobs[0].state,
                jobs[0].last_error_code,
                jobs[0].meta,
            )
        )
        assert _CANARY not in blob
        # The sender address must not leak into the row either.
        assert "boss@corp.com" not in blob
        assert _SECRET not in blob

    async def test_schedule_failure_log_has_no_canary(self) -> None:
        # A failing repo whose exception text carries the canary must not be
        # echoed into any log line — the seam logs a safe code only. Capture
        # via structlog.testing (no global reconfigure) so other tests' logger
        # config is untouched.
        from structlog.testing import capture_logs

        seam = MailSecurityIngestionSeam(
            repository=_ExplodingRepo(),
            account_scope="conn:xyz",
            env={ENV_MAIL_SECURITY_ENABLED: "1"},
        )
        with capture_logs() as logs:
            result = await seam.schedule(
                provider="imap", provider_uid="<1@corp>", rfc_message_id="<1@corp>"
            )
        assert result is None  # swallowed, not raised
        blob = " ".join(str(entry) for entry in logs)
        assert _CANARY not in blob
        # The scheduler logged a safe code, not the exception text.
        assert any(entry.get("event") == "mail_security.schedule_failed" for entry in logs)


# ---------------------------------------------------------------------------
# 7. API path parity
# ---------------------------------------------------------------------------


class TestApiPathHelper:
    async def test_api_helper_schedules_when_on(self, tmp_path, monkeypatch) -> None:
        from backend.api.endpoints.communications import _schedule_security_job

        engine = await _make_engine(tmp_path / "api.sqlite")
        repo = MailSecurityRepository(make_session_factory(engine))
        monkeypatch.setenv(ENV_MAIL_SECURITY_ENABLED, "1")
        set_mail_security_repository(repo)
        try:
            communication = SimpleNamespace(
                communication_id="comm-api-1",
                channel="email",
                source="personal-relay",
                content_ref="content://x/1",
                classification={"flags": {"bulk": False}},
            )
            await _schedule_security_job(communication)
            jobs = await repo.list_jobs("src:personal-relay")
            assert len(jobs) == 1
            assert jobs[0].state == JOB_STATE_PENDING
        finally:
            set_mail_security_repository(None)
            await engine.dispose()

    async def test_api_helper_noop_when_off(self, tmp_path, monkeypatch) -> None:
        from backend.api.endpoints.communications import _schedule_security_job

        engine = await _make_engine(tmp_path / "api_off.sqlite")
        repo = MailSecurityRepository(make_session_factory(engine))
        monkeypatch.setenv(ENV_MAIL_SECURITY_ENABLED, "0")
        set_mail_security_repository(repo)
        try:
            communication = SimpleNamespace(
                communication_id="comm-api-2",
                channel="email",
                source="personal-relay",
                content_ref="content://x/2",
                classification={},
            )
            await _schedule_security_job(communication)
            assert await repo.list_jobs("src:personal-relay") == []
        finally:
            set_mail_security_repository(None)
            await engine.dispose()


# ---------------------------------------------------------------------------
# 8. Reconcilable scanner (design §4.4 fallback)
# ---------------------------------------------------------------------------


class TestScanMissingJobs:
    async def test_recreates_missing_and_is_idempotent(self, tmp_path) -> None:
        engine = await _make_engine(tmp_path / "scan.sqlite")
        repo = MailSecurityRepository(make_session_factory(engine))
        try:
            account_scope = "conn:abc"
            sources = [
                {
                    "provider": "imap",
                    "provider_uid": "<1@corp>",
                    "rfc_message_id": "<1@corp>",
                    "folder": "INBOX",
                    "content_ref": "content://mail/1",
                    "flags": {"bulk": False},
                }
            ]
            created = await scan_missing_jobs(
                repository=repo, account_scope=account_scope, sources=sources
            )
            assert len(created) == 1
            assert created[0].state == JOB_STATE_PENDING
            # Running again is idempotent — no duplicate (same outbox key).
            again = await scan_missing_jobs(
                repository=repo, account_scope=account_scope, sources=sources
            )
            assert len(again) == 1
            # Exactly one row persisted across both sweeps (idempotent).
            assert len(await repo.list_jobs(account_scope)) == 1
        finally:
            await engine.dispose()
