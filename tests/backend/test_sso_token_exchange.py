"""Server-side SSO token exchange + Keycloak RS256 acceptance tests.

Complements ``test_auth_sso.py`` (S101).  Covers the confidential-client path
introduced for Keycloak:

- ``POST /api/auth/sso/token`` performs the authorization-code → token exchange
  *server-side* (the ``nervecenter`` client is confidential, so the browser can
  never do it).  The issuer's token endpoint is exercised with a client-secret
  Basic-auth header; the issuer's raw error body is never echoed to callers.
- ``AuthContext`` resolves the issuer's ``jwks_uri`` / ``token_endpoint`` for a
  Keycloak realm (``…/protocol/openid-connect/certs`` / ``…/token``) while
  preserving the Authentik defaults.
- ``verify_token`` accepts an issuer-minted (RS256) access token shaped like a
  Keycloak access token: no ``typ`` claim, no ``iat`` required, ``iss`` is the
  realm base, and ``aud`` is the configured client id.

No live SSO provider or network is used: the JWKS fetch and the token-endpoint
HTTP call are replaced by in-process fakes.
"""

from __future__ import annotations

import time

import jwt
import pytest
from fastapi.testclient import TestClient

import backend.api.auth_service as auth_service
from backend.api.app import create_app
from backend.api.auth_service import (
    TYP_ACCESS,
    AuthContext,
    TokenError,
)

# A Keycloak realm issuer (realm base, no trailing slash) — the live shape.
KEYCLOAK_ISSUER = "https://sso.celestium.life/realms/celestium"
KEYCLOAK_JWKS = f"{KEYCLOAK_ISSUER}/protocol/openid-connect/certs"
KEYCLOAK_TOKEN = f"{KEYCLOAK_ISSUER}/protocol/openid-connect/token"
KID = "test-kid-0001"
SUB = "11111111-2222-3333-4444-555555555555"


def _ctx(**kwargs) -> AuthContext:
    """Build an in-process :class:`AuthContext` pointed at a Keycloak realm."""
    return AuthContext(
        secret="unit-secret-" + "a" * 60,
        issuer=kwargs.pop("issuer", KEYCLOAK_ISSUER),
        client_id=kwargs.pop("client_id", "nervecenter"),
        local_admin_enabled=False,
        local_admin_password=None,
        session_factory=None,
        client_secret=kwargs.pop("client_secret", "unit-client-secret"),
        **kwargs,
    )


def _rsa_private_key():
    from cryptography.hazmat.primitives.asymmetric import rsa

    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _keycloak_access_token(
    private_key,
    *,
    aud: str = "nervecenter",
    iss: str = KEYCLOAK_ISSUER,
    sub: str = SUB,
    include_iat: bool = False,
    delta: int = 300,
) -> str:
    """Mint an RS256 token shaped like a Keycloak access token.

    Deliberately omits the ``typ`` claim (Keycloak access tokens carry none) and,
    by default, the ``iat`` claim, to exercise the relaxed ``require`` set.
    """
    now = int(time.time())
    claims: dict = {
        "iss": iss,
        "aud": aud,
        "sub": sub,
        "exp": now + delta,
        "jti": "kc-jti-1",
    }
    if include_iat:
        claims["iat"] = now
    return jwt.encode(claims, private_key, algorithm="RS256", headers={"kid": KID})


@pytest.fixture
def jwks(monkeypatch):
    """Serve a fixed RSA public key for the realm's JWKS URL (no network)."""
    private_key = _rsa_private_key()
    public_key = private_key.public_key()

    async def fake_fetch(uri: str) -> dict:
        return {KID: public_key}

    monkeypatch.setattr(auth_service, "fetch_jwks", fake_fetch)
    return private_key


# ─── RS256 verification: Keycloak access-token shape ──────────────────────────


async def test_keycloak_rs256_token_accepted(jwks):
    """A no-``typ``/no-``iat`` realm-issued token is accepted for /me."""
    ctx = _ctx()
    token = _keycloak_access_token(jwks, include_iat=False)
    claims = await ctx.verify_token(token, expected_typ=TYP_ACCESS)
    assert claims["sub"] == SUB
    assert claims["iss"] == KEYCLOAK_ISSUER


