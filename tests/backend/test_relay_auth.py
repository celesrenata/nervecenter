"""P0-E: connector authorization regressions (relay + connections).

These tests lock in three previously-missing guarantees:

1. **Relay ingest / import are token-authenticated.**
   ``POST /api/relay/messages`` and ``POST /api/relay/import-request`` must
   verify the incoming ``Authorization: Bearer <token>`` against the *exact*
   relay device's registered credential (constant-time). A missing header,
   a wrong/invalid token, a cross-device token, or a cross-user token is all
   a generic 401 — the error body never reveals which check failed.

2. **Connection Center handlers require an access token.**
   Every ``/api/connections*`` handler (read and write) enforces the
   app's existing ``current_access`` dependency; without a valid access
   token every route is a 401/403.

3. **CORS never allows wildcard origins with credentials.**
   ``allow_origins`` comes from the explicit ``NC_CORS_ORIGINS`` env var
   (comma-separated), defaulting to the deployed origin(s) or empty. The
   wildcard+credentials combination is structurally impossible.

The relay plane runs against a **test double** for the durable registry /
secrets store (no live device, no real IMAP/OAuth). The access-token
dependency runs against the real ``AuthContext`` with the in-process
session store, exactly like ``tests/backend/test_auth_sso.py``.
"""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

import backend.api.auth_service as auth_service
from backend.api.app import create_app
from backend.api.auth import current_access
from backend.api.auth_service import AuthContext
from backend.connectors.registry import ConnectionControlPlane
from backend.connectors.vault import FernetVault, generate_key

# HS256 RFC-7518-recommended minimum key length.
JWT_SECRET = "relay-auth-test-" + "k" * 48
JWT_ISSUER = "https://sso.test.example/application/o/nervecenter/"


# ──────────────────────────────────────────────────────────────────────────────
# Test double for the durable registry / secrets store (no live devices).
# Implements only the ``.connections`` / ``.imports`` / ``.communications``
# surface used by the relay control-plane methods under test.
# ──────────────────────────────────────────────────────────────────────────────


class FakeConnections:
    """In-memory stand-in for ``RepositoryRegistry.connections``."""

    def __init__(self) -> None:
        self._conns: list[Any] = []
        self._secrets: dict[tuple[str, str, str], str] = {}

    async def create_connection(
        self,
        *,
        kind: str,
        label: str | None = None,
        config: dict[str, Any] | None = None,
        auth_status: str = "unconfigured",
        operational_status: str = "never_synced",
        enabled: bool = True,
    ) -> SimpleNamespace:
        import uuid

        row = SimpleNamespace(
            id=str(uuid.uuid4()),
            kind=kind,
            label=label,
            config=dict(config or {}),
            auth_status=auth_status,
            operational_status=operational_status,
            enabled=enabled,
            last_success_at=None,
            last_attempt_at=None,
            last_error_code=None,
            last_error_message=None,
            import_progress={},
            created_at=None,
            updated_at=None,
            schema_version="v0",
        )
        self._conns.append(row)
        return row

    async def update_connection(self, connection_id: str, **kwargs: Any) -> None:
        for row in self._conns:
            if row.id == connection_id:
                if "auth_status" in kwargs:
                    row.auth_status = kwargs["auth_status"]
                if "operational_status" in kwargs:
                    row.operational_status = kwargs["operational_status"]
                if "config" in kwargs and kwargs["config"] is not None:
                    row.config = dict(kwargs["config"])
                if "import_progress" in kwargs and kwargs["import_progress"] is not None:
                    row.import_progress = dict(kwargs["import_progress"])
                if "last_attempt_at" in kwargs:
                    row.last_attempt_at = kwargs["last_attempt_at"]
                if "last_success_at" in kwargs:
                    row.last_success_at = kwargs["last_success_at"]
                if "last_error_code" in kwargs:
                    row.last_error_code = kwargs["last_error_code"]
                if "last_error_message" in kwargs:
                    row.last_error_message = kwargs["last_error_message"]
        return None

    async def get_connection(self, connection_id: str) -> SimpleNamespace | None:
        for row in self._conns:
            if row.id == connection_id:
                return row
        return None

    async def list_connections(self, *, enabled_only: bool = False) -> list[Any]:
        return [r for r in self._conns if (r.enabled or not enabled_only)]

    async def upsert_secret(
        self, *, kind: str, owner_kind: str, owner_id: str, plaintext: str
    ) -> SimpleNamespace:
        self._secrets[(kind, owner_kind, owner_id)] = plaintext
        return SimpleNamespace()

    async def get_secret(
        self, *, kind: str, owner_kind: str, owner_id: str
    ) -> str | None:
        return self._secrets.get((kind, owner_kind, owner_id))

    async def delete_secret(
        self, *, kind: str, owner_kind: str, owner_id: str
    ) -> None:
        self._secrets.pop((kind, owner_kind, owner_id), None)


