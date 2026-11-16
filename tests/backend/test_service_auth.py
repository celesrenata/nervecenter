"""Internal service credential (NS-ADR-122): hashed, constant-time, scoped."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.api import service_auth
from backend.api.app import create_app
from backend.api.public_routes import SERVICE_ROUTE_SCOPES, service_may_call

pytestmark = pytest.mark.route_auth

TOKEN = "mcp-test-service-token-0123456789abcdef0123"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv(service_auth.TOKENS_ENV, f"mcp:{TOKEN}")
    with TestClient(create_app(), raise_server_exceptions=False) as c:
        yield c


def test_identify_service_hashed_and_constant_time(monkeypatch) -> None:
    monkeypatch.setenv(service_auth.TOKENS_ENV, f"mcp:{TOKEN},other:{'x' * 40}")
    assert service_auth.identify_service(TOKEN) == "mcp"
    assert service_auth.identify_service(TOKEN + "x") is None
    assert service_auth.identify_service("") is None
    # "other" is not a known service, so its token is ignored.
    assert service_auth.identify_service("x" * 40) is None


def test_short_or_unexpanded_tokens_are_ignored(monkeypatch) -> None:
    monkeypatch.setenv(service_auth.TOKENS_ENV, "mcp:short")
    assert service_auth.identify_service("short") is None
    monkeypatch.setenv(service_auth.TOKENS_ENV, "mcp:$(NERVE_INTERNAL_MCP_TOKEN)-padding-padding")
    assert service_auth.identify_service("$(NERVE_INTERNAL_MCP_TOKEN)-padding-padding") is None


def test_no_configured_tokens_means_no_service_access(monkeypatch) -> None:
    monkeypatch.delenv(service_auth.TOKENS_ENV, raising=False)
    assert service_auth.identify_service(TOKEN) is None


def test_scope_matching_is_by_concrete_path() -> None:
    assert service_may_call("mcp", "GET", "/api/goals/g-1")
    assert service_may_call("mcp", "POST", "/api/attention/a1/snooze")
    assert not service_may_call("mcp", "DELETE", "/api/goals/g-1")
    assert not service_may_call("mcp", "GET", "/api/goals/g-1/extra")
    assert not service_may_call("mcp", "GET", "/api/connections")
    assert not service_may_call("nobody", "GET", "/api/search")


def test_every_scoped_route_accepts_the_service_token(client) -> None:
    headers = {"Authorization": f"Bearer {TOKEN}"}
    refused = []
    for method, template in sorted(SERVICE_ROUTE_SCOPES["mcp"]):
        body = {} if method == "POST" else None
        response = client.request(method, template.replace("{id}", "x"), headers=headers, json=body)
        if response.status_code in (401, 403):
            refused.append(f"{method} {template} -> {response.status_code}")
    assert refused == []


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/connections"),
        ("GET", "/api/infra/connectors"),
        ("GET", "/api/settings/omniroute"),
        ("GET", "/api/auth/tokens"),
        ("GET", "/api/auth/me"),
        ("DELETE", "/api/goals/x"),
        ("GET", "/api/openapi.json"),
    ],
)
def test_service_token_is_403_outside_scope(client, method, path) -> None:
    response = client.request(method, path, headers={"Authorization": f"Bearer {TOKEN}"})
    assert response.status_code == 403


def test_wrong_service_token_is_401(client) -> None:
    response = client.get("/api/search", headers={"Authorization": f"Bearer {TOKEN}0"})
    assert response.status_code == 401


def test_service_principal_is_not_a_person() -> None:
    from backend.api.endpoints.mail_security import _eligible_for_server_scopes

    assert _eligible_for_server_scopes(service_auth.service_claims("mcp")) is False
