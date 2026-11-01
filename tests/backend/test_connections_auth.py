"""Connection Center auth dependency (``current_access``) — focused test.

The Connection Center backend routes (``/api/connections``) are guarded by
``Depends(current_access)`` (``backend/api/endpoints/connections.py``). This
test locks in the two ends of that contract with a *real* signed JWT and the
in-process session store (no live database, no SSO provider):

1. **401 without a token.** A request with no ``Authorization`` header is
   rejected before any control-plane work — the detail is the stable
   ``"missing bearer token"`` string, and the response is ``401`` (not 5xx,
   not a silent empty list).
2. **200 with a valid session token.** A freshly minted, session-backed
   access token authorizes the list route. With no durable registry bound,
   the read degrades to the honest **200 + empty list** (Area 4), proving the
   endpoint actually loads once the bearer token is present.
3. **No plaintext leakage.** An authenticated response never echoes the
   presented token back.

This mirrors the token-minting idiom in ``tests/backend/test_relay_auth.py``
(``_mint`` + ``record_session`` against ``session_factory=None``).
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api.auth_service import (
    ACCESS_TOKEN_TTL,
    TYP_ACCESS,
    AuthContext,
    record_session,
    reset_memory_store,
)
from backend.api.endpoints import connections

# RFC 7518-recommended minimum HS256 key length; fixed so the test is
# reproducible and independent of any ambient NERVE_JWT_SECRET.
JWT_SECRET = "connections-auth-test-" + "k" * 48
JWT_ISSUER = "https://sso.test.example/application/o/nervecenter/"
SUB = "connections-test-principal"


@pytest.fixture(autouse=True)
def _clean_auth(monkeypatch: pytest.MonkeyPatch):
    """Isolate the in-process session store and strip ambient auth env."""
    for name in (
        "NERVE_JWT_SECRET",
        "NERVE_SSO_ISSUER",
        "NERVE_SSO_CLIENT_ID",
        "NERVE_LOCAL_ADMIN",
        "NERVE_LOCAL_ADMIN_PASSWORD",
    ):
        monkeypatch.delenv(name, raising=False)
    reset_memory_store()
    yield
    reset_memory_store()


def _auth_ctx() -> AuthContext:
    """A real AuthContext with the in-process session store (no DB)."""
    return AuthContext(
        secret=JWT_SECRET,
        issuer=JWT_ISSUER,
        client_id="connections-test-client",
        local_admin_enabled=False,
        local_admin_password=None,
        session_factory=None,
    )


def _app() -> FastAPI:
    """A minimal app exposing the real Connection Center router.

    The control plane is intentionally absent so that, once authenticated,
    the list route returns the honest degraded empty list (Area 4) — this
    keeps the test focused on the auth dependency, not the durable registry.
    """
    app = FastAPI()
    app.include_router(connections.router)
    app.state.auth_ctx = _auth_ctx()
    return app


async def _mint_access_token(sub: str = SUB) -> str:
    """Mint a valid, session-backed access token for the in-process store."""
    ctx = _auth_ctx()
    token, exp, claims = ctx._mint(sub=sub, typ=TYP_ACCESS, roles=["admin"], ttl=ACCESS_TOKEN_TTL)
    await record_session(
        ctx, jti=claims["jti"], sub=sub, typ=TYP_ACCESS, expires_at=exp, source="test"
    )
    return token


def test_missing_bearer_token_is_401():
    """No Authorization header → 401 'missing bearer token', never 5xx or empty."""
    app = _app()
    with TestClient(app) as client:
        resp = client.get("/api/connections")
    assert resp.status_code == 401
    assert resp.json() == {"detail": "missing bearer token"}


async def test_valid_access_token_loads_list_as_200():
    """A valid session token authorizes the route; degraded read → empty list."""
    token = await _mint_access_token()
    app = _app()
    with TestClient(app) as client:
        resp = client.get("/api/connections", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    body = resp.json()
    # Honest Area-4 degradation: an authenticated read with no durable
    # registry is 200 with an empty data list (never fabricated connections).
    assert body["data"] == []
    assert "meta" in body


async def test_valid_token_does_not_leak_plaintext():
    """An authenticated response carries no credential-like material."""
    token = await _mint_access_token()
    app = _app()
    with TestClient(app) as client:
        resp = client.get("/api/connections", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert resp.text.count(token) == 0