class FakeImports:
    """In-memory stand-in for the import/cursor repositories."""

    def __init__(self) -> None:
        self._cursors: dict[tuple[str, str], dict[str, Any]] = {}
        self._jobs: list[Any] = []
        self._seq = 0
        self._job_by_conn: dict[str, list[Any]] = {}

    async def get_cursor(self, connection_id: str, kind: str):
        value = self._cursors.get((connection_id, kind))
        if value is None:
            return None
        return SimpleNamespace(cursor_value=dict(value))

    async def set_cursor(self, connection_id: str, kind: str, value: dict[str, Any]) -> None:
        self._cursors[(connection_id, kind)] = dict(value)

    async def create_import_job(self, connection_id: str, kind: str, *, state: str = "pending"):
        self._seq += 1
        job = SimpleNamespace(
            id=f"job-{self._seq}",
            kind=kind,
            state=state,
            progress={},
            error_code=None,
            started_at=None,
            finished_at=None,
        )
        self._jobs.append(job)
        self._job_by_conn.setdefault(connection_id, []).append(job)
        return job

    async def count_import_jobs(
        self, connection_id: str, *, states: tuple[str, ...] = ("pending", "running")
    ) -> int:
        return sum(
            1
            for job in self._job_by_conn.get(connection_id, [])
            if job.state in states
        )

    async def get_import_job(self, job_id: str):
        for job in self._jobs:
            if job.id == job_id:
                return job
        return None

    async def update_import_job_progress(
        self, job_id: str, *, progress: dict[str, Any]
    ) -> None:
        for job in self._jobs:
            if job.id == job_id:
                job.progress = dict(progress)


class FakeSeen:
    """In-memory stand-in for the durable ``relay_seen`` repository (workstream H)."""

    def __init__(self) -> None:
        # (connection_id, device_id) -> set of message_ids
        self._seen: dict[tuple[str, str], set[str]] = {}

    async def is_seen(self, connection_id: str, device_id: str, message_id: str) -> bool:
        return message_id in self._seen.get((connection_id, device_id), set())

    async def mark_seen(
        self,
        connection_id: str,
        device_id: str,
        message_id: str,
        content_ref: str | None = None,
    ):
        key = (connection_id, device_id)
        self._seen.setdefault(key, set()).add(message_id)
        return SimpleNamespace(
            connection_id=connection_id,
            device_id=device_id,
            message_id=message_id,
            content_ref=content_ref,
        )

    async def count_seen(self, connection_id: str, device_id: str) -> int:
        return len(self._seen.get((connection_id, device_id), set()))


