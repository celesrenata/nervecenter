"""NerveShield local-only security worker tests (Phase 1, Task 1.9).

Proves the Task 1.9 Done criterion — a **full synthetic ingress-to-assessment
integration test** — plus the task's safety invariants. The worker is the
CONSUMER that ties the Task 1.3–1.6 deterministic spine together over the Task
1.8 scheduled jobs and the Task 1.7 durable tables:

    1.8 seam (flag on) -> PENDING job -> worker.process_one()
        -> parse (1.3) + auth (1.4) + inspections (1.5) + policy (1.6)
        -> SecurityAssessment + findings persisted (1.7) -> job DONE

Reuses the house durable harness (SQLite scratch DB + ``InMemoryObjectStore`` +
a real :class:`MailIngestWorker`, exactly as ``test_mail_ingest.py`` /
``test_mail_security_ingestion_seam.py``). No real accounts, no network.

Cases
-----
0. NO NETWORK — the worker module imports no socket / http / url / dns / model
   client (R-005.2, T-18 "no network in CI").
1. INGRESS→ASSESSMENT INTEGRATION (the headline Done criterion) — a synthetic
   message flows through the 1.8 seam, the worker processes the PENDING job, a
   SHADOW assessment + findings land in the 1.7 tables, the job is DONE, and the
   existing attention/Today outcome is unchanged (T-26, R-001.4).
2. CREDENTIAL-ASK / DECEPTIVE-LINK → REVIEW/RESTRICTED with the expected
   finding categories (R-005, R-011).
3. BENIGN → the policy's conservative non-malicious disposition (LOW_PRIORITY,
   never SAFE_TO_PROCESS per NS-ADR-013 / I-02).
4. INCOMPLETE / RESOLUTION-FAILURE / SIMULATED TIMEOUT → INCOMPLETE assessment
   + typed job failure state, NEVER clean (I-02, T-18) — the must-have.
5. IDEMPOTENT / REPLAY → processing the same job twice yields ONE assessment
   revision; verdict committed before ack (T-16, T-17).
6. CROSS-ACCOUNT → the worker never resolves/writes outside the job's
   account_scope (T-15).
7. NO RAW BODY in the persisted assessment/findings (canary, T-14).
8. FLAG OFF → the worker does nothing.
"""

from __future__ import annotations

