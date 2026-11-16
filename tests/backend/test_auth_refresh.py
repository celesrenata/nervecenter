"""Refresh rotation, expiry, CSRF guard and logout (NS-ADR-123).

Sessions start through the break-glass login, which shares the cookie path
with the SSO exchange.  All token values are fake and never printed.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from backend.api import auth_service
from backend.api.app import create_app
from backend.api.auth_service import AuthContext, AuthSettings

PASSWORD = "unit-test-password-" + "p" * 20
CSRF = {"X-NC-Refresh": "1"}


def _make(**settings: object) -> tuple[TestClient, AuthContext]:
    ctx = AuthContext(
        secret="unit-secret-" + "r" * 60,
        issuer="https://sso.test.example/realms/test",
        client_id="client",
        local_admin_enabled=True,
        local_admin_password=PASSWORD,
        session_factory=None,
        settings=AuthSettings(cookie_secure=False, **settings),
    )
    app = create_app()
    app.state.auth_ctx = ctx
    return TestClient(app), ctx


@pytest.fixture(autouse=True)
def _fresh_store():
    auth_service.reset_memory_store()
    yield
    auth_service.reset_memory_store()


def _login(client: TestClient) -> tuple[dict, str]:
    r = client.post("/api/auth/login", json={"password": PASSWORD})
    assert r.status_code == 200, r.text
    return r.json(), client.cookies.get("nc_rt")


def _refresh(client: TestClient, cookie: str | None, headers: dict | None = CSRF):
    client.cookies.clear()
    extra = dict(headers or {})
    if cookie:
        extra["Cookie"] = f"nc_rt={cookie}"
    return client.post("/api/auth/refresh", headers=extra)


def _rows():
    return list(auth_service._memory_store.refresh_sessions.values())


def test_login_sets_httponly_cookie_and_keeps_refresh_out_of_body():
    client, _ = _make()
    r = client.post("/api/auth/login", json={"password": PASSWORD})
    cookie = r.headers["set-cookie"]
    assert cookie.startswith("nc_rt=")
    assert "HttpOnly" in cookie and "Path=/api/auth" in cookie
    assert "SameSite=strict" in cookie
    assert client.cookies.get("nc_rt") not in r.text


def test_refresh_rotates_cookie_and_returns_working_access_token():
    client, _ = _make()
    _body, first = _login(client)
    r = _refresh(client, first)
    assert r.status_code == 200, r.text
    second = client.cookies.get("nc_rt")
    assert second and second != first
    assert second not in r.text
    token = r.json()["access_token"]
    me = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200 and me.json()["sub"] == "local-admin"
    assert r.json()["expires_in"] > 0


def test_missing_csrf_header_is_401():
    client, _ = _make()
    _body, cookie = _login(client)
    assert _refresh(client, cookie, headers=None).status_code == 401
    assert _refresh(client, cookie, headers={"X-NC-Refresh": "0"}).status_code == 401
    # The refused call did not consume the token.
    assert _refresh(client, cookie).status_code == 200


def test_missing_cookie_is_401():
    client, _ = _make()
    assert _refresh(client, None).status_code == 401


def test_replay_after_grace_revokes_family():
    client, _ = _make(reuse_grace=timedelta(0))
    _body, first = _login(client)
    assert _refresh(client, first).status_code == 200
    newest = client.cookies.get("nc_rt")
    replay = _refresh(client, first)
    assert replay.status_code == 401
    assert "nc_rt=" in replay.headers["set-cookie"]  # cookie cleared
    # The legitimate newest token died with its family.
    assert _refresh(client, newest).status_code == 401
    assert all(row.revoked for row in _rows())


def test_replay_inside_grace_is_accepted_for_concurrent_tabs():
    client, _ = _make(reuse_grace=timedelta(seconds=30))
    _body, first = _login(client)
    assert _refresh(client, first).status_code == 200
    assert _refresh(client, first).status_code == 200


def test_idle_expiry_is_401():
    client, _ = _make()
    _body, cookie = _login(client)
    _rows()[0].idle_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    assert _refresh(client, cookie).status_code == 401


def test_absolute_expiry_is_401_even_with_activity():
    client, _ = _make()
    _body, cookie = _login(client)
    assert _refresh(client, cookie).status_code == 200
    newest = client.cookies.get("nc_rt")
    for row in _rows():
        row.absolute_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    assert _refresh(client, newest).status_code == 401


def test_activity_slides_the_idle_window_up_to_the_cap():
    client, _ = _make(idle_timeout=timedelta(hours=1), absolute_lifetime=timedelta(days=1))
    _body, cookie = _login(client)
    first = _rows()[0]
    first.idle_expires_at = datetime.now(UTC) + timedelta(minutes=1)
    assert _refresh(client, cookie).status_code == 200
    newest = max(_rows(), key=lambda r: r.idle_expires_at)
    assert newest.idle_expires_at > datetime.now(UTC) + timedelta(minutes=50)
    assert newest.idle_expires_at <= newest.absolute_expires_at


def test_logout_revokes_family_and_clears_cookie():
    client, _ = _make()
    body, cookie = _login(client)
    auth = {"Authorization": f"Bearer {body['access_token']}"}
    r = client.post("/api/auth/logout", headers=auth)
    assert r.status_code == 200
    assert "nc_rt=" in r.headers["set-cookie"] and "Max-Age=0" in r.headers["set-cookie"]
    assert client.get("/api/auth/me", headers=auth).status_code == 401
    assert _refresh(client, cookie).status_code == 401


def test_logout_all_revokes_every_session_of_the_caller():
    client, _ = _make()
    body_a, cookie_a = _login(client)
    body_b, cookie_b = _login(client)
    auth = {"Authorization": f"Bearer {body_a['access_token']}"}
    assert client.post("/api/auth/logout-all", headers=auth).status_code == 200
    for body in (body_a, body_b):
        me = client.get(
            "/api/auth/me", headers={"Authorization": f"Bearer {body['access_token']}"}
        )
        assert me.status_code == 401
    assert _refresh(client, cookie_a).status_code == 401
    assert _refresh(client, cookie_b).status_code == 401


def test_logout_all_requires_a_bearer():
    client, _ = _make()
    assert client.post("/api/auth/logout-all").status_code == 401


def test_no_token_value_reaches_the_logs(capsys: pytest.CaptureFixture[str]):
    client, _ = _make()
    body, first = _login(client)
    r = _refresh(client, first)
    newest = client.cookies.get("nc_rt")
    _refresh(client, first)  # replay path logs a warning
    client.post(
        "/api/auth/logout",
        headers={"Authorization": f"Bearer {r.json()['access_token']}"},
    )
    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert "auth.session_started" in output
    for secret in (first, newest, body["access_token"], r.json()["access_token"]):
        assert secret not in output
