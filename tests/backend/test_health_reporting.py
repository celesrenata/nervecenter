"""Additive ``/api/health`` reporting tests (ingest + connections sections).

The existing ``db`` / ``nats`` checks and HTTP-200 semantics are unchanged;
these tests pin the *additive* ``ingest`` and ``connections`` sections:

- honest ``not configured`` degradation when the supervisor / durable
  registry are absent;
- supervisor liveness (installed, worker count, per-worker summary) when the
  plane is running;
- redacted per-connection status (kind, auth/operational status, last
  success/attempt, last error code + truncated message) — never secrets,
  config payloads, or message bodies.

Request/app.state is built the same way ``tests/backend/test_app_wiring.py``
does (a ``FastAPI`` app with attributes set on ``app.state`` and driven
through ``TestClient``).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from backend.api import health as health_module
from backend.connectors.mail.mail_transport import (
    StubMailTransport,
    TransportConnectError,
)
from backend.ingest import supervisor as supervisor_module
from backend.ingest.supervisor import MailIngestSupervisor, SUPERVISOR_STATE_KEY
from backend.persistence.connection import make_session_factory
from backend.persistence.models.personal import (
    ConnectionRow,
    ImportJobRow,
    IngestCursorRow,
    SecretRow,
)
from backend.persistence.repositories import RepositoryRegistry
from backend.persistence.repositories.connections_repo import IdentitySecretCodec
from backend.storage.memory import InMemoryObjectStore

_PERSONAL_TABLES = [
    ConnectionRow.__table__,
    SecretRow.__table__,
    IngestCursorRow.__table__,
    ImportJobRow.__table__,
]

_ACCOUNT = "health@example.com"
_SECRET = "super-secret-password"
_TS = datetime(2026, 10, 27, 9, 30, 0, tzinfo=UTC)


# --------------------------------------------------------------------------
# fixtures / helpers
# --------------------------------------------------------------------------


async def _make_engine(db_path: Path) -> AsyncEngine:
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{db_path}",
        poolclass=NullPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda sc: _PERSONAL_TABLES[0].metadata.create_all(
                sc, tables=_PERSONAL_TABLES
            )
        )
    return engine


def _app(state: dict[str, Any]) -> FastAPI:
    """A bare FastAPI app exposing the health router with the given state."""
    app = FastAPI(title="NerveCenterTest", version="0.1.0")
    for key, value in state.items():
        setattr(app.state, key, value)
    app.include_router(health_module.router)
    return app


@pytest.fixture
async def harness(tmp_path: Path, monkeypatch):
    """Durable SQLite registry + one enabled imap connection (secret stored)."""
    engine = await _make_engine(tmp_path / "health.sqlite")
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
    # The supervisor's worker runs in the background against a transport
    # that fails fast, so the durable row lands in a stable, known error
    # state (no flaky races between the test's writes and the worker's).
    monkeypatch.setattr(
        supervisor_module,
        "_build_transport",
        lambda kind, config: StubMailTransport(
            raise_exc=TransportConnectError("smtp timed out: connection refused")
        ),
    )
    yield SimpleNamespace(engine=engine, registry=registry, store=store, conn=conn)
    await engine.dispose()


async def _supervisor(harness: SimpleNamespace) -> MailIngestSupervisor:
    return MailIngestSupervisor(
        registry=harness.registry,
        object_store=harness.store,
        poll_interval_s=0.1,
        reconcile_interval_s=0.1,
    )


async def _settle(*, seconds: float = 0.3) -> None:
    """Let the background worker complete at least one poll cycle."""
    import asyncio

    await asyncio.sleep(seconds)


# --------------------------------------------------------------------------
# degradation: not configured
# --------------------------------------------------------------------------


class TestHealthDegradation:
    async def test_not_configured_without_supervisor_or_registry(
        self, harness
    ) -> None:
        """No supervisor, no registry: both sections are honestly degraded,
        the response is still 200 with the legacy fields intact."""
        app = _app({"db_engine": None, "nats_client": None,
                    "repository_registry": None})
        with TestClient(app) as client:
            response = client.get("/api/health")

        assert response.status_code == 200
        body = response.json()
        # legacy fields unchanged
        assert body["status"] == "degraded"
        assert body["version"] == "0.1.0"
        assert body["checks"]["db"]["status"] == "unreachable"
        assert body["checks"]["nats"]["status"] == "unreachable"
        # new sections degrade honestly (additive keys present; per-key
        # asserts so the env-dependent object_store section doesn't couple
        # to the test environment).
        ingest = body["ingest"]
        assert ingest["status"] == "not configured"
        assert ingest["workers"] == 0
        assert ingest["summary"] == []
        assert "reason" in ingest  # J3: gate reason is surfaced
        connections = body["connections"]
        assert connections["status"] == "not configured"
        assert connections["count"] == 0
        assert connections["connections"] == []
        assert "personal_attention_operational" in connections  # J3
        # J3: object-store readiness section is present (redacted).
        assert "object_store" in body
        assert body["object_store"]["status"] in (
            "not_configured", "incomplete", "configured", "memory", "unknown")

    async def test_connections_degrade_without_registry(self, harness) -> None:
        """Supervisor running but the registry attribute missing is not a
        realistic wiring state (wiring sets both) — the connections section
        must still degrade instead of raising."""
        supervisor = await _supervisor(harness)
        await supervisor.start()
        try:
            app = _app({"db_engine": None, "nats_client": None})
            with TestClient(app) as client:
                body = client.get("/api/health").json()
            assert body["connections"]["status"] == "not configured"
            assert body["ingest"]["status"] == "not configured"
        finally:
            await supervisor.stop()


# --------------------------------------------------------------------------
# installed supervisor + durable connections
# --------------------------------------------------------------------------


class TestHealthInstalled:
    async def test_ingest_and_connections_sections(self, harness) -> None:
        """A live worker against a failing transport → deterministic error
        bookkeeping on the durable row, reported verbatim (redacted) in
        both sections."""
        supervisor = await _supervisor(harness)
        await supervisor.start()
        await _settle()

        app = _app({
            "db_engine": None,
            "nats_client": None,
            "repository_registry": harness.registry,
            SUPERVISOR_STATE_KEY: supervisor,
        })
        try:
            with TestClient(app) as client:
                body = client.get("/api/health").json()

            ingest = body["ingest"]
            assert ingest["status"] == "installed"
            assert ingest["workers"] == 1
            assert ingest["workers_running"] == 1
            # per-worker liveness (per-connection durable status is the
            # job of the ``connections`` section below)
            assert len(ingest["summary"]) == 1
            summary = ingest["summary"][0]
            assert summary["connection_id"] == harness.conn.id
            assert summary["running"] is True
            assert summary["failures"] >= 1
            assert summary["last"]["status"] == "error"

            connections = body["connections"]
            assert connections["status"] == "ok"
            assert connections["count"] == 1
            entry = connections["connections"][0]
            assert entry["connection_id"] == harness.conn.id
            assert entry["kind"] == "imap"
            assert entry["auth_status"] == "authenticated"
            assert entry["operational_status"] == "error"
            assert entry["last_success_at"] is None
            # the mail worker writes auth/operational status + last error,
            # not last_attempt_at — report exactly what the DB holds
            assert entry["last_attempt_at"] is None
            assert entry["last_error_code"] == "connect_failed"
            assert entry["last_error_message"] == "smtp timed out: connection refused"

            # never secrets / config payloads / message bodies
            assert _SECRET not in str(body)
            assert "127.0.0.1" not in str(body)
        finally:
            await supervisor.stop()

    async def test_last_error_message_redacted_to_256(self, harness,
                                                       monkeypatch) -> None:
        """An over-long durable last_error_message is truncated, not echoed
        in full."""
        supervisor = await _supervisor(harness)
        await supervisor.start()

        transport = StubMailTransport(
            raise_exc=TransportConnectError("boom " * 200)
        )
        monkeypatch.setattr(
            supervisor_module, "_build_transport", lambda kind, config: transport
        )
        # stop the existing worker, then reconcile once so the *new*
        # transport backs a fresh worker (reuses the builder code path, §16)
        for worker in list(supervisor.workers.values()):
            await worker.stop()
        supervisor._workers.clear()
        await supervisor.reconcile_once()
        await _settle()

        app = _app({
            "db_engine": None,
            "nats_client": None,
            "repository_registry": harness.registry,
            SUPERVISOR_STATE_KEY: supervisor,
        })
        try:
            with TestClient(app) as client:
                body = client.get("/api/health").json()
            message = body["connections"]["connections"][0]["last_error_message"]
            assert message == ("boom " * 200)[:256]
            assert len(message) == 256
        finally:
            await supervisor.stop()

    async def test_ingest_reports_zero_workers_when_no_mail_connections(
        self, harness
    ) -> None:
        supervisor = await _supervisor(harness)
        await harness.registry.connections.update_connection(
            harness.conn.id, enabled=False
        )
        await supervisor.start()

        app = _app({
            "db_engine": None,
            "nats_client": None,
            "repository_registry": harness.registry,
            SUPERVISOR_STATE_KEY: supervisor,
        })
        try:
            with TestClient(app) as client:
                body = client.get("/api/health").json()
            assert body["ingest"]["status"] == "installed"
            assert body["ingest"]["workers"] == 0
            assert body["ingest"]["workers_running"] == 0
            assert body["ingest"]["summary"] == []
        finally:
            await supervisor.stop()

    async def test_existing_db_nats_fields_unchanged(self, harness, monkeypatch) -> None:
        """The additive sections must not disturb the legacy probe results."""
        from backend.persistence import connection as persistence_connection

        class _Report:
            ok = True

            def as_dict(self) -> dict[str, Any]:
                return {"latency_ms": 1.0}

        async def _fake_check_health(engine: Any) -> _Report:
            return _Report()

        monkeypatch.setattr(
            persistence_connection, "check_health", _fake_check_health
        )
        engine = create_async_engine("sqlite+aiosqlite://")
        supervisor = await _supervisor(harness)
        await supervisor.start()

        app = _app({
            "db_engine": engine,
            "nats_client": None,
            "repository_registry": harness.registry,
            SUPERVISOR_STATE_KEY: supervisor,
        })
        try:
            with TestClient(app) as client:
                body = client.get("/api/health").json()
            assert body["checks"]["db"]["status"] == "ok"
            assert body["checks"]["db"]["latency_ms"] == 1.0
            assert body["status"] == "degraded"  # nats unconfigured
            assert body["ingest"]["workers"] == 1
            assert body["connections"]["count"] == 1
        finally:
            await supervisor.stop()
            await engine.dispose()