async def test_keycloak_rs256_token_with_iat_accepted(jwks):
    """A token that *does* carry ``iat`` is also accepted (no regression)."""
    ctx = _ctx()
    token = _keycloak_access_token(jwks, include_iat=True)
    claims = await ctx.verify_token(token)
    assert claims["sub"] == SUB


async def test_keycloak_rs256_wrong_audience_rejected(jwks):
    """A token addressed to another client id is rejected."""
    ctx = _ctx()
    token = _keycloak_access_token(jwks, aud="some-other-client")
    with pytest.raises(TokenError, match="Audience"):
        await ctx.verify_token(token)


async def test_keycloak_rs256_wrong_issuer_rejected(jwks):
    """A token from a foreign issuer is rejected."""
    ctx = _ctx()
    token = _keycloak_access_token(
        jwks, iss="https://evil.example/realms/other"
    )
    with pytest.raises(TokenError, match="issuer"):
        await ctx.verify_token(token)


async def test_keycloak_rs256_expired_rejected(jwks):
    """An expired realm-issued token is rejected."""
    ctx = _ctx()
    token = _keycloak_access_token(jwks, delta=-60)
    with pytest.raises(TokenError, match="expired"):
        await ctx.verify_token(token)


async def test_keycloak_rs256_unsigned_client_id_skips_aud(jwks):
    """When no client id is configured, the audience check is skipped."""
    ctx = _ctx(client_id="")
    token = _keycloak_access_token(jwks, aud="whatever")
    claims = await ctx.verify_token(token)
    assert claims["sub"] == SUB


# ─── Endpoint / config resolution ─────────────────────────────────────────────


def test_keycloak_endpoint_resolution():
    """A /realms/ issuer resolves to Keycloak's protocol paths."""
    ctx = _ctx()
    assert ctx.jwks_uri == KEYCLOAK_JWKS
    assert ctx.token_endpoint == KEYCLOAK_TOKEN


def test_authentik_endpoint_resolution_unchanged():
    """A non-realm (Authentik) issuer keeps its ``<base>/jwks/`` and ``<base>/token/`` paths."""
    base = "https://sso.test.example/application/o/nervecenter"
    ctx = _ctx(issuer=f"{base}/")
    assert ctx.jwks_uri == f"{base}/jwks/"
    assert ctx.token_endpoint == f"{base}/token/"


def test_jwks_uri_env_override_wins():
    ctx = _ctx(jwks_uri_override="https://mirror.example/certs")
    assert ctx.jwks_uri == "https://mirror.example/certs"


def test_sso_config_enabled_with_keycloak_discovery():
    ctx = _ctx()
    discovery = {
        "issuer": KEYCLOAK_ISSUER,
        "authorization_endpoint": f"{KEYCLOAK_ISSUER}/protocol/openid-connect/auth",
        "token_endpoint": KEYCLOAK_TOKEN,
        "jwks_uri": KEYCLOAK_JWKS,
        "end_session_endpoint": f"{KEYCLOAK_ISSUER}/protocol/openid-connect/logout",
    }
    cfg = auth_service.sso_config(ctx, discovery)
    assert cfg["enabled"] is True
    assert cfg["token_endpoint"] == KEYCLOAK_TOKEN
    assert cfg["jwks_uri"] == KEYCLOAK_JWKS
    assert cfg["authorization_endpoint"].endswith("/protocol/openid-connect/auth")


def test_sso_config_includes_derived_issuer_display_name():
    # The display name is derived from the issuer (last path segment / realm
    # name) — no invented names, and absent/empty issuer degrades to "".
    cfg = auth_service.sso_config(_ctx(), {"issuer": KEYCLOAK_ISSUER})
    assert cfg["issuer_display_name"] == "celestium"

    cfg_plain = auth_service.sso_config(_ctx(issuer="https://plain.example.com"))
    assert cfg_plain["issuer_display_name"] == "plain.example.com"

    cfg_bare = auth_service.sso_config(_ctx(issuer=""))
    assert cfg_bare["issuer_display_name"] == ""


def test_sso_config_disabled_without_client_id():
    ctx = _ctx(client_id="")
    cfg = auth_service.sso_config(ctx, {"issuer": KEYCLOAK_ISSUER})
    assert cfg["enabled"] is False