class FakeCommunications:
    """In-memory stand-in for ``RepositoryRegistry.communications``.

    The relay ingest path (``_relay_store_message`` / ``_resolve_person``)
    writes persons, identities, communications and conversations; this fake
    records them so the durable ingest completes against the test double.
    """

    def __init__(self) -> None:
        self._persons: dict[str, SimpleNamespace] = {}
        self._identities: list[Any] = []
        self._communications: list[Any] = []
        self._conversations: list[Any] = []
        self._seq = 0

    async def create_person(
        self,
        *,
        id: str | None = None,
        display_name: str,
        vip: bool = False,
        default_sensitivity: str | None = None,
        notes: dict[str, Any] | None = None,
        boundaries: dict[str, Any] | None = None,
    ) -> SimpleNamespace:
        self._seq += 1
        person = SimpleNamespace(
            id=id or f"person-{self._seq}",
            display_name=display_name,
            vip=vip,
            default_sensitivity=default_sensitivity,
            notes=notes or {},
            boundaries=boundaries or {},
        )
        self._persons[person.id] = person
        return person

    async def get_person(self, person_id: str):
        return self._persons.get(person_id)

    async def upsert_person_identity(
        self,
        *,
        person_id: str,
        channel: str,
        handle: str,
        resolved: bool = False,
    ) -> SimpleNamespace:
        identity = SimpleNamespace(
            person_id=person_id, channel=channel, handle=handle, resolved=resolved
        )
        self._identities.append(identity)
        return identity

    async def create_communication(
        self,
        *,
        person_id: str,
        channel: str,
        direction: str,
        content_ref: str,
        subject: str | None = None,
        sensitivity: str | None = None,
        received_at: Any = None,
        flags: dict[str, Any] | None = None,
    ) -> SimpleNamespace:
        self._seq += 1
        comm = SimpleNamespace(
            id=f"comm-{self._seq}",
            person_id=person_id,
            channel=channel,
            direction=direction,
            content_ref=content_ref,
            subject=subject,
            sensitivity=sensitivity,
            received_at=received_at,
            flags=flags or {},
        )
        self._communications.append(comm)
        return comm

    async def upsert_conversation(
        self,
        *,
        person_id: str,
        channel: str,
        last_message_at: Any = None,
        unread_count: int = 0,
    ) -> SimpleNamespace:
        self._seq += 1
        conversation = SimpleNamespace(
            id=f"conv-{self._seq}",
            person_id=person_id,
            channel=channel,
            last_message_at=last_message_at,
            unread_count=unread_count,
        )
        self._conversations.append(conversation)
        return conversation


def _digest(device_id: str, message_id: str, body: str) -> str:
    """Stable content_ref digest (mirrors the control plane's formula)."""
    return hashlib.sha256(f"{device_id}:{message_id}:{body}".encode("utf-8")).hexdigest()


class FakeRegistry:
    """Wires the fake connection + import + communication stores the control plane expects."""

    def __init__(self) -> None:
        self.connections = FakeConnections()
        self.imports = FakeImports()
        self.communications = FakeCommunications()
        self.relay_seen = FakeSeen()


# ──────────────────────────────────────────────────────────────────────────────
# Fixtures / helpers
# ──────────────────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch):
    for name in (
        "NERVE_JWT_SECRET",
        "NERVE_SSO_ISSUER",
        "NERVE_SSO_CLIENT_ID",
        "NERVE_LOCAL_ADMIN",
        "NERVE_LOCAL_ADMIN_PASSWORD",
        "NC_CORS_ORIGINS",
    ):
        monkeypatch.delenv(name, raising=False)
    auth_service.reset_memory_store()
    yield


def _auth_ctx() -> AuthContext:
    return AuthContext(
        secret=JWT_SECRET,
        issuer=JWT_ISSUER,
        client_id="unit",
        local_admin_enabled=False,
        local_admin_password=None,
        session_factory=None,
    )


async def _mint_access_token(sub: str = "user-a") -> str:
    """Mint a valid, session-backed access token for the in-process store."""
    ctx = _auth_ctx()
    token, exp, claims = ctx._mint(
        sub=sub, typ="access", roles=["admin"], ttl=auth_service.ACCESS_TOKEN_TTL
    )
    await auth_service.record_session(
        ctx, jti=claims["jti"], sub=sub, typ="access", expires_at=exp, source="test"
    )
    return token


async def _mint_expired_access_token() -> str:
    """Mint an already-expired access token (negative TTL, then restore).

    The session row is still recorded so *only* expiry fails verification —
    this isolates the expiry check from signature/session checks.
    """
    from datetime import timedelta

    ctx = _auth_ctx()
    real_ttl = auth_service.ACCESS_TOKEN_TTL
    try:
        auth_service.ACCESS_TOKEN_TTL = timedelta(seconds=-1)
        token, _exp, claims = ctx._mint(
            sub="user-a", typ="access", roles=["admin"], ttl=auth_service.ACCESS_TOKEN_TTL
        )
    finally:
        auth_service.ACCESS_TOKEN_TTL = real_ttl
    past = datetime.now(UTC) - timedelta(seconds=10)
    await auth_service.record_session(
        ctx, jti=claims["jti"], sub="user-a", typ="access", expires_at=past, source="test"
    )
    return token


