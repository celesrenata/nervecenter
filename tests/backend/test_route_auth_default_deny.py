"""Permanent default-deny proof (NS-ADR-122): marker ``route_auth``.

Builds the real app with the real gate (no fake person), enumerates every
operation from ``app.openapi()`` plus the WebSocket, and calls each with no
credentials, an invalid path id and an empty body. Any route outside
``PUBLIC_ROUTES`` that does not answer 401/403 fails the test. A new public
route therefore needs a deliberate edit of ``backend/api/public_routes.py``
and of the pinned list below.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from backend.api import auth, auth_service
from backend.api.app import create_app
from backend.api.public_routes import (
    PERSON_OR_DEVICE_ROUTES,
    PUBLIC_ROUTES,
    SERVICE_ROUTE_SCOPES,
)

pytestmark = pytest.mark.route_auth

METHODS = ("get", "post", "put", "patch", "delete")
FAKE_SERVICE_TOKEN = "mcp-test-service-token-0123456789abcdef0123"

DOCUMENTED_PUBLIC = {
    ("GET", "/api/health"),
    ("GET", "/api/v1/health"),
    ("GET", "/version"),
    ("GET", "/api/auth/sso/config"),
    ("POST", "/api/auth/sso/token"),
    ("POST", "/api/auth/login"),
    ("POST", "/api/auth/refresh"),
    ("POST", "/api/auth/pairing/confirm"),
    ("POST", "/api/relay/messages"),
    ("POST", "/api/relay/contacts"),
    ("POST", "/api/relay/import-request"),
    ("POST", "/api/voicemail/ingest"),
    ("POST", "/api/health/ingest"),
    ("POST", "/api/handoffs/device/claim"),
    ("POST", "/api/handoffs/device/result"),
    ("POST", "/api/mcp/projects"),
    ("WS", "/api/events/ws"),
}


@pytest.fixture(scope="module")
def app():
    return create_app()


@pytest.fixture(scope="module")
def client(app):
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def _operations(app) -> list[tuple[str, str]]:
    schema = app.openapi()
    return [
        (method.upper(), path)
        for path, item in schema["paths"].items()
        for method in item
        if method in METHODS
    ]


def _concrete(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "x", path)


def test_allowlist_is_the_documented_seventeen() -> None:
    assert set(PUBLIC_ROUTES) == DOCUMENTED_PUBLIC
    assert len(PUBLIC_ROUTES) == 17
    assert PERSON_OR_DEVICE_ROUTES == {
        ("GET", "/api/approvals/{approval_id}/verify"),
        ("POST", "/api/approvals/{approval_id}/verify"),
        ("POST", "/api/approvals/{approval_id}/decide"),
    }
    assert not (PERSON_OR_DEVICE_ROUTES & set(PUBLIC_ROUTES))


def test_no_stale_allowlist_entries(app) -> None:
    ops = set(_operations(app)) | {("WS", "/api/events/ws")}
    assert set(PUBLIC_ROUTES) <= ops, sorted(set(PUBLIC_ROUTES) - ops)
    assert PERSON_OR_DEVICE_ROUTES <= ops, sorted(PERSON_OR_DEVICE_ROUTES - ops)


def test_every_operation_outside_allowlist_is_refused(app, client) -> None:
    open_routes: list[str] = []
    checked = 0
    for method, path in _operations(app):
        if (method, path) in PUBLIC_ROUTES:
            continue
        checked += 1
        response = client.request(method, _concrete(path))
        if response.status_code not in (401, 403):
            open_routes.append(f"{method} {path} -> {response.status_code}")
    assert checked > 300
    assert open_routes == []


def test_docs_and_schema_are_login_only(client) -> None:
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404
    assert client.get("/api/openapi.json").status_code == 401


def test_invalid_bearer_is_401_not_500(client) -> None:
    response = client.get("/api/goals", headers={"Authorization": "Bearer not-a-jwt"})
    assert response.status_code == 401


def test_websocket_without_auth_frame_closes_4401(app, monkeypatch) -> None:
    from backend.api.endpoints import events_ws

    monkeypatch.setattr(events_ws, "AUTH_TIMEOUT_S", 0.2)
    with TestClient(app) as c, c.websocket_connect("/api/events/ws") as ws:
        with pytest.raises(WebSocketDisconnect) as info:
            ws.receive_text()
    assert info.value.code == 4401


def test_websocket_with_bad_token_closes_4401(app) -> None:
    with TestClient(app) as c, c.websocket_connect("/api/events/ws") as ws:
        ws.send_text('{"type": "auth", "token": "nope"}')
        with pytest.raises(WebSocketDisconnect) as info:
            ws.receive_text()
    assert info.value.code == 4401


async def _device_token(app) -> str:
    ctx = auth._get_ctx(_Conn(app))
    result = await auth_service.initiate_pairing(ctx, by_sub="owner")
    confirmed = await auth_service.confirm_pairing(
        ctx,
        challenge_id=result["challenge_id"],
        nonce=result["nonce"],
        device_name="test phone",
        platform="ios",
        attestation=None,
        device_id="dev-test",
    )
    return confirmed["device_credential"]


class _Conn:
    def __init__(self, app) -> None:
        self.app = app


def test_device_token_is_403_on_gated_routes_but_ok_on_approvals(app, client) -> None:
    import asyncio

    token = asyncio.run(_device_token(app))
    headers = {"Authorization": f"Bearer {token}"}
    assert client.get("/api/goals", headers=headers).status_code == 403
    assert client.get("/api/auth/me", headers=headers).status_code == 403
    # Approvals verify/decide accept a paired device: the gate lets it through
    # (the route then answers 404 for an unknown approval, never 401/403).
    assert client.get("/api/approvals/x/verify", headers=headers).status_code == 404
    assert client.post("/api/approvals/bind", headers=headers, json={}).status_code == 403


def test_service_token_only_passes_scoped_routes(app, client, monkeypatch) -> None:
    monkeypatch.setenv("NERVE_INTERNAL_SERVICE_TOKENS", f"mcp:{FAKE_SERVICE_TOKEN}")
    headers = {"Authorization": f"Bearer {FAKE_SERVICE_TOKEN}"}
    assert client.get("/api/search", headers=headers).status_code not in (401, 403)
    gated = (
        "/api/connections",
        "/api/infra/connectors",
        "/api/kube/status",
        "/api/settings/omniroute",
        "/api/auth/tokens",
    )
    for path in gated:
        assert client.get(path, headers=headers).status_code == 403, path
    assert client.delete("/api/connections/x", headers=headers).status_code == 403


def test_service_scope_matches_mcp_tool_catalogue() -> None:
    from mcp.tools_module import _SPEC

    tool_paths = {(spec[4], spec[5]) for spec in _SPEC} | {("GET", "/api/context")}
    assert set(SERVICE_ROUTE_SCOPES["mcp"]) == tool_paths


def test_backend_has_no_hard_coded_api_service_url() -> None:
    root = Path(__file__).resolve().parents[2] / "backend"
    offenders = [
        str(path.relative_to(root.parent))
        for path in root.rglob("*.py")
        if re.search(r"(api:8000|127\.0\.0\.1:8000|localhost:8000)", path.read_text("utf-8"))
    ]
    assert offenders == []