# ─── POST /api/auth/sso/token (server-side exchange) ───────────────────────────


class _FakeResponse:
    def __init__(self, status_code: int, body: dict) -> None:
        self.status_code = status_code
        self._body = body

    def json(self) -> dict:
        return self._body


class _FakeHttpClient:
    """Stands in for ``httpx.AsyncClient``; records the last ``post`` call."""

    last: tuple | None = None

    def __init__(self, response: _FakeResponse) -> None:
        self._response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url: str, **kwargs):
        _FakeHttpClient.last = (url, kwargs)
        return self._response


def _patch_http(monkeypatch, status: int, body: dict) -> None:
    def fake_client(*args, **kwargs):
        return _FakeHttpClient(_FakeResponse(status, body))

    monkeypatch.setattr(auth_service.httpx, "AsyncClient", fake_client)


def _sso_client(ctx: AuthContext) -> TestClient:
    app = create_app()
    app.state.auth_ctx = ctx  # _get_ctx() returns this without touching env
    return TestClient(app)


def test_sso_token_exchange_success(monkeypatch):
    """A good code is exchanged server-side and the tokens are returned."""
    ctx = _ctx()
    token_body = {
        "access_token": "AT",
        "token_type": "Bearer",
        "refresh_token": "RT",
        "expires_in": 300,
    }
    _patch_http(monkeypatch, 200, token_body)
    client = _sso_client(ctx)

    r = client.post(
        "/api/auth/sso/token",
        json={
            "code": "code-xyz",
            "code_verifier": "verifier-xyz",
            "redirect_uri": "https://host/web/auth/callback",
        },
    )
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["access_token"] == "AT"
    assert out["token_type"] == "Bearer"
    assert out["refresh_token"] == "RT"
    assert out["expires_in"] == 300

    url, kwargs = _FakeHttpClient.last
    assert url == KEYCLOAK_TOKEN
    assert kwargs["data"]["grant_type"] == "authorization_code"
    assert kwargs["data"]["code"] == "code-xyz"
    assert kwargs["data"]["code_verifier"] == "verifier-xyz"
    assert kwargs["data"]["client_id"] == "nervecenter"
    # Confidential client: the client secret travels as Basic auth, never in body.
    assert "Basic " in kwargs["headers"]["Authorization"]
    assert "client_secret" not in kwargs["data"]


def test_sso_token_exchange_not_configured_501():
    """Without a client secret the exchange is refused with 501."""
    ctx = _ctx(client_secret="")
    client = _sso_client(ctx)
    r = client.post("/api/auth/sso/token", json={"code": "c", "code_verifier": "v"})
    assert r.status_code == 501
    assert r.json()["detail"] == "SSO token exchange is not configured"


def test_sso_token_exchange_invalid_code_401(monkeypatch):
    """A 4xx from the issuer → 401, and the issuer's error body is not leaked."""
    ctx = _ctx()
    _patch_http(monkeypatch, 400, {"error": "invalid_grant", "secret_echo": "top"})
    client = _sso_client(ctx)
    r = client.post(
        "/api/auth/sso/token",
        json={"code": "bad", "code_verifier": "v"},
    )
    assert r.status_code == 401
    assert "invalid" in r.json()["detail"]
    # The raw issuer error body must not reach the caller.
    assert "invalid_grant" not in r.text
    assert "secret_echo" not in r.text


def test_sso_token_exchange_issuer_5xx_401(monkeypatch):
    """A 5xx from the issuer maps to a generic 401 retry message."""
    ctx = _ctx()
    _patch_http(monkeypatch, 503, {"detail": "down"})
    client = _sso_client(ctx)
    r = client.post(
        "/api/auth/sso/token",
        json={"code": "c", "code_verifier": "v"},
    )
    assert r.status_code == 401
    assert "try again" in r.json()["detail"]


def test_sso_token_exchange_missing_code_422():
    """Missing required fields are rejected by the request model (422)."""
    ctx = _ctx()
    client = _sso_client(ctx)
    r = client.post("/api/auth/sso/token", json={"code_verifier": "v"})
    assert r.status_code == 422