@pytest.fixture
def relay_env():
    """Wire the real relay router onto a bare app backed by the test double."""
    vault = FernetVault(generate_key())
    fake = FakeRegistry()
    plane = ConnectionControlPlane(fake, vault=vault)
    app = FastAPI()
    from backend.api.endpoints import relay

    app.include_router(relay.router)
    app.state.connection_control_plane = plane
    return app, plane


def _device(
    plane: ConnectionControlPlane, *, name: str, user_sub: str = "user-a"
) -> tuple[str, str, str]:
    """Create + register a relay device; returns ``(device_id, token, conn_id)``.

    Uses the real control-plane ``relay_pair`` / ``relay_register`` over the
    test double so the pairing-token → credential path is exercised end to
    end. The owning user is recorded on the connection config (non-secret).
    """
    async def _setup() -> tuple[str, str, str]:
        pair = await plane.relay_pair(device_name=name)
        device_id = pair["device_id"]
        token = pair["token"]
        register = await plane.relay_register(device_id=device_id, token=token)
        conn_id = register["id"]
        for row in await plane._registry.connections.list_connections():
            if row.id == conn_id:
                row.config["owner_user"] = user_sub
        return device_id, token, conn_id

    # The control plane is async; drive the pair/register setup on a private
    # event loop so the sync HTTP tests can mint a device before they open a
    # TestClient (which runs the app on its own loop).
    return asyncio.run(_setup())


@pytest.fixture
def connections_env():
    """Wire the real connections router + a real AuthContext onto a bare app."""
    app = FastAPI()
    from backend.api.endpoints import connections

    app.include_router(connections.router)
    app.state.auth_ctx = _auth_ctx()
    return app


def _no_auth(app: FastAPI, method: str, path: str, body: dict[str, Any] | None) -> Any:
    with TestClient(app) as client:
        fn = getattr(client, method.lower())
        if body is not None:
            return fn(path, json=body)
        return fn(path)


def _protected_app() -> FastAPI:
    """A minimal app whose single route is guarded by ``current_access``."""
    app = FastAPI()
    app.state.auth_ctx = _auth_ctx()

    @app.get("/protected")
    async def protected(
        claims: dict[str, Any] = Depends(current_access),
    ) -> dict[str, Any]:
        return {"ok": True, "sub": claims.get("sub")}

    return app


# ──────────────────────────────────────────────────────────────────────────────
# 1. Relay ingest is token-authenticated (constant-time, exact device)
# ──────────────────────────────────────────────────────────────────────────────


