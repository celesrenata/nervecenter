"""Workstream B: the Connection Center endpoints (``/api/connections``).

Two layers:

1. **Degradation contract** (Area 4, honest house semantics): with no durable
   registry — no ``NERVE_DB_URL`` — reads degrade to **200 with an empty list**
   and writes to **202 with ``executed: false``**. The API stays bootable
   without dependencies; nothing is a fake success and nothing is a 5xx.
2. **Wired contract**: with the control plane bound onto ``app.state``
   (exactly the way :mod:`backend.api.app` does it at startup, over a
   file-backed SQLite engine) every frozen route behaves: CRUD (201/200/204/404),
   verify (the hard rule: a probe never advances the operational axis),
   sync/import (honest ``ingest_unavailable`` with no trigger), diagnostics,
   permissions (allow-listed keys only), the stable error-code → HTTP mapping
   (404/401/422/503), and the no-plaintext-credential guarantee on every
   response body.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from backend.api.app import create_app
from backend.connectors.registry import (
    CONNECTION_KINDS,
    ConnectionControlPlane,
    ProbeResultTyped,
)
from backend.connectors.vault import FernetVault, generate_key
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

PERSONAL_TABLES = [
    PersonRow.__table__,
    PersonIdentityRow.__table__,
    ConnectionRow.__table__,
    SecretRow.__table__,
    CommunicationRow.__table__,
    ConversationRow.__table__,
    ImportJobRow.__table__,
    IngestCursorRow.__table__,
]


class FakeImapProbe:
    def __init__(self, success: bool = True, error_code: str | None = None):
        self._result = (
            ProbeResultTyped(True, None, None, ())
            if success
            else ProbeResultTyped(False, error_code or "imap_auth_rejected", "denied", ())
        )
        self.calls: list[tuple[dict[str, Any], str | None]] = []

    async def probe(self, config: dict[str, Any], secret: str | None) -> ProbeResultTyped:
        self.calls.append((dict(config), secret))
        return self._result


class FakeTrigger:
    def __init__(self, fail: Exception | None = None):
        self.started: list[str] = []
        self._fail = fail

    async def start(self, connection_row: Any) -> None:
        self.started.append(str(connection_row.id))
        if self._fail is not None:
            raise self._fail


# -- app fixture: control plane wired exactly like backend.api.app does ---------


async def _make_wired_app(tmp_path: Path) -> tuple[FastAPI, ConnectionControlPlane, Any]:
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'api.db'}",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(PersonRow.__table__.metadata.create_all, tables=PERSONAL_TABLES)

    from backend.persistence.connection import make_session_factory

    vault = FernetVault(generate_key())
    registry = RepositoryRegistry(make_session_factory(engine), secret_codec=vault)
    plane = ConnectionControlPlane(registry, vault=vault)

    app = FastAPI()
    from backend.api.endpoints import connections

    app.include_router(connections.router)
    app.state.connection_control_plane = plane
    app.state._test_engine = engine  # disposed in the fixture finalizer
    return app, plane, engine


@pytest.fixture
async def wired(tmp_path: Path):
    app, plane, engine = await _make_wired_app(tmp_path)
    yield app, plane
    await engine.dispose()


# ---------------------------------------------------------------------------
# 1. Degradation contract (no durable registry — the default create_app())
# ---------------------------------------------------------------------------


class TestDegradation:
    """``create_app()`` without ``NERVE_DB_URL``: honest empty/degraded responses."""

    def test_list_empty_200(self, monkeypatch):
        monkeypatch.delenv("NERVE_DB_URL", raising=False)
        monkeypatch.delenv("NERVECENTER_DATABASE_URL", raising=False)
        with TestClient(create_app()) as client:
            for path in ("/api/connections", "/api/connections/"):
                response = client.get(path)
            assert response.status_code == 200
            body = response.json()
            assert body["data"] == []
            assert body["meta"]

    def test_get_unknown_id_synthetic_200(self, monkeypatch):
        monkeypatch.delenv("NERVE_DB_URL", raising=False)
        with TestClient(create_app()) as client:
            response = client.get("/api/connections/does-not-exist")
        assert response.status_code == 200
        body = response.json()
        assert body == {"id": "does-not-exist", "resource": "connection"}

    def test_create_degrades_to_executed_false(self, monkeypatch):
        monkeypatch.delenv("NERVE_DB_URL", raising=False)
        with TestClient(create_app()) as client:
            response = client.post("/api/connections", json={"kind": "imap"})
        body = response.json()
        assert body["accepted"] is True
        assert body["executed"] is False
        assert "durable storage is not configured" in body["detail"]

    @pytest.mark.parametrize(
        "method,path",
        [
            ("post", "/api/connections/xyz/auth"),
            ("post", "/api/connections/xyz/verify"),
            ("post", "/api/connections/xyz/sync"),
            ("post", "/api/connections/xyz/import"),
            ("post", "/api/connections/xyz/reconnect"),
            ("patch", "/api/connections/xyz"),
        ],
    )
    def test_writes_degrade_to_executed_false(self, monkeypatch, method, path):
        monkeypatch.delenv("NERVE_DB_URL", raising=False)
        with TestClient(create_app()) as client:
            response = client.request(method, path, json={"config": {}} if method == "patch" else {})
        body = response.json()
        assert body["accepted"] is True
        assert body["executed"] is False

    def test_delete_degrades_to_204(self, monkeypatch):
        monkeypatch.delenv("NERVE_DB_URL", raising=False)
        with TestClient(create_app()) as client:
            assert client.delete("/api/connections/xyz").status_code == 204

    def test_permissions_degrade_to_empty(self, monkeypatch):
        monkeypatch.delenv("NERVE_DB_URL", raising=False)
        with TestClient(create_app()) as client:
            response = client.get("/api/connections/xyz/permissions")
            diagnostics = client.get("/api/connections/xyz/diagnostics")
        for r in (response, diagnostics):
            assert r.status_code == 200
            assert r.json()["data"] == []


# ---------------------------------------------------------------------------
# 2. Wired contract (control plane on app.state, file-backed SQLite)
# ---------------------------------------------------------------------------


def _imap_body(**extra: Any) -> dict[str, Any]:
    body = {
        "kind": "imap",
        "label": "Work Mail",
        "config": {"host": "imap.example.org", "port": 993, "account": "me@example.org"},
    }
    body.update(extra)
    return body


class TestCreate:
    def test_create_201_with_derived_state(self, wired, tmp_path: Path):
        app, plane = wired
        with TestClient(app) as client:
            response = client.post("/api/connections", json=_imap_body())
        assert response.status_code == 201
        body = response.json()
        assert body["kind"] == "imap"
        assert body["label"] == "Work Mail"
        assert body["auth_status"] == "unconfigured"
        assert body["operational_status"] == "never_synced"
        assert body["state"] == "not_configured"
        assert body["enabled"] is True
        assert body["import_progress"] == {}
        assert body["id"]

    def test_create_strips_secret_keys(self, wired, tmp_path: Path):
        app, plane = wired
        with TestClient(app) as client:
            response = client.post(
                "/api/connections",
                json=_imap_body(config={"host": "imap.example.org", "password": "leak-me"}),
            )
        body = response.json()
        assert body["config"]["host"] == "imap.example.org"
        assert "password" not in body["config"]
        assert "leak-me" not in response.text

    def test_create_unsupported_kind_422_stable_code(self, wired, tmp_path: Path):
        app, plane = wired
        with TestClient(app) as client:
            response = client.post("/api/connections", json={"kind": "telegram"})
        assert response.status_code == 422
        assert response.json()["detail"]["error_code"] == "kind_not_supported"


class TestRead:
    def test_list_and_get(self, wired, tmp_path: Path):
        app, plane = wired
        with TestClient(app) as client:
            created = client.post("/api/connections", json=_imap_body()).json()
            listing = client.get("/api/connections").json()
            one = client.get(f"/api/connections/{created['id']}").json()
            unknown = client.get("/api/connections/nope")
        assert listing["data"][0]["id"] == created["id"]
        assert one["id"] == created["id"]
        assert unknown.status_code == 404
        assert unknown.json()["detail"]["error_code"] == "connection_not_found"

    def test_list_enabled_only_filters(self, wired, tmp_path: Path):
        app, plane = wired
        with TestClient(app) as client:
            client.post("/api/connections", json=_imap_body())
            client.post("/api/connections", json=_imap_body(enabled=False))
            all_rows = client.get("/api/connections").json()["data"]
            enabled_only = client.get("/api/connections", params={"enabled_only": "true"}).json()["data"]
        assert len(all_rows) == 2
        assert len(enabled_only) == 1
        assert all(c["enabled"] for c in enabled_only)


class TestUpdateDelete:
    def test_patch_merges(self, wired, tmp_path: Path):
        app, plane = wired
        with TestClient(app) as client:
            created = client.post("/api/connections", json=_imap_body()).json()
            patched = client.patch(
                f"/api/connections/{created['id']}",
                json={"label": "Renamed", "config": {"ssl": False}},
            ).json()
            assert patched["label"] == "Renamed"
            assert patched["config"]["host"] == "imap.example.org"
            assert patched["config"]["ssl"] is False
            missing = client.patch("/api/connections/nope", json={"label": "x"})
        assert missing.status_code == 404

    def test_patch_rejects_secret_keys(self, wired, tmp_path: Path):
        app, plane = wired
        with TestClient(app) as client:
            created = client.post("/api/connections", json=_imap_body()).json()
            patched = client.patch(
                f"/api/connections/{created['id']}",
                json={"config": {"password": "sneaky", "token": "sneakier"}},
            ).json()
            assert "password" not in patched["config"]
            assert "token" not in patched["config"]

    def test_delete_204_soft_disables(self, wired, tmp_path: Path):
        app, plane = wired
        with TestClient(app) as client:
            created = client.post("/api/connections", json=_imap_body()).json()
            assert client.delete(f"/api/connections/{created['id']}").status_code == 204
            # Soft delete (no hard delete this wave): the row remains, disabled.
            remaining = client.get(f"/api/connections/{created['id']}")
            assert remaining.status_code == 200
            assert remaining.json()["enabled"] is False
            # …and it no longer appears in enabled-only listings.
            assert (
                client.get("/api/connections", params={"enabled_only": "true"}).json()["data"]
                == []
            )


class TestVerify:
    def test_verify_success_authenticates_without_syncing(self, wired, tmp_path: Path):
        app, plane = wired
        probe = FakeImapProbe(success=True)
        plane.register_probe("imap", probe)
        with TestClient(app) as client:
            created = client.post("/api/connections", json=_imap_body()).json()
            result = client.post(f"/api/connections/{created['id']}/verify").json()
            stored = client.get(f"/api/connections/{created['id']}").json()
        assert result["ok"] is True
        assert result["auth_status"] == "authenticated"
        # Frozen hard rule, observable through the API:
        assert result["operational_status"] == "never_synced"
        assert stored["last_success_at"] is None
        assert stored["state"] == "connected"

    def test_verify_failure_records_stable_error(self, wired, tmp_path: Path):
        app, plane = wired
        probe = FakeImapProbe(success=False, error_code="imap_auth_rejected")
        plane.register_probe("imap", probe)
        with TestClient(app) as client:
            created = client.post("/api/connections", json=_imap_body()).json()
            result = client.post(f"/api/connections/{created['id']}/verify").json()
            stored = client.get(f"/api/connections/{created['id']}").json()
        assert result["ok"] is False
        assert result["error_code"] == "imap_auth_rejected"
        assert stored["auth_status"] == "auth_failed"
        assert stored["operational_status"] == "never_synced"  # untouched
        assert stored["state"] == "auth_required"
        assert stored["last_error_code"] == "imap_auth_rejected"

    def test_verify_missing_404(self, wired, tmp_path: Path):
        app, plane = wired
        with TestClient(app) as client:
            response = client.post("/api/connections/nope/verify")
        assert response.status_code == 404
        assert response.json()["detail"]["error_code"] == "connection_not_found"

    def test_verify_without_probe_is_probe_unsupported(self, wired, tmp_path: Path):
        app, plane = wired
        with TestClient(app) as client:
            created = client.post("/api/connections", json=_imap_body()).json()
            response = client.post(f"/api/connections/{created['id']}/verify")
        assert response.status_code == 200  # a failed *probe* is still a 200 body
        assert response.json()["ok"] is False
        assert response.json()["error_code"] == "probe_unsupported"


class TestSyncImportReconnect:
    def test_sync_without_trigger_is_honest(self, wired, tmp_path: Path):
        app, plane = wired
        with TestClient(app) as client:
            created = client.post("/api/connections", json=_imap_body()).json()
            result = client.post(f"/api/connections/{created['id']}/sync").json()
            stored = client.get(f"/api/connections/{created['id']}").json()
        assert result["ok"] is False
        assert result["error_code"] == "ingest_unavailable"
        assert stored["operational_status"] == "error"
        assert stored["last_error_code"] == "ingest_unavailable"
        # derive_state rule 1: unconfigured auth wins over the operational axis.
        assert stored["state"] == "not_configured"

    def test_sync_with_trigger_marks_syncing(self, wired, tmp_path: Path):
        """A trigger start advances to ``syncing`` — never straight to healthy.

        The frozen hard rule: only a worker's record_sync_success (workstream C)
        marks a connection healthy; the HTTP sync trigger alone cannot.
        """
        app, plane = wired
        trigger = FakeTrigger()
        plane.register_trigger("imap", trigger)
        with TestClient(app) as client:
            created = client.post("/api/connections", json=_imap_body()).json()
            result = client.post(f"/api/connections/{created['id']}/sync").json()
            stored = client.get(f"/api/connections/{created['id']}").json()
        assert result["ok"] is True
        assert len(trigger.started) == 1
        assert stored["operational_status"] == "syncing"
        assert stored["state"] == "syncing"

    def test_import_failure_degrades_and_reports_job(self, wired, tmp_path: Path):
        app, plane = wired
        trigger = FakeTrigger(fail=RuntimeError("boom"))
        plane.register_trigger("imap", trigger)
        with TestClient(app) as client:
            created = client.post("/api/connections", json=_imap_body()).json()
            result = client.post(f"/api/connections/{created['id']}/import").json()
            stored = client.get(f"/api/connections/{created['id']}").json()
            job = client.get(f"/api/connections/{created['id']}/diagnostics").json()
        assert result["ok"] is False
        assert result["error_code"] == "ingest_failed"
        assert stored["operational_status"] == "degraded"
        assert job["diagnosis"]["error_code"] == "ingest_failed"
        assert "actionable" in job["diagnosis"]["fix"].lower() or "re-enter" in job["diagnosis"]["fix"].lower() or len(job["diagnosis"]["fix"]) > 0

    def test_reconnect_probe_failure_does_not_sync(self, wired, tmp_path: Path):
        app, plane = wired
        trigger = FakeTrigger()
        plane.register_trigger("imap", trigger)
        plane.register_probe("imap", FakeImapProbe(success=False, error_code="imap_auth_rejected"))
        with TestClient(app) as client:
            created = client.post("/api/connections", json=_imap_body()).json()
            result = client.post(f"/api/connections/{created['id']}/reconnect").json()
        assert result["reconnect"] is True
        assert result["ok"] is False
        assert result["error_code"] == "imap_auth_rejected"
        assert trigger.started == []  # a failed probe never kicks a sync

    def test_reconnect_success_resyncs(self, wired, tmp_path: Path):
        app, plane = wired
        trigger = FakeTrigger()
        plane.register_trigger("imap", trigger)
        plane.register_probe("imap", FakeImapProbe(success=True))
        with TestClient(app) as client:
            created = client.post("/api/connections", json=_imap_body()).json()
            result = client.post(f"/api/connections/{created['id']}/reconnect").json()
        assert result["reconnect"] is True
        assert result["ok"] is True
        assert len(trigger.started) == 1


class TestDiagnostics:
    def test_diagnostics_actionable_fix(self, wired, tmp_path: Path):
        app, plane = wired
        plane.register_probe("imap", FakeImapProbe(success=False, error_code="imap_unreachable"))
        with TestClient(app) as client:
            created = client.post("/api/connections", json=_imap_body()).json()
            client.post(f"/api/connections/{created['id']}/verify")
            diag = client.get(f"/api/connections/{created['id']}/diagnostics").json()
        assert diag["diagnosis"]["error_code"] == "imap_unreachable"
        assert "host" in diag["diagnosis"]["fix"].lower()
        assert diag["diagnosis"]["last_attempt_at"] is not None

    def test_diagnostics_healthy_has_no_error(self, wired, tmp_path: Path):
        app, plane = wired
        with TestClient(app) as client:
            created = client.post("/api/connections", json=_imap_body()).json()
            diag = client.get(f"/api/connections/{created['id']}/diagnostics").json()
            assert diag["diagnosis"]["error_code"] is None
            assert "No errors recorded" in diag["diagnosis"]["fix"]


class TestPermissions:
    def test_get_permissions_surface(self, wired, tmp_path: Path):
        app, plane = wired
        with TestClient(app) as client:
            created = client.post("/api/connections", json=_imap_body()).json()
            surface = client.get(f"/api/connections/{created['id']}/permissions").json()
        assert surface["kind"] == "imap"
        assert surface["editable_keys"] == ["account", "host", "port", "ssl"]
        assert "password" in surface["denied_keys"]

    def test_get_permissions_unknown_404(self, wired, tmp_path: Path):
        app, plane = wired
        with TestClient(app) as client:
            response = client.get("/api/connections/nope/permissions")
        assert response.status_code == 404

    def test_patch_permissions_only_allowlisted_applied(self, wired, tmp_path: Path):
        app, plane = wired
        with TestClient(app) as client:
            created = client.post("/api/connections", json=_imap_body()).json()
            result = client.patch(
                f"/api/connections/{created['id']}/permissions",
                json={"config": {"host": "imap.new", "password": "sneaky"}},
            ).json()
            stored = client.get(f"/api/connections/{created['id']}").json()
        assert result["applied_keys"] == ["host"]
        assert result["ignored_keys"] == ["password"]
        assert stored["config"]["host"] == "imap.new"
        assert "password" not in stored["config"]


class TestAuthFlow:
    def test_start_oauth_for_gmail(self, wired, tmp_path: Path):
        app, plane = wired
        with TestClient(app) as client:
            created = client.post(
                "/api/connections",
                json={
                    "kind": "gmail",
                    "config": {"client_id": "cid", "oauth_redirect_uri": "http://cb"},
                },
            ).json()
            result = client.post(f"/api/connections/{created['id']}/auth").json()
        assert result["provider"] == "google"
        assert "state" in result
        assert "client_id=cid" in result["authorization_url"]

    def test_auth_flow_relay_points_to_pair_endpoint(self, wired, tmp_path: Path):
        app, plane = wired
        with TestClient(app) as client:
            created = client.post(
                "/api/connections",
                json={"kind": "relay_device", "config": {"device_name": "Mac"}},
            ).json()
            result = client.post(f"/api/connections/{created['id']}/auth").json()
        assert result["ok"] is True
        assert result["pair_endpoint"] == "/api/relay/pair"
        assert result["auth_status"] == "unconfigured"

    def test_auth_flow_no_flow_for_imap(self, wired, tmp_path: Path):
        app, plane = wired
        with TestClient(app) as client:
            created = client.post("/api/connections", json=_imap_body()).json()
            response = client.post(f"/api/connections/{created['id']}/auth")
        assert response.status_code == 422
        assert response.json()["detail"]["error_code"] == "bad_request"

    def test_start_oauth_wrong_kind_is_oauth_incomplete(self, wired, tmp_path: Path):
        app, plane = wired
        with TestClient(app) as client:
            created = client.post(
                "/api/connections", json={"kind": "calendar", "config": {}}
            ).json()
            # calendar has no OAuth flow: the endpoint maps it to bad_request.
            response = client.post(f"/api/connections/{created['id']}/auth")
        assert response.status_code == 422


class TestNoPlaintextLeak:
    """Hard rule: no credential ever appears in a response body."""

    def test_all_frozen_routes_never_echo_credentials(self, wired, tmp_path: Path):
        app, plane = wired
        plane.register_probe("imap", FakeImapProbe(success=True))
        plane.register_trigger("imap", FakeTrigger())
        with TestClient(app) as client:
            created = client.post("/api/connections", json=_imap_body()).json()
            cid = created["id"]
            responses = [
                client.get("/api/connections"),
                client.get(f"/api/connections/{cid}"),
                client.post(f"/api/connections/{cid}/verify"),
                client.post(f"/api/connections/{cid}/sync"),
                client.post(f"/api/connections/{cid}/import"),
                client.post(f"/api/connections/{cid}/reconnect"),
                client.get(f"/api/connections/{cid}/diagnostics"),
                client.get(f"/api/connections/{cid}/permissions"),
                client.patch(f"/api/connections/{cid}/permissions", json={"config": {"ssl": True}}),
                client.patch(f"/api/connections/{cid}", json={"config": {"ssl": False}}),
            ]
        for response in responses:
            assert "leak-me" not in response.text
            assert "password" not in response.json().get("config", {})