import importlib
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select
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
from backend.mail_security.identifiers import (
    assessment_id as derive_assessment_id,
)
from backend.mail_security.identifiers import (
    logical_source_key,
    outbox_job_key,
)
from backend.mail_security.ingestion_seam import (
    DEFAULT_ANALYSIS_PROFILE_VERSION,
    ENV_MAIL_SECURITY_ENABLED,
    JOB_STATE_PENDING,
    MailSecurityIngestionSeam,
    set_mail_security_repository,
)
from backend.mail_security.models import (
    Disposition,
    FindingCategory,
    ProcessingState,
)
from backend.mail_security.worker import (
    JOB_STATE_DEAD_LETTER,
    JOB_STATE_DONE,
    JOB_STATE_ERROR_RETRYABLE,
    MailSecurityWorker,
    ObjectStoreContentResolver,
    SecurityScanTimeout,
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
#: A secret that stands in for raw body text — it must never reach the DB.
_CANARY = "CANARY-SECRET-dont-persist-me-9f3b2a"
_TS = datetime(2026, 10, 27, 9, 0, 0, tzinfo=UTC)
_CLOCK = datetime(2026, 10, 27, 10, 0, 0, tzinfo=UTC)

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


# ---------------------------------------------------------------------------
# synthetic messages (raw RFC822 so the parser extracts real evidence)
# ---------------------------------------------------------------------------

#: A benign plain-text newsletter. No threat findings; complete inspection.
_BENIGN_MIME = (
    f"From: Newsletter <news@example-news.com>\r\n"
    f"To: {_ACCOUNT}\r\n"
    f"Subject: Weekly digest {_CANARY}\r\n"
    f"Message-ID: <benign-1@example-news.com>\r\n"
    f"Date: Mon, 27 Oct 2026 09:00:00 +0000\r\n"
    f"Content-Type: text/plain; charset=utf-8\r\n"
    f"\r\n"
    f"Here is your weekly digest {_CANARY}. Thanks for reading.\r\n"
).encode()

#: A credential-ask + deceptive-link HTML message: the visible anchor text
#: claims paypal.com but the href points at a different registrable domain,
#: plus an account-takeover pressure phrase. Drives URL_DECEPTION (MEDIUM) ->
#: policy REVIEW (R-005, R-011).
_PHISH_MIME = (
    f"From: PayPal Security <alert@evil-login.example>\r\n"
    f"To: {_ACCOUNT}\r\n"
    f"Subject: Verify your account now {_CANARY}\r\n"
    f"Message-ID: <phish-1@evil-login.example>\r\n"
    f"Date: Mon, 27 Oct 2026 09:00:00 +0000\r\n"
    f"Content-Type: text/html; charset=utf-8\r\n"
    f"\r\n"
    f"<html><body>"
    f"<p>Your account will be suspended. Confirm your password now.</p>"
    f'<a href="http://evil-login.example/reset">http://paypal.com/account</a>'
    f"<!-- {_CANARY} -->"
    f"</body></html>\r\n"
).encode()


def _item(message_id: str, body: bytes, *, sender: str) -> MailItem:
    """A synthetic MailItem whose content_bytes are raw RFC822."""
    return MailItem(
        message_id=message_id,
        account=_ACCOUNT,
        subject=f"Subject {_CANARY}",
        content_bytes=body,
        timestamp=_TS,
        headers={
            "From": sender,
            "Message-ID": message_id,
            "Date": "Mon, 27 Oct 2026 09:00:00 +0000",
        },
    )


# ---------------------------------------------------------------------------
# harness
# ---------------------------------------------------------------------------


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
    """Durable SQLite registry + object store + one imap connection on INBOX."""
    reset_communications_service()
    engine = await _make_engine(tmp_path / "worker.sqlite")
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
    yield SimpleNamespace(
        engine=engine,
        registry=registry,
        store=store,
        conn=conn,
        scope=f"conn:{conn.id}",
    )
    set_communications_store(None)
    set_mail_security_repository(None)
    reset_communications_service()
    await engine.dispose()


def _ingest_worker(harness, *, transport: Any) -> MailIngestWorker:
    return MailIngestWorker(
        connection_id=harness.conn.id,
        cursor_kind="imap_seq",
        connections=harness.registry.connections,
        imports=harness.registry.imports,
        object_store=harness.store,
        transport=transport,
        seam=DefaultIngestSeam(communications_store=harness.registry.communications),
        poll_interval_s=30.0,
    )


def _security_worker(harness, **kwargs: Any) -> MailSecurityWorker:
    """A worker bound to the harness's repo + object-store resolver.

    The account scope is the job's canonical ``conn:<id>`` boundary and the
    content resolver is the dedicated typed object-store resolver (LOCAL-only).
    """
    return MailSecurityWorker(
        repository=harness.registry.mail_security,
        content_resolver=ObjectStoreContentResolver(object_store=harness.store),
        account_scope=harness.scope,
        analysis_profile_version=DEFAULT_ANALYSIS_PROFILE_VERSION,
        clock=lambda: _CLOCK,
        env={ENV_MAIL_SECURITY_ENABLED: "1"},
        **kwargs,
    )


async def _ingest(harness, monkeypatch, item: MailItem, *, uid: int = 5) -> None:
    """Drive one synthetic message through the real ingest worker with the
    1.8 seam flag ON, so a PENDING security job is scheduled beside it."""
    monkeypatch.setenv(ENV_MAIL_SECURITY_ENABLED, "1")
    set_mail_security_repository(harness.registry.mail_security)
    transport = StubMailTransport(items=[item], next_cursor={"uid": uid})
    await _ingest_worker(harness, transport=transport).poll_once()


async def _jobs(harness) -> list[MailSecurityJobRow]:
    return await harness.registry.mail_security.list_jobs(harness.scope)


async def _assessments(harness) -> list[MailSecurityAssessmentRow]:
    return await harness.registry.mail_security.list_assessments(harness.scope)


async def _attention(engine: AsyncEngine) -> list[dict[str, Any]]:
    async with make_session_factory(engine)() as session:
        rows = list((await session.execute(select(AttentionItemRow))).scalars().all())
    return sorted(
        (
            {"id": r.id, "title": r.title, "state": r.state, "importance": r.importance}
            for r in rows
        ),
        key=lambda d: d["id"],
    )


# ---------------------------------------------------------------------------
# 0. NO NETWORK (R-005.2, T-18)
# ---------------------------------------------------------------------------


class TestNoNetwork:
    def test_worker_imports_no_network_or_model(self) -> None:
        import backend.mail_security.worker as worker_mod

        src = Path(worker_mod.__file__).read_text(encoding="utf-8")
        for forbidden in (
            "import socket",
            "import http",
            "import urllib",
            "import requests",
            "import dns",
            "omniroute",
            "inference",
        ):
            assert forbidden not in src, f"worker must not reference {forbidden!r}"
        # Re-import is clean (no import-time side effects / network).
        importlib.reload(worker_mod)


# ---------------------------------------------------------------------------
# 1. INGRESS -> ASSESSMENT INTEGRATION (the headline Done criterion)
# ---------------------------------------------------------------------------


class TestIngressToAssessmentIntegration:
    async def test_full_slice_shadow_and_attention_unchanged(
        self, harness, monkeypatch
    ) -> None:
        # Capture the existing attention outcome with the flag ON but BEFORE the
        # security worker runs — the worker must not perturb it (shadow, T-26).
        await _ingest(harness, monkeypatch, _item(
            "<slice@corp>", _BENIGN_MIME, sender="Newsletter <news@example-news.com>"
        ))
        attention_before = await _attention(harness.engine)

        # A single PENDING job exists from the 1.8 seam.
        jobs = await _jobs(harness)
        assert len(jobs) == 1
        assert jobs[0].state == JOB_STATE_PENDING

        # The worker processes it end to end.
        worker = _security_worker(harness)
        processed = await worker.process_one()
        assert processed is not None
        assert processed.state == JOB_STATE_DONE

        # A SHADOW assessment + findings landed in the 1.7 tables.
        assessments = await _assessments(harness)
        assert len(assessments) == 1
        row = assessments[0]
        assert row.account_scope == harness.scope
        assert row.mode == "SHADOW"
        assert row.processing_state == ProcessingState.ASSESSED.value
        # Deterministic id ties to the job's source key (Task 1.2).
        expected_id = derive_assessment_id(
            harness.scope, jobs[0].source_key, 1, DEFAULT_ANALYSIS_PROFILE_VERSION
        )
        assert row.assessment_id == expected_id

        # An evidence manifest was written (metadata only).
        evidence = await harness.registry.mail_security.get_evidence(
            harness.scope, jobs[0].source_key, 1
        )
        assert evidence is not None

        # The existing attention/Today outcome is UNCHANGED by the worker.
        assert await _attention(harness.engine) == attention_before


# ---------------------------------------------------------------------------
# 2. CREDENTIAL-ASK / DECEPTIVE-LINK -> REVIEW / RESTRICTED (R-005, R-011)
# ---------------------------------------------------------------------------


class TestDeceptiveLinkReview:
    async def test_phish_message_is_reviewed_with_expected_findings(
        self, harness, monkeypatch
    ) -> None:
        await _ingest(harness, monkeypatch, _item(
            "<phish@corp>", _PHISH_MIME, sender="PayPal Security <alert@evil-login.example>"
        ))
        worker = _security_worker(harness)
        job = await worker.process_one()
        assert job is not None and job.state == JOB_STATE_DONE

        row = (await _assessments(harness))[0]
        # Deception finding forces a restrictive, non-clean disposition.
        assert row.disposition in (
            Disposition.REVIEW.value,
            Disposition.RESTRICTED.value,
        )
        assert row.disposition != Disposition.SAFE_TO_PROCESS.value

        findings = await harness.registry.mail_security.get_findings(
            harness.scope, row.assessment_id
        )
        categories = {f.category for f in findings}
        assert FindingCategory.URL_DECEPTION.value in categories


# ---------------------------------------------------------------------------
# 3. BENIGN -> LOW_PRIORITY, never SAFE_TO_PROCESS (I-02, NS-ADR-013)
# ---------------------------------------------------------------------------


class TestBenignConservative:
    async def test_benign_is_low_priority_never_safe(
        self, harness, monkeypatch
    ) -> None:
        await _ingest(harness, monkeypatch, _item(
            "<benign@corp>", _BENIGN_MIME, sender="Newsletter <news@example-news.com>"
        ))
        job = await _security_worker(harness).process_one()
        assert job is not None and job.state == JOB_STATE_DONE

        row = (await _assessments(harness))[0]
        assert row.processing_state == ProcessingState.ASSESSED.value
        # Conservative: clean-but-unverified is LOW_PRIORITY, never SAFE.
        assert row.disposition == Disposition.LOW_PRIORITY.value
        assert row.disposition != Disposition.SAFE_TO_PROCESS.value


# ---------------------------------------------------------------------------
# 4. INCOMPLETE / RESOLUTION-FAILURE / TIMEOUT -> never clean (I-02, T-18)
# ---------------------------------------------------------------------------


class TestNeverCleanOnFailure:
    async def test_resolution_failure_is_incomplete_not_clean(
        self, harness, monkeypatch
    ) -> None:
        await _ingest(harness, monkeypatch, _item(
            "<nores@corp>", _BENIGN_MIME, sender="Newsletter <news@example-news.com>"
        ))
        # Wipe the object store so content can never be resolved.
        harness.store._objects.clear()

        job = await _security_worker(harness).process_one()
        assert job is not None
        # Typed retryable failure state, NEVER done/clean.
        assert job.state == JOB_STATE_ERROR_RETRYABLE
        assert job.last_error_code  # a safe code, not raw text

        row = (await _assessments(harness))[0]
        assert row.processing_state == ProcessingState.INCOMPLETE.value
        assert row.disposition != Disposition.SAFE_TO_PROCESS.value
        assert row.disposition in (
            Disposition.INCOMPLETE.value,
            Disposition.REVIEW.value,
            Disposition.RESTRICTED.value,
        )

    async def test_simulated_timeout_is_incomplete_not_clean(
        self, harness, monkeypatch
    ) -> None:
        await _ingest(harness, monkeypatch, _item(
            "<slow@corp>", _BENIGN_MIME, sender="Newsletter <news@example-news.com>"
        ))
        # A monotonic clock that always reports the deadline exceeded.
        ticks = iter([0.0, 999.0])
        worker = _security_worker(
            harness,
            scan_deadline_seconds=0.001,
            monotonic=lambda: next(ticks),
        )
        job = await worker.process_one()
        assert job is not None
        assert job.state == JOB_STATE_ERROR_RETRYABLE
        assert job.last_error_code == "SCAN_TIMEOUT"

        row = (await _assessments(harness))[0]
        assert row.processing_state == ProcessingState.INCOMPLETE.value
        assert row.disposition != Disposition.SAFE_TO_PROCESS.value

    async def test_dead_letter_when_retries_exhausted(
        self, harness, monkeypatch
    ) -> None:
        await _ingest(harness, monkeypatch, _item(
            "<dl@corp>", _BENIGN_MIME, sender="Newsletter <news@example-news.com>"
        ))
        harness.store._objects.clear()
        worker = _security_worker(harness, max_attempts=1)
        job = await worker.process_one()
        assert job is not None
        # attempts == 1 == ceiling -> dead-letter, still never clean.
        assert job.state == JOB_STATE_DEAD_LETTER
        row = (await _assessments(harness))[0]
        assert row.processing_state == ProcessingState.INCOMPLETE.value


# ---------------------------------------------------------------------------
# 5. IDEMPOTENT / REPLAY -> one assessment, verdict before ack (T-16, T-17)
# ---------------------------------------------------------------------------


class TestIdempotentReplay:
    async def test_reprocess_same_job_one_assessment(
        self, harness, monkeypatch
    ) -> None:
        await _ingest(harness, monkeypatch, _item(
            "<rep@corp>", _BENIGN_MIME, sender="Newsletter <news@example-news.com>"
        ))
        job_id = (await _jobs(harness))[0].id

        worker = _security_worker(harness)
        first = await worker.process_one()
        assert first is not None and first.state == JOB_STATE_DONE

        # Re-process the SAME job (simulate a crash before ack then re-claim):
        # force the job back to PENDING and run again.
        await harness.registry.mail_security.mark_job_state(
            harness.scope, job_id, state=JOB_STATE_PENDING,
            lease_owner=None, lease_expires_at=None,
        )
        second = await _security_worker(harness).process_one()
        assert second is not None and second.state == JOB_STATE_DONE

        # Exactly ONE assessment revision, no duplicate (idempotent id).
        assert len(await _assessments(harness)) == 1
        findings = await harness.registry.mail_security.get_findings(
            harness.scope, second.assessment_id if hasattr(second, "assessment_id") else
            (await _assessments(harness))[0].assessment_id
        )
        # Findings are not duplicated across the replay.
        keys = [f.finding_key for f in findings]
        assert len(keys) == len(set(keys))

    async def test_verdict_committed_before_ack(self, harness, monkeypatch) -> None:
        """A repo that records call order proves the assessment is committed
        before the job is marked DONE (design §4.4 step 5)."""
        await _ingest(harness, monkeypatch, _item(
            "<order@corp>", _BENIGN_MIME, sender="Newsletter <news@example-news.com>"
        ))
        real = harness.registry.mail_security
        order: list[str] = []

        class _OrderingRepo:
            async def list_jobs(self, *a: Any, **k: Any) -> Any:
                return await real.list_jobs(*a, **k)

            async def mark_job_state(self, *a: Any, **k: Any) -> Any:
                if k.get("state") == JOB_STATE_DONE:
                    order.append("ack")
                return await real.mark_job_state(*a, **k)

            async def upsert_evidence(self, *a: Any, **k: Any) -> Any:
                return await real.upsert_evidence(*a, **k)

            async def upsert_assessment(self, *a: Any, **k: Any) -> Any:
                order.append("verdict")
                return await real.upsert_assessment(*a, **k)

        worker = MailSecurityWorker(
            repository=_OrderingRepo(),
            content_resolver=ObjectStoreContentResolver(object_store=harness.store),
            account_scope=harness.scope,
            analysis_profile_version=DEFAULT_ANALYSIS_PROFILE_VERSION,
            clock=lambda: _CLOCK,
            env={ENV_MAIL_SECURITY_ENABLED: "1"},
        )
        await worker.process_one()
        assert order == ["verdict", "ack"], order


# ---------------------------------------------------------------------------
# 6. CROSS-ACCOUNT isolation (T-15, R-017)
# ---------------------------------------------------------------------------


class TestCrossAccountIsolation:
    async def test_worker_only_touches_its_own_scope(
        self, harness, monkeypatch
    ) -> None:
        await _ingest(harness, monkeypatch, _item(
            "<iso@corp>", _BENIGN_MIME, sender="Newsletter <news@example-news.com>"
        ))
        other_scope = "conn:other-account-zzzz"

        # A worker bound to a DIFFERENT scope claims nothing (the job is A's).
        other_worker = MailSecurityWorker(
            repository=harness.registry.mail_security,
            content_resolver=ObjectStoreContentResolver(object_store=harness.store),
            account_scope=other_scope,
            analysis_profile_version=DEFAULT_ANALYSIS_PROFILE_VERSION,
            clock=lambda: _CLOCK,
            env={ENV_MAIL_SECURITY_ENABLED: "1"},
        )
        assert await other_worker.process_one() is None

        # The correct-scope worker processes it; the assessment is A's only.
        await _security_worker(harness).process_one()
        assert await _assessments(harness)  # A has one
        assert (
            await harness.registry.mail_security.list_assessments(other_scope) == []
        )


# ---------------------------------------------------------------------------
# 7. NO RAW BODY in the persisted assessment / findings (canary, T-14)
# ---------------------------------------------------------------------------


class TestNoRawBodyPersisted:
    async def test_canary_never_persisted(self, harness, monkeypatch) -> None:
        await _ingest(harness, monkeypatch, _item(
            "<canary@corp>", _PHISH_MIME, sender="PayPal Security <alert@evil-login.example>"
        ))
        await _security_worker(harness).process_one()

        async with make_session_factory(harness.engine)() as session:
            asmt_rows = list(
                (await session.execute(select(MailSecurityAssessmentRow))).scalars().all()
            )
            find_rows = list(
                (await session.execute(select(MailSecurityFindingRow))).scalars().all()
            )
            ev_rows = list(
                (await session.execute(select(MailEvidenceRow))).scalars().all()
            )

        def _blob(rows: list[Any]) -> str:
            import json

            parts: list[str] = []
            for r in rows:
                for col in r.__table__.columns:
                    val = getattr(r, col.name)
                    parts.append(json.dumps(val, default=str))
            return "\n".join(parts)

        assert _CANARY not in _blob(asmt_rows)
        assert _CANARY not in _blob(find_rows)
        assert _CANARY not in _blob(ev_rows)


# ---------------------------------------------------------------------------
# 8. FLAG OFF -> worker does nothing
# ---------------------------------------------------------------------------


class TestFlagOffIsNoOp:
    async def test_flag_off_processes_nothing(self, harness, monkeypatch) -> None:
        await _ingest(harness, monkeypatch, _item(
            "<off@corp>", _BENIGN_MIME, sender="Newsletter <news@example-news.com>"
        ))
        # A worker whose env has the flag OFF must not claim or process.
        worker = MailSecurityWorker(
            repository=harness.registry.mail_security,
            content_resolver=ObjectStoreContentResolver(object_store=harness.store),
            account_scope=harness.scope,
            analysis_profile_version=DEFAULT_ANALYSIS_PROFILE_VERSION,
            clock=lambda: _CLOCK,
            env={ENV_MAIL_SECURITY_ENABLED: "0"},
        )
        assert worker.enabled is False
        assert await worker.process_one() is None
        assert await worker.process_available() == []
        # The job is left untouched (still PENDING) and no assessment exists.
        assert (await _jobs(harness))[0].state == JOB_STATE_PENDING
        assert await _assessments(harness) == []


# A ``SecurityScanTimeout`` is importable / a distinct exception type.
def test_scan_timeout_is_exception() -> None:
    assert issubclass(SecurityScanTimeout, Exception)
