"""Durable mail-ingest plane tests (Personal-Ops Recovery §4 — workstream C).

Covers the three seams the spec freezes:

1. **Transport** — :class:`ImapPollClient` (stdlib :mod:`imaplib`) and
   :class:`GmailClient` (``httpx``) resume from an opaque cursor, advance it,
   and map credential vs. network failures onto :class:`TransportAuthError` /
   :class:`TransportConnectError`. The credential is a *separate*
   ``credentials`` keyword, never folded into the cursor.
2. **Worker** — one :class:`MailIngestWorker` per enabled connection re-reads
   the durable cursor/secret every poll, dedups by the cursor-embedded
   ``seen`` list (restart-safe), stores raw bytes in the object store, and
   funnels through the existing communication ingestion seam.
3. **Supervisor / wiring** — :func:`install_ingest_supervisor` /
   :func:`stop_ingest_supervisor` start one worker per enabled mail connection
   from durable state and degrade honestly when prerequisites are absent.

Credential hygiene (§4/§6) is asserted throughout: the plaintext secret never
appears in a persisted cursor, a connection row, a seen-list entry, or an
object-store key/metadata.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import StaticPool

from backend.api.models import MailConnector
from backend.api.wiring import install_ingest_supervisor, stop_ingest_supervisor
from backend.communications.models import (
    CommunicationChannel,
    CommunicationDirection,
)
from backend.connectors.mail.mail_transport import (
    GmailClient,
    ImapPollClient,
    MailItem,
    StubMailTransport,
    TransportAuthError,
    TransportConnectError,
)
from backend.ingest import (
    MAX_SEEN_KEYS,
    MailIngestWorker,
    cursor_with_seen,
    is_seen,
    mark_seen,
    seen_keys,
)
from backend.ingest.supervisor import SUPERVISOR_STATE_KEY
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

# Only the four personal-plane tables the worker actually touches.
_PERSONAL_TABLES = [
    ConnectionRow.__table__,
    SecretRow.__table__,
    IngestCursorRow.__table__,
    ImportJobRow.__table__,
]

_ACCOUNT = "user@example.com"
_SECRET = "super-secret-password"
_TS = datetime(2026, 10, 27, 9, 0, 0, tzinfo=UTC)


# --------------------------------------------------------------------------
# fakes
# --------------------------------------------------------------------------

def _make_item(message_id: str, *, sender: str = "boss@corp.com") -> MailItem:
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


class FakeSeam:
    """Records the existing ingestion-seam calls the worker makes."""

    def __init__(self, *, created: bool = True) -> None:
        self.created = created
        self.ingests: list[dict[str, Any]] = []
        self.pipeline: list[tuple[Any, Any]] = []

    def ingest_communication(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.ingests.append(payload)
        communication = SimpleNamespace(
            communication_id=payload["communication_id"],
            content_ref=payload.get("content_ref"),
            channel=payload.get("channel"),
            timestamp=payload.get("timestamp"),
        )
        return {"communication": communication, "conversation": None, "created": self.created}

    def run_pipeline(self, communication: Any, *, now: Any) -> Any:
        self.pipeline.append((communication, now))
        return None


class ReplayTransport:
    """A transport that re-delivers the *same* items on every poll — the
    replay case the cursor-embedded seen-list must dedup."""

    def __init__(self, items: list[MailItem], next_cursor: dict[str, Any]) -> None:
        self._items = list(items)
        self._next_cursor = dict(next_cursor)
        self.calls: list[dict[str, Any]] = []

    async def run(
        self,
        connector: MailConnector,
        cursor: dict[str, Any],
        *,
        credentials: dict[str, str] | None = None,
    ) -> tuple[list[MailItem], dict[str, Any]]:
        self.calls.append(
            {
                "cursor": dict(cursor),
                "credential_keys": sorted(credentials or {}),
            }
        )
        return list(self._items), dict(self._next_cursor)


# --------------------------------------------------------------------------
# durable fixtures
# --------------------------------------------------------------------------

async def _make_engine(db_path: Path) -> AsyncEngine:
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{db_path}",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda sc: _PERSONAL_TABLES[0].metadata.create_all(
                sc, tables=_PERSONAL_TABLES
            )
        )
    return engine


def _build_registry(engine: AsyncEngine) -> RepositoryRegistry:
    return RepositoryRegistry(
        make_session_factory(engine),
        secret_codec=IdentitySecretCodec(),
    )


@pytest.fixture
async def harness(tmp_path: Path):
    """A durable SQLite registry + in-memory object store + one imap connection."""
    engine = await _make_engine(tmp_path / "ingest.sqlite")
    registry = _build_registry(engine)
    store = InMemoryObjectStore()
    conn = await registry.connections.create_connection(
        kind="imap",
        label="Work",
        config={
            "account": _ACCOUNT,
            "imap_host": "127.0.0.1",
            "mailbox": "INBOX",
        },
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
    yield SimpleNamespace(engine=engine, registry=registry, store=store, conn=conn)
    await engine.dispose()


def _worker(
    registry: RepositoryRegistry,
    store: InMemoryObjectStore,
    conn: ConnectionRow,
    *,
    transport: Any,
    seam: FakeSeam,
) -> MailIngestWorker:
    return MailIngestWorker(
        connection_id=conn.id,
        cursor_kind="imap_seq",
        connections=registry.connections,
        imports=registry.imports,
        object_store=store,
        transport=transport,
        seam=seam,
        poll_interval_s=30.0,
    )


# --------------------------------------------------------------------------
# 1. pure seen-list helpers
# --------------------------------------------------------------------------

class TestSeenHelpers:
    def test_seen_keys_none_is_empty(self) -> None:
        assert seen_keys(None) == []
        assert seen_keys({}) == []
        assert seen_keys({"seen": ["a", "b"]}) == ["a", "b"]

    def test_seen_keys_does_not_mutate_input(self) -> None:
        cursor = {"uid": 3, "seen": ["a"]}
        seen_keys(cursor)
        assert cursor == {"uid": 3, "seen": ["a"]}

    def test_mark_seen_is_idempotent_and_appends(self) -> None:
        seen = ["a"]
        assert mark_seen(seen, "a") == ["a"]
        assert mark_seen(seen, "b") == ["a", "b"]

    def test_mark_seen_caps_to_max_most_recent(self) -> None:
        seen = [f"k{i}" for i in range(MAX_SEEN_KEYS)]
        result = mark_seen(seen, "newest")
        assert len(result) == MAX_SEEN_KEYS
        assert result[-1] == "newest"
        assert result[0] == "k1"  # oldest dropped
        assert "k0" not in result

    def test_is_seen(self) -> None:
        assert is_seen(["a", "b"], "a")
        assert not is_seen(["a", "b"], "c")

    def test_cursor_with_seen_is_non_mutating(self) -> None:
        cursor = {"uid": 10}
        out = cursor_with_seen(cursor, ["x", "y"])
        assert out == {"uid": 10, "seen": ["x", "y"]}
        assert "seen" not in cursor  # input untouched
        # and it round-trips through seen_keys
        assert seen_keys(out) == ["x", "y"]


# --------------------------------------------------------------------------
# 2. worker poll / dedup / restart semantics (real durable repos)
# --------------------------------------------------------------------------

class TestWorkerPoll:
    async def test_happy_path_ingests_and_advances_cursor(self, harness) -> None:
        seam = FakeSeam()
        transport = StubMailTransport(
            items=[_make_item("<1@corp>"), _make_item("<2@corp>")],
            next_cursor={"uid": 99},
        )
        worker = _worker(harness.registry, harness.store, harness.conn, transport=transport, seam=seam)

        await worker.poll_once()
        summary = worker.status()["last"]

        assert summary["status"] == "ok"
        assert summary["ingested"] == 2
        assert summary["duplicates"] == 0

        # cursor advanced AND carries the seen list
        row = await harness.registry.imports.get_cursor(harness.conn.id, "imap_seq")
        assert row is not None
        assert row.cursor_value["uid"] == 99
        assert sorted(row.cursor_value["seen"]) == [
            f"{_ACCOUNT}:<1@corp>",
            f"{_ACCOUNT}:<2@corp>",
        ]

        # connection flipped to healthy / authenticated
        conn = await harness.registry.connections.get_connection(harness.conn.id)
        assert conn.auth_status == "authenticated"
        assert conn.operational_status == "healthy"
        assert conn.last_error_code is None

        # raw bytes went to the object store; the record keeps only a content_ref
        assert len(seam.ingests) == 2
        payload = seam.ingests[0]
        assert payload["channel"] is CommunicationChannel.EMAIL
        assert payload["direction"] is CommunicationDirection.INCOMING
        assert payload["content_ref"].startswith(f"content://mail/{harness.conn.id}/")
        stored = await harness.store.get(payload["content_ref"].split("/", 2)[2])
        assert stored == b"Please deploy by Friday\n"

        # an import job was recorded and its id pinned in the connection config
        assert conn.config.get("import_job_id")
        job = await harness.registry.imports.get_import_job(conn.config["import_job_id"])
        assert job is not None
        assert job.state == "running"

    async def test_replay_is_deduped_by_cursor_seen_list(self, harness) -> None:
        seam = FakeSeam()
        replay = ReplayTransport(
            items=[_make_item("<1@corp>"), _make_item("<2@corp>")],
            next_cursor={"uid": 99},
        )
        worker = _worker(harness.registry, harness.store, harness.conn, transport=replay, seam=seam)

        await worker.poll_once()
        first = worker.status()["last"]
        await worker.poll_once()
        second = worker.status()["last"]

        assert first["ingested"] == 2
        assert first["duplicates"] == 0
        assert second["ingested"] == 0
        assert second["duplicates"] == 2
        # the seam was called exactly once per message — no re-ingest on replay
        assert len(seam.ingests) == 2
        # the seen list is idempotent (still two entries, capped)
        row = await harness.registry.imports.get_cursor(harness.conn.id, "imap_seq")
        assert len(row.cursor_value["seen"]) == 2

    async def test_restart_resumes_from_db_not_memory(self, harness) -> None:
        """A fresh worker over the same durable DB must not re-import mail the
        previous process already ingested (spec §4: restart from DB state)."""
        seam_a = FakeSeam()
        first = _worker(
            harness.registry, harness.store, harness.conn,
            transport=StubMailTransport(items=[_make_item("<1@corp>")], next_cursor={"uid": 55}),
            seam=seam_a,
        )
        await first.poll_once()

        # "restart": a brand-new worker + seam, a transport that replays the
        # same message. The durable cursor/seen-list must suppress re-ingest.
        seam_b = FakeSeam()
        replay = ReplayTransport(items=[_make_item("<1@corp>")], next_cursor={"uid": 55})
        second = _worker(harness.registry, harness.store, harness.conn, transport=replay, seam=seam_b)
        await second.poll_once()
        summary = second.status()["last"]

        assert summary["ingested"] == 0
        assert summary["duplicates"] == 1
        assert len(seam_b.ingests) == 0  # the fresh process ingested nothing
        # the durable cursor survived the "restart" with the seen key
        row = await harness.registry.imports.get_cursor(harness.conn.id, "imap_seq")
        assert row.cursor_value["uid"] == 55
        assert seen_keys(row.cursor_value) == [f"{_ACCOUNT}:<1@corp>"]

    async def test_credentials_are_passed_but_never_persisted(self, harness) -> None:
        seam = FakeSeam()
        transport = StubMailTransport(items=[_make_item("<1@corp>")], next_cursor={"uid": 7})
        worker = _worker(harness.registry, harness.store, harness.conn, transport=transport, seam=seam)
        await worker.poll_once()

        # the secret was handed to the transport as the separate credentials kw
        call = transport.calls[0]
        assert call["credential_keys"] == ["password", "username"]
        # ...but the plaintext is nowhere in durable state
        row = await harness.registry.imports.get_cursor(harness.conn.id, "imap_seq")
        assert _SECRET not in str(row.cursor_value)
        conn = await harness.registry.connections.get_connection(harness.conn.id)
        assert _SECRET not in str(conn.config)
        # object-store metadata is secret-free too
        payload = seam.ingests[0]
        key = payload["content_ref"].split("/", 2)[2]
        meta = await harness.store.head(key)
        assert meta is not None
        assert _SECRET not in str(meta.metadata)

    async def test_auth_error_maps_to_auth_expired(self, harness) -> None:
        transport = StubMailTransport(raise_exc=TransportAuthError("LOGIN -> AUTHENTICATIONFAILED"))
        worker = _worker(harness.registry, harness.store, harness.conn, transport=transport, seam=FakeSeam())
        await worker.poll_once()
        summary = worker.status()["last"]
        assert summary["status"] == "auth_expired"
        conn = await harness.registry.connections.get_connection(harness.conn.id)
        assert conn.auth_status == "auth_expired"
        assert conn.operational_status == "error"
        assert conn.last_error_code == "auth_expired"

    async def test_connect_error_maps_to_connect_failed(self, harness) -> None:
        transport = StubMailTransport(raise_exc=TransportConnectError("no route to host"))
        worker = _worker(harness.registry, harness.store, harness.conn, transport=transport, seam=FakeSeam())
        await worker.poll_once()
        summary = worker.status()["last"]
        assert summary["status"] == "error"
        conn = await harness.registry.connections.get_connection(harness.conn.id)
        assert conn.operational_status == "error"
        assert conn.last_error_code == "connect_failed"
        assert "no route to host" in conn.last_error_message
        # auth status is NOT touched by a connect failure
        assert conn.auth_status == "authenticated"

    async def test_missing_secret_is_a_connect_failure(self, harness) -> None:
        # remove the secret so _resolve_credentials raises
        await harness.registry.connections.delete_secret(
            kind="app_password", owner_kind="connection", owner_id=harness.conn.id
        )
        transport = StubMailTransport(items=[_make_item("<1@corp>")])
        worker = _worker(harness.registry, harness.store, harness.conn, transport=transport, seam=FakeSeam())
        await worker.poll_once()
        summary = worker.status()["last"]
        assert summary["status"] == "error"
        conn = await harness.registry.connections.get_connection(harness.conn.id)
        assert conn.last_error_code == "connect_failed"

    async def test_disabled_connection_is_skipped(self, harness) -> None:
        await harness.registry.connections.update_connection(
            harness.conn.id, enabled=False
        )
        transport = StubMailTransport(items=[_make_item("<1@corp>")])
        worker = _worker(harness.registry, harness.store, harness.conn, transport=transport, seam=FakeSeam())
        await worker.poll_once()
        summary = worker.status()["last"]
        assert summary["status"] == "disabled"
        assert transport.calls == []


# --------------------------------------------------------------------------
# 3. transport seams (IMAP + Gmail) — cursor advancement + error mapping
# --------------------------------------------------------------------------

class _FakeImap:
    def __init__(
        self,
        *,
        login: Any = ("OK", [b"OK"]),
        uids: bytes = b"101 102",
        raw: bytes = b"",
    ) -> None:
        self._login = login
        self._uids = uids
        self._raw = raw
        self.logged_in = None
        self.untagged_responses: dict[str, list[bytes]] = {}

    def login(self, user, password):
        self.logged_in = (user, password)
        return self._login

    def select(self, mailbox):
        # B3: report the folder's UIDVALIDITY from the SELECT untagged response
        # so the transport can capture it into the durable cursor.
        self.untagged_responses = {"UIDVALIDITY": [b"7"]}
        return "OK", [b"2"]

    def uid(self, op, *args):
        if op == "SEARCH":
            return "OK", [self._uids]
        # FETCH
        return "OK", [(b"1 (BODY.PEEK[] {5})", self._raw)]

    def logout(self):
        return "OK", [b"BYE"]


def _imap_raw() -> bytes:
    return (
        b"From: boss@corp.com\r\n"
        b"To: user@example.com\r\n"
        b"Subject: Deploy by Friday\r\n"
        b"Date: Mon, 27 Oct 2026 09:00:00 +0000\r\n"
        b"Message-ID: <uid-101@corp>\r\n"
        b"\r\n"
        b"Deploy by Friday\n"
    )


def _connector(**kw: Any) -> MailConnector:
    base = dict(connector_id="c-1", account="Work", mailbox=_ACCOUNT, host="imap.example.com", port=993)
    base.update(kw)
    return MailConnector(**base)


class TestImapTransport:
    async def test_advances_uid_and_ignores_unknown_cursor_keys(self, monkeypatch) -> None:
        import backend.connectors.mail.mail_transport as mt

        seen = ["a"]  # the worker folds a seen list into the cursor
        monkeypatch.setattr(mt.imaplib, "IMAP4_SSL", lambda host, port, timeout=None: _FakeImap(raw=_imap_raw()))
        client = ImapPollClient()
        items, cursor = await client.run(
            _connector(), {"uid": 100, "seen": seen}, credentials={"username": _ACCOUNT, "password": _SECRET}
        )
        assert len(items) == 2
        # B1/B3/B5: the durable cursor is per (connection, folder) — the
        # explicit INBOX folder, the captured UIDVALIDITY, and the folded
        # seen-list (dedup of the re-emitted uid is the seen-list's job).
        assert cursor == {
            "uid": 102,
            "uidvalidity": 7,
            "folder": "INBOX",
            "seen": ["a", f"{_ACCOUNT}:<uid-101@corp>"],
        }
        assert seen == ["a"]  # input not mutated

    async def test_auth_failure_raises_transport_auth_error(self, monkeypatch) -> None:
        import backend.connectors.mail.mail_transport as mt

        monkeypatch.setattr(
            mt.imaplib, "IMAP4_SSL",
            lambda host, port, timeout=None: _FakeImap(login=("AUTHENTICATIONFAILED", [b"NO"])),
        )
        client = ImapPollClient()
        with pytest.raises(TransportAuthError):
            await client.run(_connector(), {"uid": 0}, credentials={"username": "u", "password": "p"})

    async def test_connect_failure_raises_transport_connect_error(self, monkeypatch) -> None:
        import backend.connectors.mail.mail_transport as mt

        def _boom(host, port, timeout=None):
            raise ConnectionError("no route to host")

        monkeypatch.setattr(mt.imaplib, "IMAP4_SSL", _boom)
        client = ImapPollClient()
        with pytest.raises(TransportConnectError):
            await client.run(_connector(), {"uid": 0}, credentials={"username": "u", "password": "p"})


class _FakeResp:
    def __init__(self, status: int, body: Any) -> None:
        self.status_code = status
        self._body = body

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}")


class _FakeGmailClient:
    """httpx.AsyncClient-shaped fake for the corrected Gmail wire contract.

    ``history.start`` returns a valid baseline ``historyId``; ``history.get``
    serves the configured entries (never a bare startHistoryId on the first
    sync); per-message fetches serve a base64url plain-text payload."""

    def __init__(
        self, *, history: Any, history_status: int = 200, start_id: int = 70
    ) -> None:
        self._history = history
        self._history_status = history_status
        self._start_id = start_id
        self.tok_seen: list[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, url, *, headers=None, params=None):
        auth = (headers or {}).get("Authorization", "")
        self.tok_seen.append(auth)
        if url.endswith("/history.start"):
            return _FakeResp(self._history_status, {"historyId": self._start_id})
        if url.endswith("/users/me/history"):
            return _FakeResp(self._history_status, self._history)
        # per-message fetch
        return _FakeResp(200, {
            "payload": {
                "mimeType": "text/plain; charset=utf-8",
                "headers": [
                    {"name": "From", "value": "boss@corp.com"},
                    {"name": "Subject", "value": "Hi"},
                    {"name": "Date", "value": "Mon, 27 Oct 2026 09:00:00 +0000"},
                ],
                # Real Gmail wire shape: body.data is base64url ("Hi there").
                "body": {"data": "SGkgdGhlcmU="},
            }
        })


class TestGmailTransport:
    async def test_advances_history_id_and_returns_account(self, monkeypatch) -> None:
        import backend.connectors.mail.mail_transport as mt

        history = {"history": [{"id": 77, "messages": [{"id": "m1"}]}]}
        observed: list[str] = []

        class _Recorder(_FakeGmailClient):
            def __init__(self, **kw: Any) -> None:
                super().__init__(**kw)
                self._observed = observed

            async def get(self, url, *, headers=None, params=None):
                auth = (headers or {}).get("Authorization", "")
                self._observed.append(auth)
                return await super().get(url, headers=headers, params=params)

        # First sync: history.start gives the valid baseline (70), then
        # history.get polls from it; the cursor advances to the last
        # processed entry and the per-message fetch decodes the base64url body.
        monkeypatch.setattr(mt.httpx, "AsyncClient", lambda **kw: _Recorder(history=history))
        client = GmailClient()
        items, cursor = await client.run(
            _connector(mailbox=""), {}, credentials={"access_token": "tok-abc"}
        )
        assert len(items) == 1
        assert items[0].message_id == "gmail:m1"
        assert items[0].content_bytes == b"Hi there"
        assert cursor == {"history_id": 77, "account": "", "seen": [":gmail:m1"]}
        # the transient access_token is sent as a bearer but never returned in the cursor
        assert observed and all(auth == "Bearer tok-abc" for auth in observed)
        assert "tok-abc" not in str(cursor)

    async def test_auth_failure_maps_to_transport_auth_error(self, monkeypatch) -> None:
        import backend.connectors.mail.mail_transport as mt

        monkeypatch.setattr(
            mt.httpx, "AsyncClient",
            lambda **kw: _FakeGmailClient(history={"history": []}, history_status=401),
        )
        client = GmailClient()
        with pytest.raises(TransportAuthError):
            await client.run(_connector(mailbox=""), {}, credentials={"access_token": "bad"})


# --------------------------------------------------------------------------
# 4. supervisor / app-lifespan wiring
# --------------------------------------------------------------------------

class TestSupervisorWiring:
    def _target(self, registry: RepositoryRegistry | None) -> SimpleNamespace:
        return SimpleNamespace(
            state=SimpleNamespace(repository_registry=registry, db_engine=None, nats_client=None)
        )

    async def test_install_degrades_without_toggle(self, harness) -> None:
        monkeypatch_env: dict[str, str] = {}
        import os

        prev = os.environ.pop("NERVE_INGEST_ENABLED", None)
        try:
            supervisor = await install_ingest_supervisor(self._target(harness.registry))
            assert supervisor is None
        finally:
            if prev is not None:
                os.environ["NERVE_INGEST_ENABLED"] = prev

    async def test_install_degrades_without_registry_or_store(self, monkeypatch) -> None:
        monkeypatch.setenv("NERVE_INGEST_ENABLED", "1")
        monkeypatch.delenv("NERVECENTER_OBJECT_STORE_URL", raising=False)

        # no durable registry → skip (honest "not configured" degradation)
        assert await install_ingest_supervisor(self._target(None)) is None
        # durable registry but no object store → skip
        assert await install_ingest_supervisor(self._target(_dummy_registry())) is None
        # object store configured but still no registry → skip
        monkeypatch.setenv("NERVECENTER_OBJECT_STORE_URL", "memory://test")
        assert await install_ingest_supervisor(self._target(None)) is None

    async def test_one_worker_per_enabled_mail_connection(self, harness, monkeypatch) -> None:
        monkeypatch.setenv("NERVE_INGEST_ENABLED", "1")
        monkeypatch.setenv("NERVECENTER_OBJECT_STORE_URL", "memory://test")

        # a non-mail + a disabled connection must NOT get workers
        await harness.registry.connections.create_connection(kind="calendar", enabled=True)
        await harness.registry.connections.create_connection(kind="imap", enabled=False)

        target = self._target(harness.registry)
        supervisor = await install_ingest_supervisor(target)

        assert supervisor is not None
        assert getattr(target.state, SUPERVISOR_STATE_KEY) is supervisor
        assert set(supervisor.workers) == {harness.conn.id}
        worker = supervisor.worker_for(harness.conn.id)
        assert worker is not None

        await stop_ingest_supervisor(target)
        assert getattr(target.state, SUPERVISOR_STATE_KEY) is None
        assert supervisor.workers == {}

    async def test_wire_dependencies_installs_and_teardown_stops(
        self, harness, monkeypatch
    ) -> None:
        """The app-lifespan path: ``wire_dependencies`` installs the plane from
        durable state; ``teardown_dependencies`` stops it before disposing the
        engine (order matters — workers hold the session factory)."""
        import backend.api.wiring as wiring

        monkeypatch.setenv("NERVE_INGEST_ENABLED", "1")
        monkeypatch.setenv("NERVECENTER_OBJECT_STORE_URL", "memory://test")
        monkeypatch.setattr(wiring, "build_db_engine", lambda: harness.engine)

        async def _no_nats(target: Any) -> None:
            return None

        async def _no_services(engine: Any) -> dict[str, Any]:
            return {}

        monkeypatch.setattr(wiring, "_build_and_connect_nats", _no_nats)
        monkeypatch.setattr(wiring, "build_services", _no_services)

        target = self._target(None)
        target.state.db_engine = None
        target.state.services = {}
        await wiring.wire_dependencies(target)

        supervisor = getattr(target.state, SUPERVISOR_STATE_KEY, None)
        assert supervisor is not None
        assert set(supervisor.workers) == {harness.conn.id}

        await wiring.teardown_dependencies(target)
        assert getattr(target.state, SUPERVISOR_STATE_KEY) is None
        assert target.state.db_engine is None


def _dummy_registry() -> RepositoryRegistry:
    """A registry whose identity passes the isinstance check but is never
    queried (used only for the store-missing degradation path)."""
    return RepositoryRegistry.__new__(RepositoryRegistry)
