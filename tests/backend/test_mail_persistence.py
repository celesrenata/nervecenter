"""Durable mail-ingest persistence + person resolution (P1-107).

These assertions are NEW — the existing ``communication``/``people``/``ingest``
suites only encode the in-memory contract. Here we prove the durable half of
the plane the production-activation task closes:

(i)   a mail ingest produces a durable ``people`` row AND a durable
      ``communications`` row whose non-null ``person_id`` FK resolves;
(ii)  a restart/replay (re-run the same poll) creates NO duplicate people or
      communications (idempotent deterministic ids);
(iii) a simulated durable-write failure does NOT advance the cursor and the
      message is re-attempted on the next poll;
(iv)  ``GET /api/communications`` and ``GET /api/people`` serve the durably
      -persisted records (durable-first reads), response shape unchanged;
(v)   ``GET /api/communications/search`` returns 200 and matches persisted
      data;
(vi)  import_jobs transition to the terminal (``done``) state on a successful
      durable poll (they used to stick in ``running``).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import StaticPool

from backend.api.app import create_app
from backend.communications.registry import reset_communications_service
from backend.connectors.mail.mail_transport import MailItem, StubMailTransport
from backend.ingest.mail_worker import (
    DefaultIngestSeam,
    MailIngestWorker,
    set_communications_store,
)
from backend.ingest.seen import seen_keys
from backend.persistence.connection import make_session_factory
from backend.persistence.models.personal import (
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
from backend.storage.memory import InMemoryObjectStore

# Every table the durable ingest path touches (communication plane + ingest).
_TABLES = [
    PersonRow.__table__,
    PersonIdentityRow.__table__,
    CommunicationRow.__table__,
    ConversationRow.__table__,
    ConnectionRow.__table__,
    SecretRow.__table__,
    IngestCursorRow.__table__,
    ImportJobRow.__table__,
]

_ACCOUNT = "user@example.com"
_SECRET = "super-secret-password"
_TS = datetime(2026, 10, 27, 9, 0, 0, tzinfo=UTC)


def _make_item(message_id: str, *, sender: str = "Boss Person <boss@corp.com>") -> MailItem:
    return MailItem(
        message_id=message_id,
        account=_ACCOUNT,
        subject="Deploy by Friday",
        content_bytes=b"Please deploy by Friday\n",
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
    """Durable SQLite registry + in-memory object store + one imap connection,
    with a fresh in-memory communications service and the durable store bound
    through the production seam (``set_communications_store``)."""
    reset_communications_service()
    engine = await _make_engine(tmp_path / "persist.sqlite")
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
    # The production seam persists durably when a store is bound; bind the same
    # repository the endpoint reads so worker + endpoint share one plane.
    set_communications_store(registry.communications)
    yield SimpleNamespace(engine=engine, registry=registry, store=store, conn=conn)
    set_communications_store(None)
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
        seam=seam if seam is not None else DefaultIngestSeam(
            communications_store=harness.registry.communications
        ),
        poll_interval_s=30.0,
    )


# --------------------------------------------------------------------------
# (i) durable people + communications with a resolving FK
# --------------------------------------------------------------------------

class TestDurablePersist:
    async def test_ingest_persists_person_and_communication(self, harness) -> None:
        transport = StubMailTransport(items=[_make_item("<1@corp>")], next_cursor={"uid": 5})
        worker = _worker(harness, transport=transport)
        await worker.poll_once()
        assert worker.status()["last"]["status"] == "ok"

        people = await harness.registry.communications.list_people()
        assert len(people) == 1
        person = people[0]
        assert person.display_name == "Boss Person"

        comms = await harness.registry.communications.list_communications()
        assert len(comms) == 1
        comm = comms[0]
        assert comm.person_id == person.id  # non-null FK that resolves
        assert comm.channel == "email"
        assert comm.direction == "incoming"
        assert comm.content_ref.startswith("content://mail/")
        assert comm.subject == "Deploy by Friday"

        # identity + conversation were written too
        identities = await harness.registry.communications.list_identities(person.id)
        assert [i.handle for i in identities] == ["boss@corp.com"]

    async def test_bare_address_sender_resolves(self, harness) -> None:
        transport = StubMailTransport(
            items=[_make_item("<2@corp>", sender="plain@corp.com")],
            next_cursor={"uid": 6},
        )
        await _worker(harness, transport=transport).poll_once()
        comms = await harness.registry.communications.list_communications()
        assert len(comms) == 1
        assert comms[0].person_id is not None


# --------------------------------------------------------------------------
# (ii)/(iii) idempotent replay + commit-gate on durable failure
# --------------------------------------------------------------------------

class TestReplayAndGate:
    async def test_restart_replay_creates_no_duplicates(self, harness) -> None:
        # first poll ingests one message durably
        first = _worker(
            harness,
            transport=StubMailTransport(items=[_make_item("<1@corp>")], next_cursor={"uid": 9}),
        )
        await first.poll_once()

        # "restart": a cold in-memory plane + a fresh seam, same message replayed.
        reset_communications_service()
        second = _worker(
            harness,
            transport=StubMailTransport(items=[_make_item("<1@corp>")], next_cursor={"uid": 9}),
        )
        await second.poll_once()

        # deterministic ids made the durable write a no-op: still exactly one row each
        assert len(await harness.registry.communications.list_people()) == 1
        assert len(await harness.registry.communications.list_communications()) == 1

    async def test_durable_failure_does_not_advance_cursor(self, harness) -> None:
        class FailingStore:
            """Wraps the real store but fails the communication insert on the
            first call only — the message must be retried on the next poll."""

            def __init__(self, inner: Any) -> None:
                self._inner = inner
                self.fail_next = True

            async def get_person(self, pid: str) -> Any:
                return await self._inner.get_person(pid)

            async def create_person(self, **kw: Any) -> Any:
                return await self._inner.create_person(**kw)

            async def upsert_person_identity(self, **kw: Any) -> Any:
                return await self._inner.upsert_person_identity(**kw)

            async def get_communications_by_ids(self, ids: list[str]) -> Any:
                return await self._inner.get_communications_by_ids(ids)

            async def upsert_conversation(self, **kw: Any) -> Any:
                return await self._inner.upsert_conversation(**kw)

            async def create_communication(self, **kw: Any) -> Any:
                if self.fail_next:
                    self.fail_next = False
                    raise RuntimeError("durable insert boom")
                return await self._inner.create_communication(**kw)

        class ReplayTransport:
            """Re-delivers the same message every poll — the real transport
            behavior once the cursor has NOT advanced."""

            def __init__(self, items: list[MailItem], next_cursor: dict[str, Any]) -> None:
                self._items = list(items)
                self._next_cursor = dict(next_cursor)

            async def run(self, connector, cursor, *, credentials=None):
                return list(self._items), dict(self._next_cursor)

        failing = FailingStore(harness.registry.communications)
        seam = DefaultIngestSeam(communications_store=failing)
        transport = ReplayTransport(items=[_make_item("<7@corp>")], next_cursor={"uid": 42})
        worker = _worker(harness, transport=transport, seam=seam)

        await worker.poll_once()
        summary = worker.status()["last"]
        assert summary["status"] == "error"
        assert summary["error_code"] == "commit_failed"

        # cursor did NOT advance and the seen-list did not fold the message
        row = await harness.registry.imports.get_cursor(harness.conn.id, "imap_seq")
        advanced = row is not None and (row.cursor_value or {}).get("uid") == 42
        assert not advanced

        # next poll retries the same message and now succeeds durably
        await worker.poll_once()
        assert worker.status()["last"]["status"] == "ok"
        comms = await harness.registry.communications.list_communications()
        assert len(comms) == 1
        row = await harness.registry.imports.get_cursor(harness.conn.id, "imap_seq")
        assert row.cursor_value["uid"] == 42
        assert seen_keys(row.cursor_value) == [f"{_ACCOUNT}:<7@corp>"]


# --------------------------------------------------------------------------
# (vi) honest import-job completion
# --------------------------------------------------------------------------

class TestImportJobCompletion:
    async def test_successful_durable_poll_completes_import_job(self, harness) -> None:
        transport = StubMailTransport(items=[_make_item("<1@corp>")], next_cursor={"uid": 3})
        worker = _worker(harness, transport=transport)
        await worker.poll_once()

        conn = await harness.registry.connections.get_connection(harness.conn.id)
        job_id = conn.config.get("import_job_id")
        assert job_id
        job = await harness.registry.imports.get_import_job(job_id)
        assert job is not None
        assert job.state == "done"  # frozen spelling of "completed"
        assert job.finished_at is not None
        assert int(job.progress.get("ingested", 0)) == 1


# --------------------------------------------------------------------------
# (iv)/(v) durable-first reads + search over the HTTP surface
# --------------------------------------------------------------------------

class TestDurableFirstReads:
    @pytest.fixture
    def client(self, harness):
        app = create_app()
        app.state.repository_registry = harness.registry
        app.state.communications_service = __import__(
            "backend.communications", fromlist=["get_communications_service"]
        ).get_communications_service()
        with TestClient(app) as c:
            # TestClient's lifespan wiring would rebind state; re-apply the
            # durable registry + store after startup so the reads are durable.
            app.state.repository_registry = harness.registry
            set_communications_store(harness.registry.communications)
            yield c

    async def _ingest_one(self, harness) -> None:
        transport = StubMailTransport(items=[_make_item("<1@corp>")], next_cursor={"uid": 5})
        await _worker(harness, transport=transport).poll_once()

    async def test_communications_and_people_served_durable_first(
        self, harness, client
    ) -> None:
        await self._ingest_one(harness)

        res = client.get("/api/communications")
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["meta"]["pagination"]["total"] == 1
        record = body["data"][0]
        # response shape unchanged (frozen Communication keys present)
        for key in (
            "communication_id",
            "person_id",
            "channel",
            "direction",
            "content_ref",
            "timestamp",
            "sensitivity",
        ):
            assert key in record
        assert record["channel"] == "email"
        assert record["person_id"] is not None

        res = client.get("/api/people")
        assert res.status_code == 200, res.text
        people = res.json()["data"]
        assert len(people) == 1
        assert people[0]["display_name"] == "Boss Person"
        for key in ("person_id", "display_name", "is_vip", "identities"):
            assert key in people[0]

    async def test_restart_cold_memory_serves_durable(self, harness, client) -> None:
        await self._ingest_one(harness)
        # simulate a pod restart: the in-memory plane is empty, durable is not
        reset_communications_service()

        res = client.get("/api/communications")
        assert res.status_code == 200
        assert res.json()["meta"]["pagination"]["total"] == 1

        res = client.get("/api/people")
        assert res.status_code == 200
        assert len(res.json()["data"]) == 1

    async def test_search_endpoint_matches_persisted_data(self, harness, client) -> None:
        await self._ingest_one(harness)
        res = client.get("/api/communications/search", params={"q": "deploy", "within_week": False})
        assert res.status_code == 200, res.text
        item = res.json()["item"]
        assert item["total"] >= 1
        assert any(c["channel"] == "email" for c in item["communications"])
