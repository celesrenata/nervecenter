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
    aud: str | None = "nervecenter",
    iss: str = KEYCLOAK_ISSUER,
    sub: str | None = SUB,
    include_iat: bool = False,
    delta: int = 300,
    preferred_username: str | None = None,
    email: str | None = None,
) -> str:
    """Mint an RS256 token shaped like a Keycloak access token.

    Deliberately omits the ``typ`` claim (Keycloak access tokens carry none) and,
    by default, the ``iat`` claim, to exercise the relaxed ``require`` set.

    ``aud`` and ``sub`` are optional — passing ``None`` omits the claim entirely,
    which mirrors the live realm's tokens (minted for a confidential client with
    no audience restriction).  ``preferred_username`` / ``email`` mirror the
    OIDC identity claims Keycloak exposes in the access token when the
    ``profile`` / ``email`` scopes are requested.
    """
    now = int(time.time())
    claims: dict = {
        "iss": iss,
        "exp": now + delta,
        "jti": "kc-jti-1",
    }
    if aud is not None:
        claims["aud"] = aud
    if sub is not None:
        claims["sub"] = sub
    if include_iat:
        claims["iat"] = now
    if preferred_username is not None:
        claims["preferred_username"] = preferred_username
    if email is not None:
        claims["email"] = email
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
    with pytest.raises(TokenError, match="audience"):
        await ctx.verify_token(token)


async def test_keycloak_rs256_no_aud_no_sub_accepted(jwks):
    """A realm token with no ``aud`` and no ``sub`` is accepted.

    This is the shape the live realm mints for a confidential client: the
    access token carries neither an audience nor a subject, only identity
    claims.  The JWKS signature + issuer + expiry are what establish trust.
    """
    ctx = _ctx()
    token = _keycloak_access_token(
        jwks,
        aud=None,
        sub=None,
        preferred_username="admin",
        email="admin@celestium.life",
    )
    claims = await ctx.verify_token(token, expected_typ=TYP_ACCESS)
    assert claims["iss"] == KEYCLOAK_ISSUER
    assert claims["preferred_username"] == "admin"
    assert "sub" not in claims


async def test_keycloak_rs256_aud_client_accepted(jwks):
    """A token whose ``aud`` is the client id (the common OIDC shape) is accepted."""
    ctx = _ctx()
    token = _keycloak_access_token(jwks, aud="nervecenter", sub=SUB)
    claims = await ctx.verify_token(token)
    assert claims["aud"] == "nervecenter"


async def test_keycloak_rs256_no_aud_with_preferred_username(jwks):
    """No-``aud`` token with identity claims resolves the display username."""
    ctx = _ctx()
    token = _keycloak_access_token(
        jwks, aud=None, sub=None, preferred_username="admin"
    )
    claims = await ctx.verify_token(token)
    assert claims["preferred_username"] == "admin"


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


# ─── SSO identity resolution (id_token) ───────────────────────────────────────


async def test_resolve_sso_identity_from_id_token(jwks):
    """The id_token's identity claims win over an empty access token."""
    ctx = _ctx()
    # Access token: realm shape, no sub / preferred_username / email.
    access = _keycloak_access_token(jwks, sub=None, aud=None)
    # id_token: full OIDC identity.
    id_token = _keycloak_access_token(
        jwks,
        aud="nervecenter",
        sub=SUB,
        preferred_username="celes",
        email="celes@celestium.life",
    )
    identity = await ctx.resolve_sso_identity(
        {"access_token": access, "id_token": id_token}
    )
    assert identity["sub"] == SUB
    assert identity["username"] == "celes"
    assert identity["email"] == "celes@celestium.life"


async def test_resolve_sso_identity_username_prefers_preferred_username(jwks):
    """username is the display name (preferred_username), not the raw sub UUID."""
    ctx = _ctx()
    id_token = _keycloak_access_token(
        jwks,
        aud="nervecenter",
        sub=SUB,
        preferred_username="admin",
    )
    identity = await ctx.resolve_sso_identity({"id_token": id_token})
    assert identity["username"] == "admin"
    assert identity["sub"] == SUB


async def test_resolve_sso_identity_no_id_token_empty(jwks):
    """Without an id_token or access token, the identity is empty."""
    ctx = _ctx()
    identity = await ctx.resolve_sso_identity({})
    assert identity == {"sub": None, "username": None, "email": None}


async def test_resolve_sso_identity_malformed_access_token_no_raise():
    """A placeholder / malformed access token must not break the exchange
    (mirrors the endpoint test's 'AT' fake token)."""
    ctx = _ctx()
    identity = await ctx.resolve_sso_identity({"access_token": "AT"})
    assert identity == {"sub": None, "username": None, "email": None}


async def test_resolve_sso_identity_id_token_rejected_falls_back(jwks):
    """A foreign-issuer id_token is rejected; identity falls back to the
    access token's claims (here, only the raw sub)."""
    ctx = _ctx()
    access = _keycloak_access_token(jwks, sub=SUB)
    id_token = _keycloak_access_token(
        jwks,
        aud="nervecenter",
        sub=SUB,
        iss="https://evil.example/realms/other",
        preferred_username="evil",
    )
    identity = await ctx.resolve_sso_identity(
        {"access_token": access, "id_token": id_token}
    )
    # The foreign id_token is dropped; the access token's sub is used and the
    # attacker's preferred_username never surfaces.
    assert identity["username"] == SUB
    assert identity.get("username") != "evil"


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
