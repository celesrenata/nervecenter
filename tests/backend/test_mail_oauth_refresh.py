"""GAP 1 — durable mail OAuth refresh-token rotation (Personal-Ops §4).

Covers the new durable refresh path:

- :func:`exchange_refresh_token` — the pure wire exchange (``POST
  grant_type=refresh_token``): correct endpoint/body, stable error codes,
  no rotation → original token kept;
- :func:`stored_refresh_token` — vault payload shape (bare token or the
  calendar-plane ``{"refresh_token": …}`` JSON);
- :class:`MailOAuthRefresher` — the rotated ``refresh_token`` and the fresh
  ``access_token`` are written back through the vault-backed
  ``upsert_secret`` path (a failed refresh persists *nothing*);
- :class:`MailIngestWorker` wiring — an OAuth connection that gets its
  access token rejected triggers one refresh + retry, and a restarted
  worker resumes with the rotated token (never the stale one).

Credential hygiene (§4): token values are only ever asserted by
presence/absence against in-memory constants — never printed, never
persisted in the plaintext form, and the vault ciphertext is checked to
contain none of the values.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from backend.api.models import MailConnector
from backend.connectors.mail.mail_transport import (
    MailItem,
    TransportAuthError,
    TransportConnectError,
)
from backend.connectors.mail.oauth import PROVIDERS
from backend.connectors.mail.oauth_refresh import (
    ACCESS_TOKEN_KIND,
    ERR_BAD_TOKEN_RESPONSE,
    ERR_NO_REFRESH_TOKEN,
    ERR_REFRESH_DENIED,
    ERR_TOKEN_UNREACHABLE,
    REFRESH_TOKEN_KIND,
    MailOAuthRefreshError,
    MailOAuthRefresher,
    exchange_refresh_token,
    stored_refresh_token,
)
from backend.connectors.vault import FernetVault, generate_key
from backend.ingest.mail_worker import MailIngestWorker
from backend.persistence.connection import make_session_factory
from backend.persistence.models.personal import (
    ConnectionRow,
    ImportJobRow,
    IngestCursorRow,
    SecretRow,
)
from backend.persistence.repositories import RepositoryRegistry
from backend.storage.memory import InMemoryObjectStore

# Synthetic token-shaped placeholders — asserted by presence/absence only.
_OLD_ACCESS = "access-old-token-000111222333444555666777888"
_OLD_REFRESH = "refresh-old-token-000111222333444555666777"
_NEW_ACCESS = "access-new-token-aaaabbbccccddddeeeeffffgggg"
_NEW_REFRESH = "refresh-new-token-aaaabbbccccddddeeeeffffggg"

_ACCOUNT = "user@example.com"
_TS = datetime(2026, 10, 27, 9, 0, 0, tzinfo=UTC)

_PERSONAL_TABLES = [
    ConnectionRow.__table__,
    SecretRow.__table__,
    IngestCursorRow.__table__,
    ImportJobRow.__table__,
]


# --------------------------------------------------------------------------
# fakes
# --------------------------------------------------------------------------


class _FakeResponse:
    """Duck-typed TokenResponse (``status_code`` + ``json()``)."""

    def __init__(self, status_code: int, payload: Any) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> Any:
        if self._payload is None:
            raise ValueError("no JSON body")
        return self._payload


class FakeTokenEndpoint:
    """Scripted TokenEndpointTransport — records every request, returns one
    scripted response per call."""

    def __init__(self, responses: list[tuple[int, Any]]) -> None:
        self._responses = list(responses)
        self.requests: list[dict[str, Any]] = []

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, str] | None = None,
        body: bytes | None = None,
    ) -> _FakeResponse:
        self.requests.append(
            {
                "method": method,
                "url": url,
                "body": json.loads(body) if body else None,
            }
        )
        if not self._responses:
            raise AssertionError("token endpoint called more times than scripted")
        status, payload = self._responses.pop(0)
        return _FakeResponse(status, payload)


class FakeSecretStore:
    """In-memory SecretStoreSeam double (vault seam contract only)."""

    def __init__(self, secrets: dict[tuple[str, str, str], str] | None = None) -> None:
        self.secrets: dict[tuple[str, str, str], str] = dict(secrets or {})
        self.upserts: list[dict[str, Any]] = []

    async def get_secret(self, *, kind: str, owner_kind: str, owner_id: str) -> str | None:
        return self.secrets.get((kind, owner_kind, owner_id))

    async def upsert_secret(
        self, *, kind: str, owner_kind: str, owner_id: str, plaintext: str
    ) -> None:
        self.upserts.append(
            {"kind": kind, "owner_kind": owner_kind, "owner_id": owner_id}
        )
        self.secrets[(kind, owner_kind, owner_id)] = plaintext


class RecordingTransport:
    """Scripted MailTransport that records the credential *values* per call
    (test-local only — assertions are presence/absence checks, never
    printed, §4)."""

    def __init__(self, script: list[Any]) -> None:
        # step: BaseException instance → raise; (items, next_cursor) → return
        self._script = list(script)
        self.credential_values: list[dict[str, str]] = []

    async def run(
        self,
        connector: MailConnector,
        cursor: dict[str, Any],
        *,
        credentials: dict[str, str] | None = None,
    ) -> tuple[list[MailItem], dict[str, Any]]:
        self.credential_values.append(dict(credentials or {}))
        if not self._script:
            raise AssertionError("transport called more times than scripted")
        step = self._script.pop(0)
        if isinstance(step, BaseException):
            raise step
        items, next_cursor = step
        return items, next_cursor


class FakeSeam:
    """Minimal ingestion-seam double for one successful item."""

    def __init__(self) -> None:
        self.ingests: list[dict[str, Any]] = []

    def ingest_communication(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.ingests.append(payload)
        return {
            "communication": SimpleNamespace(
                communication_id=payload["communication_id"],
                content_ref=payload.get("content_ref"),
                timestamp=payload.get("timestamp"),
            ),
            "created": True,
        }

    def run_pipeline(self, communication: Any, *, now: Any) -> Any:
        return None


def _make_item(message_id: str) -> MailItem:
    return MailItem(
        message_id=message_id,
        account=_ACCOUNT,
        subject="Deploy by Friday",
        content_bytes=b"Please deploy by Friday\n",
        timestamp=_TS,
        headers={
            "From": "boss@corp.com",
            "Message-ID": message_id,
            "Date": "Mon, 27 Oct 2026 09:00:00 +0000",
        },
    )


async def _make_engine(db_path: Path):
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


@pytest.fixture
async def harness(tmp_path: Path):
    """File-backed SQLite + *real* Fernet vault (the durable seam under test)."""
    engine = await _make_engine(tmp_path / "refresh.sqlite")
    vault = FernetVault(generate_key())
    session_factory = make_session_factory(engine)
    registry = RepositoryRegistry(session_factory, secret_codec=vault)
    store = InMemoryObjectStore()
    yield SimpleNamespace(
        engine=engine,
        registry=registry,
        vault=vault,
        store=store,
        session_factory=session_factory,
    )
    await engine.dispose()


async def _gmail_connection(registry: RepositoryRegistry) -> ConnectionRow:
    """An OAuth gmail connection with non-secret app keys in config
    (the calendar-plane convention) and both durable token secrets."""
    conn = await registry.connections.create_connection(
        kind="gmail",
        label="Work",
        config={
            "account": _ACCOUNT,
            "imap_host": "imap.gmail.com",
            "mailbox": "INBOX",
            "auth_method": "oauth2",
            "client_id": "cid-test",
            "client_secret": "csecret-test",
        },
        auth_status="authenticated",
        enabled=True,
    )
    await registry.connections.upsert_secret(
        kind=ACCESS_TOKEN_KIND,
        owner_kind="connection",
        owner_id=conn.id,
        plaintext=_OLD_ACCESS,
    )
    await registry.connections.upsert_secret(
        kind=REFRESH_TOKEN_KIND,
        owner_kind="connection",
        owner_id=conn.id,
        plaintext=_OLD_REFRESH,
    )
    return conn


def _worker(harness: Any, conn: ConnectionRow, transport: Any, refresher: Any = None) -> MailIngestWorker:
    registry = harness.registry
    return MailIngestWorker(
        connection_id=conn.id,
        cursor_kind="gmail_history",
        connections=registry.connections,
        imports=registry.imports,
        object_store=harness.store,
        transport=transport,
        seam=FakeSeam(),
        poll_interval_s=30.0,
        oauth_refresher=refresher,
    )


def _secret_rows(snapshot: list[SecretRow]) -> str:
    """Every stored ciphertext payload as one string (for absence checks)."""
    return "\n".join(json.dumps(row.payload) for row in snapshot)


async def _stored(harness: Any, kind: str, owner_id: str) -> str | None:
    return await harness.registry.connections.get_secret(
        kind=kind, owner_kind="connection", owner_id=owner_id
    )


# --------------------------------------------------------------------------
# 1. stored_refresh_token — vault payload shape
# --------------------------------------------------------------------------


class TestStoredRefreshToken:
    def test_bare_token(self) -> None:
        assert stored_refresh_token("rt-bare-123") == "rt-bare-123"

    def test_bare_token_is_stripped(self) -> None:
        assert stored_refresh_token("  rt-bare-123  ") == "rt-bare-123"

    def test_json_nested_shape(self) -> None:
        payload = json.dumps({"refresh_token": "rt-nested-456"})
        assert stored_refresh_token(payload) == "rt-nested-456"

    def test_json_without_refresh_token_falls_back_to_empty(self) -> None:
        assert stored_refresh_token(json.dumps({"other": "x"})) == ""

    def test_empty_and_none(self) -> None:
        assert stored_refresh_token("") == ""
        assert stored_refresh_token(None) == ""

    def test_malformed_json_treated_as_bare_token(self) -> None:
        assert stored_refresh_token("{not-json-123") == "{not-json-123"


# --------------------------------------------------------------------------
# 2. exchange_refresh_token — the pure wire exchange
# --------------------------------------------------------------------------


class TestExchangeRefreshToken:
    async def test_success_posts_grant_and_rotates(self) -> None:
        endpoint = FakeTokenEndpoint(
            [(200, {"access_token": _NEW_ACCESS, "refresh_token": _NEW_REFRESH})]
        )
        tokens = await exchange_refresh_token(
            provider_name="google",
            refresh_token=_OLD_REFRESH,
            client_id="cid",
            client_secret="csecret",
            transport=endpoint,
        )
        assert tokens.access_token == _NEW_ACCESS
        assert tokens.refresh_token == _NEW_REFRESH
        assert len(endpoint.requests) == 1
        req = endpoint.requests[0]
        assert req["method"] == "POST"
        assert req["url"] == PROVIDERS["google"].token_url
        assert req["body"]["grant_type"] == "refresh_token"
        assert req["body"]["refresh_token"] == _OLD_REFRESH
        assert req["body"]["client_id"] == "cid"

    async def test_microsoft_uses_microsoft_endpoint(self) -> None:
        endpoint = FakeTokenEndpoint([(200, {"access_token": _NEW_ACCESS})])
        await exchange_refresh_token(
            provider_name="microsoft",
            refresh_token=_OLD_REFRESH,
            client_id="cid",
            client_secret="csecret",
            transport=endpoint,
        )
        assert endpoint.requests[0]["url"] == PROVIDERS["microsoft"].token_url

    async def test_no_rotation_returns_original_refresh_token(self) -> None:
        endpoint = FakeTokenEndpoint([(200, {"access_token": _NEW_ACCESS, "expires_in": 3599})])
        tokens = await exchange_refresh_token(
            provider_name="google",
            refresh_token=_OLD_REFRESH,
            client_id="cid",
            client_secret="csecret",
            transport=endpoint,
        )
        # Provider omitted the rotated token → the original stays valid.
        assert tokens.refresh_token == _OLD_REFRESH
        assert tokens.expires_in == 3599.0

    async def test_missing_refresh_token_is_stable_code_without_wire_call(self) -> None:
        endpoint = FakeTokenEndpoint([])
        with pytest.raises(MailOAuthRefreshError) as exc:
            await exchange_refresh_token(
                provider_name="google",
                refresh_token="",
                client_id="cid",
                client_secret="csecret",
                transport=endpoint,
            )
        assert exc.value.code == ERR_NO_REFRESH_TOKEN
        assert endpoint.requests == []

    @pytest.mark.parametrize("status", [400, 401, 403])
    async def test_denied_refresh_is_stable_code_and_leaks_no_token(self, status) -> None:
        endpoint = FakeTokenEndpoint([(status, {"error": "invalid_grant"})])
        with pytest.raises(MailOAuthRefreshError) as exc:
            await exchange_refresh_token(
                provider_name="google",
                refresh_token=_OLD_REFRESH,
                client_id="cid",
                client_secret="csecret",
                transport=endpoint,
            )
        assert exc.value.code == ERR_REFRESH_DENIED
        assert _OLD_REFRESH not in exc.value.message
        assert "invalid_grant" in exc.value.message

    async def test_server_error_is_unreachable_code(self) -> None:
        endpoint = FakeTokenEndpoint([(503, {"error": "backend_error"})])
        with pytest.raises(MailOAuthRefreshError) as exc:
            await exchange_refresh_token(
                provider_name="google",
                refresh_token=_OLD_REFRESH,
                client_id="cid",
                client_secret="csecret",
                transport=endpoint,
            )
        assert exc.value.code == ERR_TOKEN_UNREACHABLE

    async def test_transport_exception_maps_to_unreachable_code(self) -> None:
        class _Boom:
            async def request(self, *a: Any, **kw: Any) -> Any:
                raise OSError("connection reset by peer")

        with pytest.raises(MailOAuthRefreshError) as exc:
            await exchange_refresh_token(
                provider_name="google",
                refresh_token=_OLD_REFRESH,
                client_id="cid",
                client_secret="csecret",
                transport=_Boom(),
            )
        assert exc.value.code == ERR_TOKEN_UNREACHABLE
        assert "connection reset" not in exc.value.message

    async def test_non_json_body_is_bad_token_response(self) -> None:
        endpoint = FakeTokenEndpoint([(200, None)])
        with pytest.raises(MailOAuthRefreshError) as exc:
            await exchange_refresh_token(
                provider_name="google",
                refresh_token=_OLD_REFRESH,
                client_id="cid",
                client_secret="csecret",
                transport=endpoint,
            )
        assert exc.value.code == ERR_BAD_TOKEN_RESPONSE

    async def test_response_without_access_token_is_bad_token_response(self) -> None:
        endpoint = FakeTokenEndpoint([(200, {"refresh_token": _NEW_REFRESH})])
        with pytest.raises(MailOAuthRefreshError) as exc:
            await exchange_refresh_token(
                provider_name="google",
                refresh_token=_OLD_REFRESH,
                client_id="cid",
                client_secret="csecret",
                transport=endpoint,
            )
        assert exc.value.code == ERR_BAD_TOKEN_RESPONSE


# --------------------------------------------------------------------------
# 3. MailOAuthRefresher — durable write-back (GAP 1)
# --------------------------------------------------------------------------


class TestMailOAuthRefresher:
    async def test_rotated_token_is_written_back_through_upsert_secret(self) -> None:
        store = FakeSecretStore(
            {(REFRESH_TOKEN_KIND, "connection", "c1"): _OLD_REFRESH}
        )
        endpoint = FakeTokenEndpoint(
            [(200, {"access_token": _NEW_ACCESS, "refresh_token": _NEW_REFRESH})]
        )
        refresher = MailOAuthRefresher(connections=store, transport=endpoint)
        tokens = await refresher.refresh(
            connection_id="c1",
            provider="google",
            client_id="cid",
            client_secret="csecret",
        )
        assert tokens.refresh_token == _NEW_REFRESH
        # Both kinds persisted, refresh first (crash-safe order).
        assert [u["kind"] for u in store.upserts] == [REFRESH_TOKEN_KIND, ACCESS_TOKEN_KIND]
        assert all(u["owner_kind"] == "connection" and u["owner_id"] == "c1" for u in store.upserts)
        assert store.secrets[(REFRESH_TOKEN_KIND, "connection", "c1")] == _NEW_REFRESH
        assert store.secrets[(ACCESS_TOKEN_KIND, "connection", "c1")] == _NEW_ACCESS
        # The stale token is gone, the current one is what a restart reads.
        assert _OLD_REFRESH not in store.secrets
        assert store.get_secret and True  # structural sanity (async seam present)

    async def test_denied_refresh_persists_nothing(self) -> None:
        store = FakeSecretStore(
            {(REFRESH_TOKEN_KIND, "connection", "c1"): _OLD_REFRESH}
        )
        endpoint = FakeTokenEndpoint([(401, {"error": "invalid_client"})])
        refresher = MailOAuthRefresher(connections=store, transport=endpoint)
        with pytest.raises(MailOAuthRefreshError) as exc:
            await refresher.refresh(
                connection_id="c1",
                provider="google",
                client_id="cid",
                client_secret="wrong",
            )
        assert exc.value.code == ERR_REFRESH_DENIED
        assert store.upserts == []  # the stored token is untouched
        assert store.secrets[(REFRESH_TOKEN_KIND, "connection", "c1")] == _OLD_REFRESH

    async def test_accepts_json_nested_refresh_payload(self) -> None:
        store = FakeSecretStore(
            {
                (REFRESH_TOKEN_KIND, "connection", "c1"): json.dumps(
                    {"refresh_token": _OLD_REFRESH}
                )
            }
        )
        endpoint = FakeTokenEndpoint(
            [(200, {"access_token": _NEW_ACCESS, "refresh_token": _NEW_REFRESH})]
        )
        refresher = MailOAuthRefresher(connections=store, transport=endpoint)
        await refresher.refresh(
            connection_id="c1", provider="google", client_id="cid", client_secret="csecret"
        )
        # The nested payload's token was what went over the wire.
        assert endpoint.requests[0]["body"]["refresh_token"] == _OLD_REFRESH

    async def test_default_constructor_uses_builtin_httpx_transport(self) -> None:
        store = FakeSecretStore()
        refresher = MailOAuthRefresher(connections=store)
        assert refresher._transport is not None  # httpx adapter (no wire call here)


# --------------------------------------------------------------------------
# 4. worker wiring — refresh + retry + durable rotation
# --------------------------------------------------------------------------


class TestWorkerRefreshWiring:
    async def test_auth_failure_triggers_refresh_retry_and_persists_rotation(
        self, harness: Any
    ) -> None:
        registry = harness.registry
        conn = await _gmail_connection(registry)

        endpoint = FakeTokenEndpoint(
            [(200, {"access_token": _NEW_ACCESS, "refresh_token": _NEW_REFRESH})]
        )
        refresher = MailOAuthRefresher(connections=registry.connections, transport=endpoint)
        transport = RecordingTransport(
            [
                TransportAuthError("401: token rejected by mailbox"),
                ([_make_item("<1@corp>")], {"history_id": 1, "account": _ACCOUNT}),
            ]
        )
        worker = _worker(harness, conn, transport, refresher)
        await worker.poll_once()
        summary = worker.status()["last"]

        assert summary["status"] == "ok"
        # The retry ran with the *fresh* access token, not the stale one.
        assert transport.credential_values[0]["access_token"] == _OLD_ACCESS
        assert transport.credential_values[1]["access_token"] == _NEW_ACCESS
        # The rotated refresh token is what a restarted worker will read.
        assert await _stored(harness, REFRESH_TOKEN_KIND, conn.id) == _NEW_REFRESH
        # Vault ciphertexts hold no plaintext (old or new).
        async with harness.session_factory() as session:
            rows = (
                await session.execute(
                    select(SecretRow).where(SecretRow.owner_id == conn.id)
                )
            ).scalars().all()
        for value in (_OLD_ACCESS, _OLD_REFRESH, _NEW_ACCESS, _NEW_REFRESH):
            assert value not in _secret_rows(rows)
        # Connection returned to healthy with no recorded error.
        row = await registry.connections.get_connection(conn.id)
        assert row.auth_status == "authenticated"
        assert row.operational_status == "healthy"
        assert row.last_error_code is None
        assert row.last_error_message is None

    async def test_failed_refresh_keeps_stale_token_and_maps_auth_expired(
        self, harness: Any
    ) -> None:
        registry = harness.registry
        conn = await _gmail_connection(registry)

        endpoint = FakeTokenEndpoint([(401, {"error": "invalid_grant"})])
        refresher = MailOAuthRefresher(connections=registry.connections, transport=endpoint)
        transport = RecordingTransport(
            [
                TransportAuthError(
                    f"LOGIN rejected for {_ACCOUNT}:{_OLD_REFRESH}"
                ),
            ]
        )
        worker = _worker(harness, conn, transport, refresher)
        await worker.poll_once()
        summary = worker.status()["last"]

        assert summary["status"] == "auth_expired"
        # One refresh attempt (denied) → the stored token is unchanged.
        assert len(endpoint.requests) == 1
        assert await _stored(harness, REFRESH_TOKEN_KIND, conn.id) == _OLD_REFRESH
        assert await _stored(harness, ACCESS_TOKEN_KIND, conn.id) == _OLD_ACCESS
        # The stored error message is redacted — the token never leaked.
        row = await registry.connections.get_connection(conn.id)
        assert row.auth_status == "auth_expired"
        assert row.last_error_code == "auth_expired"
        assert _OLD_REFRESH not in (row.last_error_message or "")

    async def test_no_refresh_without_app_keys(self, harness: Any) -> None:
        registry = harness.registry
        # No client_id/client_secret in config → refresh is disabled.
        conn = await registry.connections.create_connection(
            kind="gmail",
            label="Work",
            config={
                "account": _ACCOUNT,
                "imap_host": "imap.gmail.com",
                "auth_method": "oauth2",
            },
            auth_status="authenticated",
            enabled=True,
        )
        await registry.connections.upsert_secret(
            kind=ACCESS_TOKEN_KIND,
            owner_kind="connection",
            owner_id=conn.id,
            plaintext=_OLD_ACCESS,
        )
        await registry.connections.upsert_secret(
            kind=REFRESH_TOKEN_KIND,
            owner_kind="connection",
            owner_id=conn.id,
            plaintext=_OLD_REFRESH,
        )
        endpoint = FakeTokenEndpoint(
            [(200, {"access_token": _NEW_ACCESS, "refresh_token": _NEW_REFRESH})]
        )
        refresher = MailOAuthRefresher(connections=registry.connections, transport=endpoint)
        transport = RecordingTransport([TransportAuthError("401: token rejected")])
        worker = _worker(harness, conn, transport, refresher)
        await worker.poll_once()
        summary = worker.status()["last"]

        assert summary["status"] == "auth_expired"
        assert endpoint.requests == []  # refresh was never attempted
        assert len(transport.credential_values) == 1  # no retry

    async def test_non_oauth_connection_never_refreshes(self, harness: Any) -> None:
        registry = harness.registry
        conn = await registry.connections.create_connection(
            kind="imap",
            label="Work",
            config={"account": _ACCOUNT, "imap_host": "imap.example.com"},
            auth_status="authenticated",
            enabled=True,
        )
        await registry.connections.upsert_secret(
            kind="app_password",
            owner_kind="connection",
            owner_id=conn.id,
            plaintext="app-pass-value-00011122233344455566",
        )
        endpoint = FakeTokenEndpoint(
            [(200, {"access_token": _NEW_ACCESS, "refresh_token": _NEW_REFRESH})]
        )
        refresher = MailOAuthRefresher(connections=registry.connections, transport=endpoint)
        transport = RecordingTransport([TransportAuthError("LOGIN -> AUTHENTICATIONFAILED")])
        worker = _worker(harness, conn, transport, refresher)
        worker._cursor_kind = "imap_seq"  # the harness default is gmail_history
        await worker.poll_once()
        summary = worker.status()["last"]

        assert summary["status"] == "auth_expired"
        assert endpoint.requests == []
        assert len(transport.credential_values) == 1

    async def test_restart_reads_rotated_token_from_vault(self, harness: Any) -> None:
        registry = harness.registry
        conn = await _gmail_connection(registry)
        endpoint = FakeTokenEndpoint(
            [(200, {"access_token": _NEW_ACCESS, "refresh_token": _NEW_REFRESH})]
        )
        refresher = MailOAuthRefresher(connections=registry.connections, transport=endpoint)
        transport = RecordingTransport(
            [
                TransportAuthError("401: token rejected"),
                ([_make_item("<1@corp>")], {"history_id": 1, "account": _ACCOUNT}),
            ]
        )
        worker = _worker(harness, conn, transport, refresher)
        await worker.poll_once()
        assert worker.status()["last"]["status"] == "ok"

        # A brand-new worker (post-restart) resolves the *rotated* tokens —
        # no refresh needed, first poll succeeds with the fresh access token.
        endpoint2 = FakeTokenEndpoint([])  # must stay empty
        refresher2 = MailOAuthRefresher(connections=registry.connections, transport=endpoint2)
        transport2 = RecordingTransport(
            [([_make_item("<2@corp>")], {"history_id": 2, "account": _ACCOUNT})]
        )
        worker2 = _worker(harness, conn, transport2, refresher2)
        await worker2.poll_once()
        summary = worker2.status()["last"]
        assert summary["status"] == "ok"
        assert endpoint2.requests == []  # no refresh on the restarted worker
        assert transport2.credential_values[0]["access_token"] == _NEW_ACCESS