class TestRelayIngestAuth:
    def test_missing_authorization_header_rejected(self, relay_env):
        app, plane = relay_env
        device_id, _token, _ = _device(plane, name="Device A")
        with TestClient(app) as client:
            resp = client.post(
                "/api/relay/messages",
                json={"device_id": device_id, "messages": [{"message_id": "m-1", "body": "hi"}]},
            )
        assert resp.status_code == 401

    def test_invalid_token_rejected(self, relay_env):
        app, plane = relay_env
        device_id, _token, _ = _device(plane, name="Device A")
        with TestClient(app) as client:
            resp = client.post(
                "/api/relay/messages",
                json={"device_id": device_id, "messages": [{"message_id": "m-1", "body": "hi"}]},
                headers={"Authorization": "Bearer not-a-valid-token"},
            )
        assert resp.status_code == 401

    def test_cross_device_token_rejected(self, relay_env):
        """A valid token from device B presented with device A's id → 401."""
        app, plane = relay_env
        dev_a, _tok_a, _ = _device(plane, name="Device A")
        dev_b, tok_b, _ = _device(plane, name="Device B")
        with TestClient(app) as client:
            resp = client.post(
                "/api/relay/messages",
                json={"device_id": dev_a, "messages": [{"message_id": "m-1", "body": "hi"}]},
                headers={"Authorization": f"Bearer {tok_b}"},
            )
        assert resp.status_code == 401

    def test_cross_user_token_rejected(self, relay_env):
        """A token from a different user's device for this device → 401."""
        app, plane = relay_env
        dev_a, _tok_a, _ = _device(plane, name="Device A", user_sub="user-a")
        dev_b, tok_b, _ = _device(plane, name="Device B", user_sub="user-b")
        with TestClient(app) as client:
            resp = client.post(
                "/api/relay/messages",
                json={"device_id": dev_a, "messages": [{"message_id": "m-1", "body": "hi"}]},
                headers={"Authorization": f"Bearer {tok_b}"},
            )
        assert resp.status_code == 401

    def test_valid_token_accepted(self, relay_env):
        app, plane = relay_env
        device_id, token, _ = _device(plane, name="Device A")
        with TestClient(app) as client:
            resp = client.post(
                "/api/relay/messages",
                json={"device_id": device_id, "messages": [{"message_id": "m-1", "body": "hi"}]},
                headers={"Authorization": f"Bearer {token}"},
            )
        assert resp.status_code == 200
        assert resp.json().get("ok") is True
        assert resp.json().get("stored") == 1

    def test_seen_table_dedup_survives_cursor_reset(self, relay_env):
        """The ``relay_seen_messages`` row is authoritative: a re-sent
        ``message_id`` is skipped even after the in-process cursor is wiped
        (i.e. after a process restart), and no duplicate communication is
        created. (Personal-Ops Recovery §6 — workstream H.)"""
        app, plane = relay_env
        device_id, token, conn_id = _device(plane, name="Device A")

        async def ingest():
            first = await plane.relay_ingest_batch(
                device_id=device_id,
                messages=[
                    {"message_id": "m-1", "body": "hi", "peer_identifier": "+1", "peer_name": "Sam"}
                ],
            )
            # The durable seen row was written once.
            assert await plane._registry.relay_seen.count_seen(conn_id, device_id) == 1
            # Simulate a restart: wipe the bounded in-process cursor.
            plane._registry.imports._cursors.pop((conn_id, "relay_seq"), None)
            second = await plane.relay_ingest_batch(
                device_id=device_id,
                messages=[
                    {"message_id": "m-1", "body": "hi", "peer_identifier": "+1", "peer_name": "Sam"}
                ],
            )
            return first, second

        first, second = asyncio.run(ingest())
        assert first == {"ok": True, "stored": 1, "skipped": 0}
        assert second == {"ok": True, "stored": 0, "skipped": 1}
        # No duplicate communication; still exactly one seen row.
        assert len(plane._registry.communications._communications) == 1
        assert asyncio.run(plane._registry.relay_seen.count_seen(conn_id, device_id)) == 1

    def test_error_body_does_not_leak_which_check_failed(self, relay_env):
        """Missing header and wrong token must produce the same generic 401."""
        app, plane = relay_env
        device_id, _token, _ = _device(plane, name="Device A")
        with TestClient(app) as client:
            r_missing = client.post(
                "/api/relay/messages",
                json={"device_id": device_id, "messages": [{"message_id": "m-1"}]},
            )
            r_wrong = client.post(
                "/api/relay/messages",
                json={"device_id": device_id, "messages": [{"message_id": "m-1"}]},
                headers={"Authorization": "Bearer wrong-token"},
            )
        assert r_missing.status_code == r_wrong.status_code == 401
        assert r_missing.json()["detail"] == r_wrong.json()["detail"]
        detail = r_wrong.json()["detail"].lower()
        assert "device" not in detail
        assert "mismatch" not in detail


# ──────────────────────────────────────────────────────────────────────────────
# 2. Relay import is token-authenticated (keeps its explicit-consent check)
# ──────────────────────────────────────────────────────────────────────────────


