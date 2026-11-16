"""Personal ``nct_`` API tokens (NS-ADR-124).  All values are fake."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from backend.api import auth_service, auth_sessions
from backend.api.app import create_app
from backend.api.auth_service import AuthContext, AuthSettings
from backend.api.endpoints.mail_security import _eligible_for_server_scopes

PASSWORD = "unit-test-password-" + "p" * 20


def _make() -> tuple[TestClient, AuthContext, dict[str, str]]:
    ctx = AuthContext(
        secret="unit-secret-" + "t" * 60,
        issuer="https://sso.test.example/realms/test",
        client_id="client",
        local_admin_enabled=True,
        local_admin_password=PASSWORD,
        session_factory=None,
        settings=AuthSettings(cookie_secure=False),
    )
    app = create_app()
    app.state.auth_ctx = ctx
    client = TestClient(app)
    body = client.post("/api/auth/login", json={"password": PASSWORD}).json()
    return client, ctx, {"Authorization": f"Bearer {body['access_token']}"}


@pytest.fixture(autouse=True)
def _fresh_store():
    auth_service.reset_memory_store()
    yield
    auth_service.reset_memory_store()


def _create(client: TestClient, auth: dict, **body: object) -> dict:
    r = client.post("/api/auth/tokens", json={"name": "ci", **body}, headers=auth)
    assert r.status_code == 201, r.text
    return r.json()


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_token_is_shown_once_and_stored_hashed():
    client, _ctx, auth = _make()
    created = _create(client, auth)
    token = created["token"]
    assert token.startswith("nct_") and len(token) == 4 + 43
    row = next(iter(auth_service._memory_store.personal_tokens.values()))
    assert row.token_hash == auth_sessions.hash_token(token)
    assert token not in {str(v) for v in vars(row).values()}
    listing = client.get("/api/auth/tokens", headers=auth)
    assert listing.status_code == 200
    assert token not in listing.text and "token" not in listing.json()["tokens"][0]
    assert listing.json()["tokens"][0]["prefix"] == token[:8]


def test_pat_authenticates_as_the_owner_and_counts_as_a_person():
    client, _ctx, auth = _make()
    token = _create(client, auth)["token"]
    me = client.get("/api/auth/me", headers=_bearer(token))
    assert me.status_code == 200 and me.json()["sub"] == "local-admin"
    claims = {
        "sub": "local-admin", "typ": "access", "roles": ["user"],
        auth_service.SSO_SOURCE_CLAIM: "pat",
    }
    assert _eligible_for_server_scopes(claims) is True


def test_pat_cannot_manage_tokens():
    client, _ctx, auth = _make()
    created = _create(client, auth)
    pat = _bearer(created["token"])
    assert client.get("/api/auth/tokens", headers=pat).status_code == 403
    assert client.post("/api/auth/tokens", json={"name": "x"}, headers=pat).status_code == 403
    assert client.delete(f"/api/auth/tokens/{created['id']}", headers=pat).status_code == 403
    assert client.post("/api/auth/logout-all", headers=pat).status_code == 403


def test_revoke_stops_the_token():
    client, _ctx, auth = _make()
    created = _create(client, auth)
    pat = _bearer(created["token"])
    assert client.get("/api/auth/me", headers=pat).status_code == 200
    assert client.delete(f"/api/auth/tokens/{created['id']}", headers=auth).status_code == 200
    assert client.get("/api/auth/me", headers=pat).status_code == 401
    assert client.delete("/api/auth/tokens/nope", headers=auth).status_code == 404


def test_expiry_is_enforced_and_default_is_90_days():
    client, _ctx, auth = _make()
    created = _create(client, auth)
    expires = datetime.fromisoformat(created["expires_at"])
    assert timedelta(days=89) < expires - datetime.now(UTC) <= timedelta(days=90)
    row = next(iter(auth_service._memory_store.personal_tokens.values()))
    row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    assert client.get("/api/auth/me", headers=_bearer(created["token"])).status_code == 401


def test_never_expiring_token_and_validation():
    client, _ctx, auth = _make()
    assert _create(client, auth, expires_in_days=None)["expires_at"] is None
    bad = client.post("/api/auth/tokens", json={"name": ""}, headers=auth)
    assert bad.status_code == 422


def test_unknown_nct_token_is_401_and_other_owner_cannot_revoke():
    client, ctx, auth = _make()
    assert client.get("/api/auth/me", headers=_bearer("nct_" + "x" * 43)).status_code == 401
    created = _create(client, auth)
    # A different person cannot see or revoke it.
    other_token, exp = ctx.issue_access_token("someone-else", ["user"])
    import asyncio
    asyncio.run(auth_service.record_session(
        ctx, jti=auth_service._jti_of(other_token, ctx), sub="someone-else",
        typ="access", expires_at=exp, source="unit",
    ))
    other = _bearer(other_token)
    assert client.get("/api/auth/tokens", headers=other).json()["tokens"] == []
    assert client.delete(f"/api/auth/tokens/{created['id']}", headers=other).status_code == 404


def test_device_credential_is_refused():
    client, ctx, _auth = _make()
    device, exp = ctx.issue_device_credential("dev-1")
    import asyncio
    asyncio.run(auth_service.record_session(
        ctx, jti=auth_service._jti_of(device, ctx), sub="device:dev-1",
        typ="device", expires_at=exp, source="pairing", device_id="dev-1",
    ))
    assert client.get("/api/auth/tokens", headers=_bearer(device)).status_code == 401
    claims = {"sub": "device:dev-1", "typ": "device", "roles": ["device"]}
    assert _eligible_for_server_scopes(claims) is False


def test_last_used_is_throttled():
    client, _ctx, auth = _make()
    token = _create(client, auth)["token"]
    assert client.get("/api/auth/me", headers=_bearer(token)).status_code == 200
    row = next(iter(auth_service._memory_store.personal_tokens.values()))
    first_use = row.last_used_at
    assert first_use is not None
    assert client.get("/api/auth/me", headers=_bearer(token)).status_code == 200
    assert row.last_used_at == first_use  # inside the 5 minute window
    row.last_used_at = datetime.now(UTC) - timedelta(minutes=6)
    assert client.get("/api/auth/me", headers=_bearer(token)).status_code == 200
    assert row.last_used_at > first_use


def test_token_value_never_logged_but_use_is_audited(capsys: pytest.CaptureFixture[str]):
    client, _ctx, auth = _make()
    token = _create(client, auth)["token"]
    client.get("/api/auth/me", headers=_bearer(token))
    client.get("/api/auth/me", headers=_bearer(token[:-3] + "bad"))
    out = capsys.readouterr()
    output = out.out + out.err
    assert "auth.pat_used" in output and "auth.pat_created" in output
    assert token not in output and token[8:] not in output
