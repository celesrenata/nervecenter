"""Dynamic reconcile tests for the mail-ingest supervisor (spec §4/§16).

Covers the reconcile loop that keeps the worker set in sync with durable
state *without a process restart*:

- newly added enabled mail connections start a worker;
- disabled / removed connections stop their worker;
- startup and reconcile share one worker-builder code path;
- ``status()`` reports per-connection liveness with a redacted (truncated,
  secret-free) last error;
- the reconcile interval comes from ``NERVE_INGEST_RECONCILE_S`` (default
  30s, lower-bound 1s).

The durable fixture mirrors ``tests/backend/test_mail_ingest.py`` (file-backed
SQLite + ``InMemoryObjectStore``). ``_build_transport`` is monkeypatched to a
``StubMailTransport`` so no network I/O happens; the supervisor-under-test is
never modified — only the transport seam it consults.
"""

from __future__ import annotations

import asyncio
import inspect
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

import backend.api.models  # noqa: F401 - initialize the api package before ingest (breaks a pre-existing import cycle)
from backend.connectors.mail.mail_transport import (
    StubMailTransport,
    TransportConnectError,
)
from backend.ingest import supervisor as supervisor_module
from backend.ingest.supervisor import MailIngestSupervisor, _reconcile_interval_s
from backend.persistence.connection import make_session_factory
from backend.persistence.models.personal import (
    ConnectionRow,
    ImportJobRow,
    IngestCursorRow,
    SecretRow,
)
from backend.persistence.repositories import RepositoryRegistry
from backend.persistence.repositories.connections_repo import (
    ConnectionsRepository,
    IdentitySecretCodec,
)
from backend.storage.memory import InMemoryObjectStore

_PERSONAL_TABLES = [
    ConnectionRow.__table__,
    SecretRow.__table__,
    IngestCursorRow.__table__,
    ImportJobRow.__table__,
]

# NOTE: NullPool (not StaticPool) — the worker loop and the reconcile loop
# read/write through their own sessions concurrently with the test's
# mutations. StaticPool would share one physical SQLite connection between
# all sessions, so interleaved transactions corrupt each other (a write
# issued while another session holds the connection can be rolled back).
# One connection per session mirrors the production Postgres pool.

_ACCOUNT = "reconcile@example.com"
_SECRET = "super-secret-password"


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


@pytest.fixture
async def harness(tmp_path: Path, monkeypatch):
    """Durable SQLite registry + in-memory store + one enabled imap connection,
    with the supervisor's transport factory stubbed (no network)."""
    engine = await _make_engine(tmp_path / "reconcile.sqlite")
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
    # The supervisor constructs transports via this module-level helper —
    # stub it to a transport that fails fast. Workers never open a socket,
    # and the durable row lands in a stable, known error state (the worker
    # keeps writing ``connect_failed`` on every poll, so tests can rely on
    # it rather than race a manual write against the poll loop).
    monkeypatch.setattr(
        supervisor_module,
        "_build_transport",
        lambda kind, config: StubMailTransport(
            raise_exc=TransportConnectError("smtp timed out: connection refused")
        ),
    )
    yield SimpleNamespace(engine=engine, registry=registry, store=store, conn=conn)
    await engine.dispose()


def _supervisor(
    harness: SimpleNamespace, **kwargs: Any
) -> MailIngestSupervisor:
    """A supervisor that polls fast (0.1s) and reconciles fast (0.1s)."""
    kwargs.setdefault("poll_interval_s", 0.1)
    kwargs.setdefault("reconcile_interval_s", 0.1)
    return MailIngestSupervisor(
        registry=harness.registry, object_store=harness.store, **kwargs
    )