class TestRelayImportAuth:
    def test_missing_authorization_header_rejected(self, relay_env):
        app, plane = relay_env
        device_id, _token, _ = _device(plane, name="Device A")
        with TestClient(app) as client:
            resp = client.post(
                "/api/relay/import-request",
                json={"device_id": device_id, "consent": True},
            )
        assert resp.status_code == 401

    def test_invalid_token_rejected(self, relay_env):
        app, plane = relay_env
        device_id, _token, _ = _device(plane, name="Device A")
        with TestClient(app) as client:
            resp = client.post(
                "/api/relay/import-request",
                json={"device_id": device_id, "consent": True},
                headers={"Authorization": "Bearer nope"},
            )
        assert resp.status_code == 401

    def test_cross_device_token_rejected(self, relay_env):
        app, plane = relay_env
        dev_a, _tok_a, _ = _device(plane, name="Device A")
        dev_b, tok_b, _ = _device(plane, name="Device B")
        with TestClient(app) as client:
            resp = client.post(
                "/api/relay/import-request",
                json={"device_id": dev_a, "consent": True},
                headers={"Authorization": f"Bearer {tok_b}"},
            )
        assert resp.status_code == 401

    def test_valid_token_and_consent_accepted(self, relay_env):
        app, plane = relay_env
        device_id, token, _ = _device(plane, name="Device A")
        with TestClient(app) as client:
            resp = client.post(
                "/api/relay/import-request",
                json={"device_id": device_id, "consent": True},
                headers={"Authorization": f"Bearer {token}"},
            )
        assert resp.status_code == 200
        assert resp.json().get("ok") is True
        assert resp.json().get("consent") is True

    def test_valid_token_but_no_consent_still_422(self, relay_env):
        """Import additionally keeps its explicit-consent check (422)."""
        app, plane = relay_env
        device_id, token, _ = _device(plane, name="Device A")
        with TestClient(app) as client:
            resp = client.post(
                "/api/relay/import-request",
                json={"device_id": device_id, "consent": False},
                headers={"Authorization": f"Bearer {token}"},
            )
        assert resp.status_code == 422
        assert resp.json()["detail"]["error_code"] == "consent_required"

    def test_relay_health_reports_durable_import_backlog(self, relay_env):
        """``import_backlog`` counts pending/running import jobs (durable),
        while the device's in-flight ``pending_backlog`` counter stays 0."""
        app, plane = relay_env
        device_id, token, _ = _device(plane, name="Device A")
        health = asyncio.run(plane.relay_health())
        row = next(h for h in health if h["device_id"] == device_id)
        assert row["import_backlog"] == 0
        with TestClient(app) as client:
            resp = client.post(
                "/api/relay/import-request",
                json={"device_id": device_id, "consent": True},
                headers={"Authorization": f"Bearer {token}"},
            )
        assert resp.status_code == 200
        health = asyncio.run(plane.relay_health())
        row = next(h for h in health if h["device_id"] == device_id)
        assert row["import_backlog"] == 1
        assert row["pending_backlog"] == 0


# ──────────────────────────────────────────────────────────────────────────────
# 3. Access-token dependency: expired tokens rejected, valid accepted
# ──────────────────────────────────────────────────────────────────────────────


class TestAccessTokenValidation:
    async def test_expired_token_rejected(self):
        token = await _mint_expired_access_token()
        with TestClient(_protected_app()) as client:
            resp = client.get("/protected", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 401

    async def test_valid_token_accepted(self):
        token = await _mint_access_token("user-a")
        with TestClient(_protected_app()) as client:
            resp = client.get("/protected", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200
        assert resp.json()["sub"] == "user-a"

    async def test_wrong_signature_rejected(self):
        """A token signed by a different instance is rejected (401)."""
        other = _auth_ctx()
        other.secret = "relay-auth-test-" + "z" * 48
        token, _exp, _claims = other._mint(
            sub="user-a", typ="access", roles=["admin"], ttl=auth_service.ACCESS_TOKEN_TTL
        )
        with TestClient(_protected_app()) as client:
            resp = client.get("/protected", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 401


# ──────────────────────────────────────────────────────────────────────────────
# 4. CORS: wildcard+credentials structurally impossible
# ──────────────────────────────────────────────────────────────────────────────


def _cors_middleware(app: FastAPI) -> Any:
    return next(m for m in app.user_middleware if m.cls.__name__ == "CORSMiddleware")


class TestCors:
    def test_default_no_wildcard_with_credentials(self, monkeypatch):
        monkeypatch.delenv("NC_CORS_ORIGINS", raising=False)
        app = create_app()
        kwargs = _cors_middleware(app).kwargs
        assert not ("*" in kwargs.get("allow_origins", []) and kwargs.get("allow_credentials"))

    def test_default_does_not_use_wildcard_origins(self, monkeypatch):
        monkeypatch.delenv("NC_CORS_ORIGINS", raising=False)
        app = create_app()
        assert "*" not in _cors_middleware(app).kwargs.get("allow_origins", [])

    def test_explicit_origins_env_respected(self, monkeypatch):
        monkeypatch.setenv(
            "NC_CORS_ORIGINS", "https://app.example.com, https://panel.example.com"
        )
        app = create_app()
        kwargs = _cors_middleware(app).kwargs
        assert kwargs.get("allow_origins") == [
            "https://app.example.com",
            "https://panel.example.com",
        ]
        assert kwargs.get("allow_credentials") is True

    def test_preflight_allows_configured_origin_only(self, monkeypatch):
        monkeypatch.setenv("NC_CORS_ORIGINS", "https://app.example.com")
        app = create_app()
        with TestClient(app) as client:
            allowed = client.options(
                "/api/health",
                headers={
                    "Origin": "https://app.example.com",
                    "Access-Control-Request-Method": "GET",
                },
            )
            disallowed = client.options(
                "/api/health",
                headers={
                    "Origin": "https://evil.example.com",
                    "Access-Control-Request-Method": "GET",
                },
            )
        assert allowed.headers.get("Access-Control-Allow-Origin") == "https://app.example.com"
        assert disallowed.headers.get("Access-Control-Allow-Origin") is None

    def test_no_credentials_echoed_when_no_origins(self, monkeypatch):
        monkeypatch.setenv("NC_CORS_ORIGINS", "")
        app = create_app()
        with TestClient(app) as client:
            resp = client.options(
                "/api/health",
                headers={
                    "Origin": "https://app.example.com",
                    "Access-Control-Request-Method": "GET",
                },
            )
        # No origins configured → no ACAO header, and no credential flag.
        assert resp.headers.get("Access-Control-Allow-Origin") is None
        assert resp.headers.get("Access-Control-Allow-Credentials") is None


# ──────────────────────────────────────────────────────────────────────────────
# 5. Connection Center handlers require an access token
# ──────────────────────────────────────────────────────────────────────────────


class TestConnectionsRequireAccessToken:
    WRITE_ENDPOINTS = [
        ("POST", "/api/connections", {"kind": "imap", "label": "x"}),
        ("PATCH", "/api/connections/c1", {"label": "y"}),
        ("DELETE", "/api/connections/c1", None),
        ("POST", "/api/connections/c1/auth", None),
        ("POST", "/api/connections/c1/verify", None),
        ("POST", "/api/connections/c1/sync", None),
        ("POST", "/api/connections/c1/import", None),
        ("POST", "/api/connections/c1/reconnect", None),
        ("PATCH", "/api/connections/c1/permissions", {"config": {}}),
    ]

    def test_read_without_token_rejected(self, connections_env):
        app = connections_env
        assert _no_auth(app, "GET", "/api/connections", None).status_code in (401, 403)
        assert _no_auth(app, "GET", "/api/connections/c1", None).status_code in (401, 403)
        assert (
            _no_auth(app, "GET", "/api/connections/c1/diagnostics", None).status_code
            in (401, 403)
        )

    @pytest.mark.parametrize("method,path,body", WRITE_ENDPOINTS)
    def test_write_without_token_rejected(self, connections_env, method, path, body):
        assert _no_auth(connections_env, method, path, body).status_code in (401, 403)

    async def test_valid_access_token_passes_gate(self, connections_env):
        app = connections_env
        token = await _mint_access_token("user-a")
        with TestClient(app) as client:
            headers = {"Authorization": f"Bearer {token}"}
            # With a token the auth gate passes; the handler then reports the
            # honest degraded (no durable registry) state — NOT a 401.
            resp = client.get("/api/connections", headers=headers)
        assert resp.status_code == 200