async def _wait_until(predicate, *, timeout: float = 5.0) -> None:
    """Poll until a sync or async predicate returns truthy (test helper)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        result = predicate()
        if inspect.isawaitable(result):
            result = await result
        if result:
            return
        if loop.time() > deadline:
            raise AssertionError("condition not reached before timeout")
        await asyncio.sleep(0.02)


# --------------------------------------------------------------------------
# reconcile: start / stop of workers from durable state
# --------------------------------------------------------------------------


class TestReconcile:
    async def test_starts_worker_for_newly_added_connection(self, harness) -> None:
        supervisor = _supervisor(harness)
        await supervisor.start()
        assert set(supervisor.workers) == {harness.conn.id}

        added = await harness.registry.connections.create_connection(
            kind="imap",
            label="Personal",
            config={"account": _ACCOUNT, "imap_host": "127.0.0.1",
                    "mailbox": "INBOX"},
            auth_status="authenticated",
            enabled=True,
        )
        assert added.id not in supervisor.workers

        await supervisor.reconcile_once()

        assert set(supervisor.workers) == {harness.conn.id, added.id}
        assert supervisor.worker_for(added.id) is not None
        assert supervisor.worker_for(added.id).is_running
        await supervisor.stop()

    async def test_stops_worker_when_connection_disabled(self, harness) -> None:
        supervisor = _supervisor(harness)
        await supervisor.start()
        assert harness.conn.id in supervisor.workers

        await harness.registry.connections.update_connection(
            harness.conn.id, enabled=False
        )
        await supervisor.reconcile_once()

        assert supervisor.workers == {}
        assert supervisor.worker_for(harness.conn.id) is None
        await supervisor.stop()

    async def test_restarts_worker_when_reenabled(self, harness) -> None:
        supervisor = _supervisor(harness)
        await supervisor.start()
        await harness.registry.connections.update_connection(
            harness.conn.id, enabled=False
        )
        await supervisor.reconcile_once()
        assert supervisor.workers == {}

        await harness.registry.connections.update_connection(
            harness.conn.id, enabled=True
        )
        await supervisor.reconcile_once()
        assert set(supervisor.workers) == {harness.conn.id}
        await supervisor.stop()

    async def test_stops_worker_when_connection_removed(self, harness) -> None:
        supervisor = _supervisor(harness)
        await supervisor.start()
        assert harness.conn.id in supervisor.workers

        # hard-delete the row (no delete method on the repository)
        async with harness.engine.begin() as session:
            await session.execute(
                delete(ConnectionRow).where(ConnectionRow.id == harness.conn.id)
            )
        await supervisor.reconcile_once()

        assert supervisor.workers == {}
        await supervisor.stop()

    async def test_ignores_non_mail_connection_kinds(self, harness) -> None:
        supervisor = _supervisor(harness)
        await supervisor.start()
        await harness.registry.connections.create_connection(
            kind="calendar",
            config={"account": "cal@example.com"},
            enabled=True,
        )
        await supervisor.reconcile_once()

        # only the mail connection gets a worker
        assert set(supervisor.workers) == {harness.conn.id}
        await supervisor.stop()

    async def test_loop_picks_up_connection_without_restart(self, harness) -> None:
        """The background reconcile loop (not a manual call) starts a worker
        for a connection added after startup — no process restart needed."""
        supervisor = _supervisor(harness)
        await supervisor.start()

        added = await harness.registry.connections.create_connection(
            kind="gmail",
            label="Gmail",
            config={"account": _ACCOUNT, "gmail_api_base": "https://example.test"},
            auth_status="authenticated",
            enabled=True,
        )

        await _wait_until(lambda: added.id in supervisor.workers)
        assert supervisor.worker_for(added.id).is_running
        await supervisor.stop()

    async def test_loop_stops_worker_for_disabled_connection(self, harness) -> None:
        supervisor = _supervisor(harness)
        await supervisor.start()

        await harness.registry.connections.update_connection(
            harness.conn.id, enabled=False
        )
        await _wait_until(lambda: supervisor.workers == {})
        await supervisor.stop()

    async def test_reconcile_survives_transient_db_failure(self, harness,
                                                           monkeypatch) -> None:
        supervisor = _supervisor(harness)
        await supervisor.start()

        async def _boom(**_kwargs: Any):
            raise RuntimeError("db down")

        # patch the class (the registry builds a fresh repository per
        # property access, so an instance-level patch is not visible)
        monkeypatch.setattr(ConnectionsRepository, "list_connections", _boom)
        # must not raise — a transient failure skips the pass, not the plane
        await supervisor.reconcile_once()
        assert harness.conn.id in supervisor.workers
        await supervisor.stop()

    async def test_stop_cancels_reconcile_loop(self, harness) -> None:
        supervisor = _supervisor(harness)
        await supervisor.start()
        assert supervisor._reconcile is not None and not supervisor._reconcile.done()
        await supervisor.stop()
        assert supervisor._reconcile is None
        assert supervisor.workers == {}


# --------------------------------------------------------------------------
# status() — per-connection liveness snapshot for /api/health
# --------------------------------------------------------------------------


class TestSupervisorStatus:
    async def test_status_reports_running_worker_with_durable_error(
        self, harness
    ) -> None:
        """With the worker live against a failing transport, the durable row
        holds the deterministic worker error, and status() reports it
        secret-free."""
        supervisor = _supervisor(harness)
        await supervisor.start()

        async def _row_in_error() -> bool:
            row = await harness.registry.connections.get_connection(harness.conn.id)
            return row is not None and row.operational_status == "error"

        await _wait_until(_row_in_error)

        summary = await supervisor.status()

        assert len(summary) == 1
        entry = summary[0]
        assert entry["connection_id"] == harness.conn.id
        assert entry["state"] == "running"
        assert entry["last_error_code"] == "connect_failed"
        assert entry["last_error_message"] == "smtp timed out: connection refused"
        # secret-free: no config, credentials, or transport material
        assert set(entry) == {
            "connection_id",
            "state",
            "last_success_at",
            "last_attempt_at",
            "last_error_code",
            "last_error_message",
        }
        assert _SECRET not in str(summary)
        await supervisor.stop()

    async def test_status_redacts_long_error_messages(self, harness) -> None:
        """An over-long durable last_error_message is truncated to 256 chars
        in the status snapshot (with no worker running to race the write)."""
        supervisor = _supervisor(harness)  # never started
        long_message = "boom " * 200  # >256 chars
        await harness.registry.connections.update_connection(
            harness.conn.id,
            operational_status="error",
            last_error_code="connect_failed",
            last_error_message=long_message,
            last_attempt_at=datetime(2026, 10, 28, 1, 0, 0, tzinfo=UTC),
        )

        summary = await supervisor.status()

        assert len(summary) == 1
        entry = summary[0]
        assert entry["state"] == "stopped"  # no worker (supervisor not started)
        assert entry["last_error_code"] == "connect_failed"
        assert entry["last_error_message"] == long_message[:256]
        assert len(entry["last_error_message"]) == 256
        # SQLite round-trips naive datetimes (Postgres keeps the offset), so
        # compare tz-agnostically rather than against a literal string
        assert datetime.fromisoformat(entry["last_attempt_at"]).replace(
            tzinfo=None
        ) == datetime(2026, 10, 28, 1, 0, 0)

    async def test_status_degrades_to_empty_on_db_failure(self, harness,
                                                          monkeypatch) -> None:
        supervisor = _supervisor(harness)  # never started — pure status() call

        async def _boom(**_kwargs: Any):
            raise RuntimeError("db down")

        # patch the class: registry properties build a fresh repository per
        # access, so an instance-level patch would not be visible here
        monkeypatch.setattr(ConnectionsRepository, "list_connections", _boom)
        assert await supervisor.status() == []


# --------------------------------------------------------------------------
# reconcile interval configuration
# --------------------------------------------------------------------------


class TestReconcileInterval:
    def test_default_is_30_seconds(self, monkeypatch) -> None:
        monkeypatch.delenv("NERVE_INGEST_RECONCILE_S", raising=False)
        assert _reconcile_interval_s() == 30.0

    def test_env_override(self, monkeypatch) -> None:
        monkeypatch.setenv("NERVE_INGEST_RECONCILE_S", "5")
        assert _reconcile_interval_s() == 5.0

    def test_invalid_value_falls_back(self, monkeypatch) -> None:
        monkeypatch.setenv("NERVE_INGEST_RECONCILE_S", "not-a-number")
        assert _reconcile_interval_s() == 30.0

    def test_lower_bound_is_one_second(self, monkeypatch) -> None:
        monkeypatch.setenv("NERVE_INGEST_RECONCILE_S", "0.001")
        assert _reconcile_interval_s() == 1.0

    def test_env_dict_and_kwarg_precedence(self, harness, monkeypatch) -> None:
        monkeypatch.setenv("NERVE_INGEST_RECONCILE_S", "999")
        # an explicit env dict wins over the process environment
        sup = MailIngestSupervisor(
            registry=harness.registry,
            object_store=harness.store,
            env={"NERVE_INGEST_RECONCILE_S": "2"},
        )
        assert sup._reconcile_interval_s == 2.0
        # an explicit kwarg wins over both env sources
        sup2 = MailIngestSupervisor(
            registry=harness.registry,
            object_store=harness.store,
            reconcile_interval_s=7.0,
        )
        assert sup2._reconcile_interval_s == 7.0
        # without a kwarg or env dict, the process env is used
        sup3 = MailIngestSupervisor(registry=harness.registry, object_store=harness.store)
        assert sup3._reconcile_interval_s == 999.0


# --------------------------------------------------------------------------
# unsupported kinds (P1-106: an unservable provider must not look healthy)
# --------------------------------------------------------------------------


def _sync_item(message_id: str) -> Any:
    """One scripted :class:`MailItem` for the ``sync_once`` success tests."""
    from datetime import UTC

    from backend.connectors.mail.mail_transport import MailItem

    return MailItem(
        message_id=message_id,
        account=_ACCOUNT,
        subject="Sync once",
        content_bytes=b"one line of body\n",
        timestamp=datetime(2026, 10, 29, 0, 0, 0, tzinfo=UTC),
        headers={"From": _ACCOUNT, "Message-ID": message_id},
    )


class _RecordingSeam:
    """A minimal :class:`IngestSeam` that records ingest calls (no
    ``flush_durable`` → treated as committed)."""

    def __init__(self) -> None:
        self.ingests: list[dict[str, Any]] = []

    def ingest_communication(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.ingests.append(payload)
        communication = SimpleNamespace(
            communication_id=payload["communication_id"],
            content_ref=payload.get("content_ref"),
            channel=payload.get("channel"),
            timestamp=payload.get("timestamp"),
        )
        return {"communication": communication, "conversation": None, "created": True}

    def run_pipeline(self, communication: Any, *, now: Any) -> Any:
        return None


class _ExplodingSeam:
    """An :class:`IngestSeam` that raises a credential-bearing message —
    exercises the shared redaction path (URL userinfo is stripped by
    :func:`backend.connectors.redact.redact_message`)."""

    def ingest_communication(self, payload: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("boom connecting imap://u:boom-secret-value@corp.example")

    def run_pipeline(self, communication: Any, *, now: Any) -> Any:
        return None


class TestSyncOnce:
    """``sync_once`` — the control plane's delegation target for a manual
    ``Sync Now`` / ``Import`` (workstream B → C). One bounded ingest cycle on
    a one-shot worker over the shared durable seams; never touches the live
    worker set (a running worker is left untouched, nothing is spawned)."""

    async def test_sync_once_ingests_and_marks_healthy(
        self, harness, monkeypatch
    ) -> None:
        from backend.connectors.registry import ConnectionControlPlaneError  # noqa: F401

        seam = _RecordingSeam()
        transport = StubMailTransport(items=[_sync_item("sync-1@corp.com")])
        monkeypatch.setattr(
            supervisor_module, "_build_transport", lambda kind, config: transport
        )
        supervisor = _supervisor(harness, seam=seam)

        summary = await supervisor.sync_once(harness.conn.id)
        assert summary["status"] == "ok"
        assert summary["ingested"] == 1
        assert len(seam.ingests) == 1

        stored = await harness.registry.connections.get_connection(harness.conn.id)
        assert stored.operational_status == "healthy"
        assert stored.auth_status == "authenticated"
        assert stored.last_error_code is None
        # The durable cursor advanced with the folded seen-list.
        cursor = await harness.registry.imports.get_cursor(
            harness.conn.id, "imap_seq"
        )
        assert cursor is not None
        assert f"{_ACCOUNT}:sync-1@corp.com" in cursor.cursor_value.get("seen", [])
        # A real import job row was opened and its progress updated.
        job = await harness.registry.imports.get_import_job(
            str((stored.config or {}).get("import_job_id"))
        )
        assert job is not None
        assert job.state == "running"
        assert int((job.progress or {}).get("ingested", 0)) == 1
        # sync_once leaves no worker behind (no loop task spawned).
        assert harness.conn.id not in supervisor.workers

    async def test_sync_once_records_durable_failure(self, harness) -> None:
        """The fixture's transport factory fails fast; the failure must be a
        stable :class:`ConnectionControlPlaneError` carrying the *durable*
        row code (``connect_failed``) so the plane's ``_start_sync`` records
        the same code on its failure path."""
        from backend.connectors.registry import ConnectionControlPlaneError

        supervisor = _supervisor(harness)
        with pytest.raises(ConnectionControlPlaneError) as exc:
            await supervisor.sync_once(harness.conn.id)
        assert exc.value.code == "connect_failed"

        stored = await harness.registry.connections.get_connection(harness.conn.id)
        assert stored.last_error_code == "connect_failed"
        assert harness.conn.id not in supervisor.workers

    async def test_sync_once_unknown_connection_is_connection_not_found(
        self, harness
    ) -> None:
        from backend.connectors.registry import ConnectionControlPlaneError

        supervisor = _supervisor(harness)
        with pytest.raises(ConnectionControlPlaneError) as exc:
            await supervisor.sync_once("no-such-id")
        assert exc.value.code == "connection_not_found"

    async def test_sync_once_non_mail_kind_is_kind_not_supported(
        self, harness
    ) -> None:
        """A ``graph_mail`` row (control-plane OAuth kind) is not servable by
        the mail ingest kinds: a trigger registered against it must fail with
        a stable code, never a worker (P1-106: an unservable provider must
        never look like a healthy worker)."""
        from backend.connectors.registry import ConnectionControlPlaneError

        row = await harness.registry.connections.create_connection(
            kind="graph_mail",
            label="Outlook (OAuth)",
            config={"account": "graph@example.com"},
            enabled=True,
        )
        supervisor = _supervisor(harness)
        with pytest.raises(ConnectionControlPlaneError) as exc:
            await supervisor.sync_once(row.id)
        assert exc.value.code == "kind_not_supported"
        assert row.id not in supervisor.workers

    async def test_sync_once_disabled_connection_fails_stably(
        self, harness
    ) -> None:
        from backend.connectors.registry import ConnectionControlPlaneError

        await harness.registry.connections.update_connection(
            harness.conn.id, enabled=False
        )
        supervisor = _supervisor(harness)
        with pytest.raises(ConnectionControlPlaneError) as exc:
            await supervisor.sync_once(harness.conn.id)
        assert exc.value.code == "ingest_failed"
        assert "disabled" in exc.value.message

    async def test_sync_once_failure_is_redacted(
        self, harness, monkeypatch
    ) -> None:
        """An unexpected exception inside the cycle surfaces a stable code
        (``ingest_failed``) and never leaks the raw exception text."""
        from backend.connectors.registry import ConnectionControlPlaneError

        transport = StubMailTransport(items=[_sync_item("sync-x@corp.com")])
        monkeypatch.setattr(
            supervisor_module, "_build_transport", lambda kind, config: transport
        )
        supervisor = _supervisor(harness, seam=_ExplodingSeam())
        with pytest.raises(ConnectionControlPlaneError) as exc:
            await supervisor.sync_once(harness.conn.id)
        assert exc.value.code == "ingest_failed"
        assert "boom-secret-value" not in exc.value.message


class TestUnsupportedKind:
    async def test_graph_mail_never_gets_a_worker(self, harness) -> None:
        """A ``graph_mail`` connection (the control-plane OAuth kind for
        Microsoft) is not a mail-ingest kind: the supervisor must skip it on
        startup *and* on reconcile, so it never silently polls a mailbox it
        has no transport for. The durable row stays untouched — the honest
        "unsupported" degradation, not a hidden worker."""
        row = await harness.registry.connections.create_connection(
            kind="graph_mail",
            label="Outlook (OAuth)",
            config={"account": "graph@example.com"},
            auth_status="authenticated",
            enabled=True,
        )
        supervisor = _supervisor(harness)
        await supervisor.start()
        # the fixture's imap connection gets its worker — the graph_mail
        # row must not
        assert harness.conn.id in supervisor.workers
        assert row.id not in supervisor.workers

        # a reconcile pass over durable state must not spawn one either
        await supervisor.reconcile_once()
        assert row.id not in supervisor.workers
        await supervisor.stop()

        # and the durable row was left alone — no operational movement
        after = await harness.registry.connections.get_connection(row.id)
        assert after.operational_status == "never_synced"
        assert after.enabled is True
