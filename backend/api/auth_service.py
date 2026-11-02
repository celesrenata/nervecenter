"""Signed-JWT auth core for NerveCenter (spec sso-integration, S101).

This module owns everything that is *not* an HTTP route:

- JWT minting and validation (AC-S02): NerveCenter issues HS256 tokens signed
  with ``NERVE_JWT_SECRET``.  When ``NERVE_SSO_ISSUER`` is set and the provider
  is live, tokens minted *by the issuer* (RS256) are additionally accepted and
  verified against the issuer's JWKS (Authentik).  Every NerveCenter-minted
  token carries ``sub`` / ``iat`` / ``exp`` / ``typ`` / ``roles`` plus a ``jti``
  that identifies its persistent :class:`~backend.persistence.models.auth.AuthSession`.
- Session persistence (AC-S03): every locally-minted token gets an
  ``auth_sessions`` row (PostgreSQL in production, in-process fallback when no
  database is configured) so an API restart never invalidates a still-valid
  JWT, and logout/revocation works by flipping ``revoked``.
- SSO discovery (AC-S01): :func:`sso_config` probes the issuer's
  ``/.well-known/openid-configuration`` and reports the standard shape
  (``issuer`` / ``client_id`` / ``authorization_endpoint`` / ``token_endpoint``
  / ``userinfo_endpoint`` / ``jwks_uri`` / ``enabled``).
- Device pairing (AC-S04 / prompot §71): :func:`initiate_pairing` mints a
  challenge (nonce, ``expires_at`` ≤ 5 min); :func:`confirm_pairing` binds the
  device and mints a device-credential JWT whose ``sub`` is ``device:<device_id>``.
- Approval binding (AC-S05 / prompot §72): :func:`create_approval` binds an
  approval to ``device_id`` + ``nonce`` + ``expires_at`` and an exact
  ``planned_change_id`` and stores a fingerprint of the change;
  :func:`verify_approval` invalidates it when the bound change is modified.

Environment contract
--------------------
- ``NERVE_JWT_SECRET`` — HS256 signing secret.  When unset a per-process
  secret is generated and a warning is logged (tokens are then valid only
  until the process restarts).
- ``NERVE_SSO_ISSUER`` — OIDC issuer; default
  ``https://sso.celestium.life/application/o/nervecenter/`` (spec AC-S01).
- ``NERVE_SSO_CLIENT_ID`` — the provider's client id ("" when unprovisioned).
- ``NERVE_LOCAL_ADMIN`` — break-glass: when set to a non-empty, non-falsy value
  the local-admin login is enabled; the password comes from
  ``NERVE_LOCAL_ADMIN_PASSWORD``.  There is **no** default password (AC-S03).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import secrets
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import httpx
import jwt
import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.persistence.models.auth import (
    ApprovalRecord,
    AuthDevice,
    AuthSession,
    PairingChallenge,
)

log = structlog.get_logger(__name__)

# ─── Environment contract ─────────────────────────────────────────────────────

ENV_JWT_SECRET = "NERVE_JWT_SECRET"
ENV_SSO_ISSUER = "NERVE_SSO_ISSUER"
ENV_SSO_CLIENT_ID = "NERVE_SSO_CLIENT_ID"
ENV_SSO_CLIENT_SECRET = "NERVE_SSO_CLIENT_SECRET"
ENV_SSO_JWKS_URI = "NERVE_SSO_JWKS_URI"
ENV_SSO_TOKEN_ENDPOINT = "NERVE_SSO_TOKEN_ENDPOINT"
ENV_LOCAL_ADMIN = "NERVE_LOCAL_ADMIN"
ENV_LOCAL_ADMIN_PASSWORD = "NERVE_LOCAL_ADMIN_PASSWORD"

DEFAULT_SSO_ISSUER = "https://sso.celestium.life/application/o/nervecenter/"
AUDIENCE = "nervecenter"

#: Token lifetimes.
ACCESS_TOKEN_TTL = timedelta(minutes=30)
REFRESH_TOKEN_TTL = timedelta(days=7)
DEVICE_CREDENTIAL_TTL = timedelta(days=30)
PAIRING_CHALLENGE_TTL = timedelta(minutes=5)
APPROVAL_TTL = timedelta(minutes=5)

#: The ``typ`` claim values minted by NerveCenter.
TYP_ACCESS = "access"
TYP_REFRESH = "refresh"
TYP_DEVICE = "device"

#: Local-admin subject.  The break-glass account has no default password — it
#: only exists when ``NERVE_LOCAL_ADMIN`` is enabled (AC-S03).
LOCAL_ADMIN_SUB = "local-admin"

#: JWKS / OIDC-discovery cache TTLs.
JWKS_CACHE_TTL_S = 300
PROBE_CACHE_TTL_S = 15

#: Planned-change content fields that participate in the approval fingerprint.
#: Any modification of one of these fields (or of the record) invalidates a
#: bound approval (prompot §72).
FINGERPRINT_FIELDS: tuple[str, ...] = (
    "id",
    "change_type",
    "title",
    "description",
    "status",
    "risk_level",
    "subject_ids",
    "diff",
    "rollback",
    "proposed_at",
    "expires_at",
)


class TokenError(Exception):
    """A token or operation was rejected.  ``detail`` maps to the HTTP 4xx body."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


def _parse_falsy(raw: str | None) -> bool:
    """True when ``raw`` is a non-empty, non-falsy env value."""
    if raw is None:
        return False
    return raw.strip().lower() not in ("", "0", "false", "no", "off")


# ─── JWKS / OIDC discovery ────────────────────────────────────────────────────

def _b64_int(jwk: dict[str, Any], name: str) -> int:
    raw = jwk[name]
    padding = "=" * (-len(raw) % 4)
    return int.from_bytes(base64.urlsafe_b64decode(raw + padding), "big")


def jwk_to_public_key(jwk: dict[str, Any]) -> Any:
    """Build a ``cryptography`` RSA public key from a JWK ``{kty: 'RSA'}`` entry.

    Only the ``n`` (modulus) and ``e`` (public exponent) members are required
    for an RSA JWK.  ``RSAPublicNumbers(...).public_key()`` returns a
    ``cryptography`` :class:`RSAPublicKey`, which is what ``jwt.decode``
    expects.
    """
    from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicNumbers

    return RSAPublicNumbers(_b64_int(jwk, "e"), _b64_int(jwk, "n")).public_key()


async def fetch_jwks(jwks_uri: str) -> dict[str, Any]:
    """Fetch and decode a JWKS document; returns ``{kid: rsa_public_key}``."""
    async with httpx.AsyncClient(timeout=httpx.Timeout(5.0)) as client:
        response = await client.get(jwks_uri)
        response.raise_for_status()
        data = response.json()

    keys: dict[str, Any] = {}
    for jwk in data.get("keys", []):
        kid = jwk.get("kid")
        if not kid or jwk.get("kty") != "RSA":
            continue
        keys[kid] = jwk_to_public_key(jwk)
    if not keys:
        raise TokenError("issuer JWKS contains no usable RSA keys")
    return keys


class JwksProvider:
    """Lazily-fetched, cached JWKS for the configured SSO issuer.

    :meth:`resolve_key` returns the public key matching a JWT header's ``kid``
    (rejecting unknown kids with a cache-bust retry).
    """

    def __init__(self, jwks_uri: str) -> None:
        self.jwks_uri = jwks_uri
        self._keys: dict[str, Any] = {}
        self._fetched_at = 0.0
        self._inflight: asyncio.Task[dict[str, Any]] | None = None

    async def resolve_key(self, token: str) -> Any:
        kid: str | None = None
        try:
            kid = jwt.get_unverified_header(token).get("kid")
        except Exception:  # noqa: BLE001 - header read is best-effort
            kid = None
        if kid is not None and kid in self._keys:
            return self._keys[kid]

        keys = await self._load(force=(kid is not None and kid not in self._keys))
        if kid is not None and kid in keys:
            return keys[kid]
        raise TokenError(
            "issuer token signed with an unknown JWKS key"
            if kid
            else "issuer token is missing a 'kid' header"
        )

    async def _load(self, force: bool = False) -> dict[str, Any]:
        if not force and self._keys and time.monotonic() - self._fetched_at < JWKS_CACHE_TTL_S:
            return self._keys
        if self._inflight is None:
            self._inflight = asyncio.ensure_future(self._fetch())
            try:
                keys = await self._inflight
            finally:
                self._inflight = None
            self._keys = keys
            self._fetched_at = time.monotonic()
            return keys
        return await self._inflight  # single-flight: join the in-progress fetch

    async def _fetch(self) -> dict[str, Any]:
        return await fetch_jwks(self.jwks_uri)


# ─── Auth context (config + mint/verify) ──────────────────────────────────────

@dataclass
class AuthContext:
    """Process-level auth configuration for one app instance.

    ``session_factory`` is the async session factory bound to the deployed
    PostgreSQL database; ``None`` selects the in-process session store so the
    API stays bootable without a database (local development / mock mode).
    """

    secret: str
    issuer: str
    client_id: str
    local_admin_enabled: bool
    local_admin_password: str | None
    session_factory: async_sessionmaker[AsyncSession] | None = None

    #: Confidential-client secret for the server-side PKCE token exchange.  When
    #: set, the authorization-code exchange happens *server-side* (the secret
    #: never reaches the browser).  Empty for public-client / test contexts.
    client_secret: str = ""
    #: Optional explicit overrides for endpoint discovery.
    jwks_uri_override: str = ""
    token_endpoint_override: str = ""

    _jwks: JwksProvider | None = field(default=None, repr=False)

    @classmethod
    def from_env(
        cls, session_factory: async_sessionmaker[AsyncSession] | None = None
    ) -> AuthContext:
        secret = os.environ.get(ENV_JWT_SECRET, "").strip()
        if not secret:
            # Development posture: tokens are valid only until this process
            # restarts.  Production must set NERVE_JWT_SECRET (deployments
            # Kubernetes Secret ``nervecenter-auth``).
            log.warning(
                "auth.no_jwt_secret",
                detail="NERVE_JWT_SECRET is not set; issuing tokens with an "
                "ephemeral per-process secret",
            )
            secret = secrets.token_urlsafe(32)
        return cls(
            secret=secret,
            issuer=os.environ.get(ENV_SSO_ISSUER, "").strip() or DEFAULT_SSO_ISSUER,
            client_id=os.environ.get(ENV_SSO_CLIENT_ID, "").strip(),
            local_admin_enabled=_parse_falsy(os.environ.get(ENV_LOCAL_ADMIN)),
            local_admin_password=os.environ.get(ENV_LOCAL_ADMIN_PASSWORD),
            session_factory=session_factory,
            client_secret=os.environ.get(ENV_SSO_CLIENT_SECRET, "").strip(),
            jwks_uri_override=os.environ.get(ENV_SSO_JWKS_URI, "").strip(),
            token_endpoint_override=os.environ.get(ENV_SSO_TOKEN_ENDPOINT, "").strip(),
        )

    @property
    def issuer_base(self) -> str:
        """Issuer without the ``/.well-known/openid-configuration`` suffix."""
        suffix = "/.well-known/openid-configuration"
        if self.issuer.endswith(suffix):
            return self.issuer[: -len(suffix)]
        return self.issuer

    @property
    def _is_keycloak(self) -> bool:
        """True when the issuer is a Keycloak realm base URL.

        Keycloak exposes its OIDC endpoints under
        ``<realm>/protocol/openid-connect/`` (JWKS at ``certs``, token at
        ``token``), unlike Authentik which serves them under the realm root.
        """
        return "/realms/" in self.issuer

    @property
    def jwks_uri(self) -> str:
        """Resolve the issuer's JWKS location.

        An explicit ``jwks_uri_override`` (``NERVE_SSO_JWKS_URI``) wins.  For a
        Keycloak realm, use ``<base>/protocol/openid-connect/certs``; otherwise
        fall back to the Authentik convention ``<base>/jwks/``.
        """
        if self.jwks_uri_override:
            return self.jwks_uri_override
        base = self.issuer_base.rstrip("/")
        if self._is_keycloak:
            return f"{base}/protocol/openid-connect/certs"
        return base if base.endswith("/jwks/") else f"{base}/jwks/"

    @property
    def token_endpoint(self) -> str:
        """Resolve the issuer's token endpoint (for the server-side exchange).

        An explicit ``token_endpoint_override`` (``NERVE_SSO_TOKEN_ENDPOINT``)
        wins; otherwise the Keycloak realm path or the Authentik root.
        """
        if self.token_endpoint_override:
            return self.token_endpoint_override
        base = self.issuer_base.rstrip("/")
        if self._is_keycloak:
            return f"{base}/protocol/openid-connect/token"
        return f"{base}/token/"

    def _jwks_provider(self) -> JwksProvider:
        if self._jwks is None:
            self._jwks = JwksProvider(self.jwks_uri)
        return self._jwks

    async def exchange_sso_code(
        self,
        code: str,
        code_verifier: str,
        redirect_uri: str,
    ) -> dict[str, Any]:
        """Exchange an authorization code for OIDC tokens *server-side*.

        The ``nervecenter`` client is a **confidential** client (its token
        endpoint rejects requests without the client secret — verified live), so
        the code→token exchange cannot happen in the browser.  This method POSTs
        to the issuer's token endpoint with the client secret (Basic auth), the
        ``code_verifier`` (PKCE), and the exact ``redirect_uri`` that was used in
        the authorization request, and returns the token response
        (``access_token`` / ``refresh_token`` / ``expires_in`` /
        ``token_type``).  The raw issuer error body is never returned to callers
        (see :class:`TokenError`).
        """
        if not self.client_secret:
            raise TokenError(
                "SSO token exchange is not configured (NERVE_SSO_CLIENT_SECRET)"
            )
        if not code or not code_verifier:
            raise TokenError("code and code_verifier are required")

        form: dict[str, str] = {
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": code_verifier,
            "client_id": self.client_id,
        }
        if redirect_uri:
            form["redirect_uri"] = redirect_uri

        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(10.0)
            ) as client:
                response = await client.post(
                    self.token_endpoint,
                    data=form,
                    headers={"Authorization":
                             f"Basic {base64.b64encode(f'{self.client_id}:{self.client_secret}'.encode()).decode()}"},
                )
        except httpx.HTTPError as exc:
            raise TokenError(
                "could not reach the SSO token endpoint "
                "(SSO token exchange unavailable)"
            ) from exc

        if response.status_code != 200:
            # A 4xx is a client-side failure (bad/expired code, wrong
            # redirect_uri, bad PKCE verifier).  Do NOT surface the issuer's
            # error body — it can echo secrets.
            status = response.status_code
            if 400 <= status < 500:
                raise TokenError(
                    "SSO authorization code is invalid, expired, or was "
                    "already used"
                )
            raise TokenError("SSO token exchange failed; please try again")

        try:
            data = response.json()
        except ValueError as exc:
            raise TokenError("SSO token endpoint returned a non-JSON response") from exc
        if not isinstance(data, dict) or not data.get("access_token"):
            raise TokenError("SSO token exchange returned no access token")

        log.info(
            "auth.sso_code_exchanged",
            sub=str(data.get("sub", ""))[:120],
            token_type=str(data.get("token_type", "")),
        )
        return data

    # -- token minting ---------------------------------------------------------

    def _mint(
        self,
        *,
        sub: str,
        typ: str,
        roles: list[str],
        ttl: timedelta,
        device_id: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> tuple[str, datetime, dict[str, Any]]:
        iat = datetime.now(UTC)
        exp = iat + ttl
        jti = secrets.token_urlsafe(24)
        claims: dict[str, Any] = {
            "sub": sub,
            "iss": self.issuer_base,
            "aud": AUDIENCE,
            "typ": typ,
            "roles": roles,
            "iat": iat,
            "exp": exp,
            "jti": jti,
        }
        if device_id is not None:
            claims["device_id"] = device_id
        if extra:
            claims.update(extra)
        token = jwt.encode(claims, self.secret, algorithm="HS256")
        return token, exp, claims

    def issue_access_token(
        self,
        sub: str,
        roles: list[str] | None = None,
        *,
        device_id: str | None = None,
    ) -> tuple[str, datetime]:
        """Mint an access token.  Returns ``(token, expires_at)``."""
        token, exp, _ = self._mint(
            sub=sub, typ=TYP_ACCESS, roles=list(roles or []), ttl=ACCESS_TOKEN_TTL,
            device_id=device_id,
        )
        return token, exp

    def issue_refresh_token(
        self, sub: str, *, roles: list[str] | None = None
    ) -> tuple[str, datetime]:
        """Mint a refresh token bound to the same subject.  Returns ``(token, expires_at)``."""
        token, exp, _ = self._mint(
            sub=sub, typ=TYP_REFRESH, roles=list(roles or []), ttl=REFRESH_TOKEN_TTL
        )
        return token, exp

    def issue_device_credential(
        self, device_id: str, *, roles: list[str] | None = None
    ) -> tuple[str, datetime]:
        """Mint a device credential bound to ``device_id`` (§71)."""
        token, exp, _ = self._mint(
            sub=f"device:{device_id}",
            typ=TYP_DEVICE,
            roles=list(roles or ["device"]),
            ttl=DEVICE_CREDENTIAL_TTL,
            device_id=device_id,
        )
        return token, exp

    # -- token validation ------------------------------------------------------

    @staticmethod
    def _validate_typ(claims: dict[str, Any], expected_typ: str) -> None:
        typ = claims.get("typ")
        if typ != expected_typ:
            raise TokenError(f"invalid token type: expected {expected_typ!r}, got {typ!r}")

    async def verify_token(
        self,
        token: str,
        expected_typ: str | None = None,
        *,
        require_session: bool = True,
    ) -> dict[str, Any]:
        """Verify a presented JWT and return its claims.

        Raises :class:`TokenError` (mapped to HTTP 401 by the endpoint layer) for:
        malformed tokens, expired tokens, wrong ``typ``, tokens signed with an
        unknown/foreign key, tampered payloads, and — for locally-minted tokens
        — revoked or unknown sessions.

        RS256 tokens are minted by the *issuer* (Authentik) and verified
        statelessly against the JWKS; they have no NerveCenter session row, so
        the session check applies only to HS256 (locally-minted) tokens.
        """
        header = jwt.get_unverified_header(token)
        alg = header.get("alg")
        if alg == "HS256":
            try:
                claims = jwt.decode(
                    token,
                    self.secret,
                    algorithms=["HS256"],
                    audience=AUDIENCE,
                    options={"require": ["exp", "iat", "sub", "typ"]},
                )
            except jwt.ExpiredSignatureError as exc:
                raise TokenError("token has expired") from exc
            except jwt.PyJWTError as exc:
                raise TokenError(f"token rejected: {exc}") from exc
            # Locally-minted tokens: enforce issuer + audience so a token from
            # another NerveCenter instance is not silently accepted.
            if claims.get("iss") != self.issuer_base:
                raise TokenError("token issuer mismatch")
            if claims.get("aud") not in (AUDIENCE, [AUDIENCE]):
                raise TokenError("token audience mismatch")
        elif alg in ("RS256", "RS384", "RS512"):
            key = await self._jwks_provider().resolve_key(token)
            # An issuer-minted token is addressed to the *client id* (not the
            # NerveCenter app audience), so the ``aud`` check targets the client
            # id.  PyJWT validates ``aud`` whenever ``audience`` is supplied,
            # which also satisfies the OIDC ``aud`` requirement; when no client
            # id is configured (local dev / tests) the check is disabled.
            audience = self.client_id or None
            if audience is None:
                options = {"require": ["exp", "sub"], "verify_aud": False}
            else:
                options = {"require": ["exp", "sub"]}
            try:
                claims = jwt.decode(
                    token,
                    key,
                    algorithms=["RS256", "RS384", "RS512"],
                    audience=audience,
                    options=options,
                )
            except jwt.ExpiredSignatureError as exc:
                raise TokenError("token has expired") from exc
            except jwt.PyJWTError as exc:
                raise TokenError(f"issuer token rejected: {exc}") from exc
            # Issuer: an issuer-minted token's ``iss`` is the issuer/realm base.
            # Tolerate a trailing slash on either side.
            if str(claims.get("iss") or "").rstrip("/") != self.issuer_base.rstrip("/"):
                raise TokenError("token issuer mismatch")
        else:
            raise TokenError(f"unsupported token algorithm: {alg!r}")

        # The ``typ`` claim is minted only by NerveCenter itself (HS256).  An
        # issuer-minted (RS256) token carries no ``typ``, so the type check
        # applies only to locally-minted tokens.
        if expected_typ is not None and alg == "HS256":
            self._validate_typ(claims, expected_typ)

        if require_session and alg == "HS256":
            jti = claims.get("jti")
            if not jti:
                raise TokenError("token is missing its session identifier (jti)")
            if await self._session_for(jti, claims) is None:
                raise TokenError("token session is unknown or has been revoked")

        return claims

    async def _session_for(self, jti: str, claims: dict[str, Any]) -> Any:
        """Look the token's session row up; ``None`` when absent/revoked/mismatched."""
        if self.session_factory is None:
            row = _memory_store.get_session(jti)
        else:
            async with self.session_factory() as session:
                row = (
                    await session.execute(select(AuthSession).where(AuthSession.id == jti))
                ).scalar_one_or_none()
        if row is None or getattr(row, "revoked", False):
            return None
        if row.sub != claims.get("sub") or row.typ != claims.get("typ"):
            return None
        return row


# ─── In-process session store (no-DB fallback + test double) ──────────────────

class _InMemoryStore:
    """Stand-in for PostgreSQL when no database is configured.

    State survives across *app instances* (module-level) so a recreated
    FastAPI app — the test model for "API restart" — does not lose it.
    """

    def __init__(self) -> None:
        self.sessions: dict[str, Any] = {}
        self.devices: dict[str, Any] = {}
        self.challenges: dict[str, Any] = {}
        self.approvals: dict[str, Any] = {}

    def get_session(self, jti: str) -> Any:
        return self.sessions.get(jti)


_memory_store = _InMemoryStore()


def reset_memory_store() -> None:
    """Test helper: wipe the in-process store between tests."""
    _memory_store.__init__()


# ─── Session persistence helpers (shared by endpoints) ────────────────────────

async def record_session(
    ctx: AuthContext,
    *,
    jti: str,
    sub: str,
    typ: str,
    expires_at: datetime,
    source: str,
    device_id: str | None = None,
) -> None:
    """Persist an :class:`AuthSession` for a freshly minted token."""
    if ctx.session_factory is None:
        _memory_store.sessions[jti] = SimpleNamespace(
            id=jti, sub=sub, typ=typ, source=source, device_id=device_id,
            issued_at=datetime.now(UTC), expires_at=expires_at,
            revoked=False, revoked_at=None,
        )
        return
    async with ctx.session_factory() as session:
        session.add(
            AuthSession(
                id=jti, sub=sub, typ=typ, source=source,
                device_id=device_id, expires_at=expires_at,
            )
        )
        await session.commit()


async def revoke_session(ctx: AuthContext, jti: str) -> bool:
    """Revoke the session identified by ``jti``; returns True when it existed."""
    if ctx.session_factory is None:
        session = _memory_store.sessions.get(jti)
        if session is None:
            return False
        session.revoked = True
        session.revoked_at = datetime.now(UTC)
        return True
    async with ctx.session_factory() as session:
        row = (
            await session.execute(select(AuthSession).where(AuthSession.id == jti))
        ).scalar_one_or_none()
        if row is None:
            return False
        row.revoked = True
        row.revoked_at = datetime.now(UTC)
        await session.commit()
        return True


# ─── SSO discovery (AC-S01) ───────────────────────────────────────────────────

@dataclass
class _ProbeCache:
    at: float = 0.0
    ok: bool = False
    issuer: str = ""
    discovery: dict[str, Any] | None = None


_probe_cache: _ProbeCache | None = None


def reset_discovery_cache() -> None:
    """Test helper: drop the OIDC discovery probe cache."""
    global _probe_cache
    _probe_cache = None


async def probe_provider(issuer: str) -> tuple[bool, dict[str, Any] | None]:
    """Check whether the SSO provider is live; returns ``(live, discovery_doc)``.

    A provider is live when ``{issuer base}/.well-known/openid-configuration``
    answers 200 with JSON carrying an ``issuer`` field.
    """
    global _probe_cache
    base = issuer
    suffix = "/.well-known/openid-configuration"
    if base.endswith(suffix):
        base = base[: -len(suffix)]
    url = f"{base.rstrip('/')}/.well-known/openid-configuration"

    now = time.monotonic()
    if (
        _probe_cache is not None
        and _probe_cache.issuer == issuer
        and _probe_cache.at > now - PROBE_CACHE_TTL_S
    ):
        return _probe_cache.ok, _probe_cache.discovery

    ok = False
    discovery: dict[str, Any] | None = None
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(3.0)) as client:
            response = await client.get(url)
            if response.status_code == 200:
                try:
                    data = response.json()
                except Exception:  # noqa: BLE001 - non-JSON body → not live
                    data = None
                if isinstance(data, dict) and data.get("issuer"):
                    ok = True
                    discovery = data
    except Exception as exc:  # noqa: BLE001 - unreachable provider is "not live"
        log.debug("auth.sso_probe_failed", url=url, error=str(exc))

    _probe_cache = _ProbeCache(
        at=time.monotonic(), ok=ok, issuer=issuer, discovery=discovery
    )
    return ok, discovery


def sso_config(ctx: AuthContext, discovery: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build the ``GET /api/auth/sso/config`` body (AC-S01 shape).

    Endpoints come from the live discovery document when available, otherwise
    they are derived from the issuer so the shape is complete even before the
    Authentik provider exists.  ``enabled`` is true only when the provider is
    live AND a client id is configured.
    """
    base = ctx.issuer_base
    discovered = discovery or {}
    endpoints = {
        "authorization_endpoint": discovered.get(
            "authorization_endpoint", f"{base}/authorize/"
        ),
        "token_endpoint": discovered.get("token_endpoint", f"{base}/token/"),
        "userinfo_endpoint": discovered.get("userinfo_endpoint", f"{base}/userinfo/"),
        "jwks_uri": discovered.get("jwks_uri", ctx.jwks_uri),
        "end_session_endpoint": discovered.get("end_session_endpoint"),
    }
    enabled = bool(discovered) and bool(ctx.client_id)
    return {
        "enabled": enabled,
        "issuer": ctx.issuer,
        "client_id": ctx.client_id,
        "authorization_endpoint": endpoints["authorization_endpoint"],
        "token_endpoint": endpoints["token_endpoint"],
        "userinfo_endpoint": endpoints["userinfo_endpoint"],
        "jwks_uri": endpoints["jwks_uri"],
        "end_session_endpoint": endpoints["end_session_endpoint"],
        "grant_types": ["authorization_code"],
        "code_challenge_methods": ["S256"],
        "audience": AUDIENCE,
        "local_admin_enabled": ctx.local_admin_enabled,
    }


# ─── Device pairing (AC-S04 / prompot §71) ────────────────────────────────────

def _pairing_view(challenge: Any) -> dict[str, Any]:
    return {
        "challenge_id": challenge.challenge_id,
        "nonce": challenge.nonce,
        "status": challenge.status,
        "expires_at": challenge.expires_at.isoformat(),
        "created_at": challenge.created_at.isoformat(),
    }


async def initiate_pairing(ctx: AuthContext, *, by_sub: str) -> dict[str, Any]:
    """Start device pairing for an authenticated session (prompot §71).

    Returns the challenge (``challenge_id``, ``nonce``, ``expires_at`` ≤
    ``now + 5 min``) that the new device must present to :func:`confirm_pairing`.
    """
    challenge_id = secrets.token_urlsafe(12)
    nonce = secrets.token_urlsafe(24)
    expires_at = datetime.now(UTC) + PAIRING_CHALLENGE_TTL

    if ctx.session_factory is None:
        _memory_store.challenges[challenge_id] = SimpleNamespace(
            challenge_id=challenge_id, nonce=nonce, requested_by=by_sub,
            status="pending", expires_at=expires_at, created_at=datetime.now(UTC),
            confirmed_at=None,
        )
        return _pairing_view(_memory_store.challenges[challenge_id])

    async with ctx.session_factory() as session:
        challenge = PairingChallenge(
            challenge_id=challenge_id, nonce=nonce,
            requested_by=by_sub, expires_at=expires_at,
        )
        session.add(challenge)
        await session.commit()
        await session.refresh(challenge)
        return _pairing_view(challenge)


async def _load_challenge(ctx: AuthContext, challenge_id: str) -> Any | None:
    if ctx.session_factory is None:
        return _memory_store.challenges.get(challenge_id)
    async with ctx.session_factory() as session:
        return (
            await session.execute(
                select(PairingChallenge).where(
                    PairingChallenge.challenge_id == challenge_id
                )
            )
        ).scalar_one_or_none()


def _device_view(device: Any) -> dict[str, Any]:
    return {
        "device_id": device.device_id,
        "name": device.name,
        "platform": device.platform,
        "status": device.status,
        "created_at": device.created_at.isoformat() if device.created_at else None,
        "last_seen_at": device.last_seen_at.isoformat() if device.last_seen_at else None,
    }


def _jti_of(token: str, ctx: AuthContext) -> str:
    return jwt.decode(
        token,
        ctx.secret,
        algorithms=["HS256"],
        audience=AUDIENCE,
        options={"verify_exp": False},
    )["jti"]


async def confirm_pairing(
    ctx: AuthContext,
    *,
    challenge_id: str,
    nonce: str,
    device_name: str,
    platform: str = "ios",
    attestation: dict[str, Any] | None = None,
    device_id: str | None = None,
) -> dict[str, Any]:
    """Confirm a pairing challenge and mint the device credential (§71).

    Raises :class:`TokenError` (→ 4xx at the endpoint layer) when the challenge
    is unknown, already used, expired, or the nonce does not match.
    """
    challenge = await _load_challenge(ctx, challenge_id)
    if challenge is None:
        raise TokenError("unknown pairing challenge")
    now = datetime.now(UTC)
    if challenge.status != "pending":
        raise TokenError(
            f"pairing challenge is {challenge.status!r}; challenges are single-use"
        )
    if challenge.expires_at <= now:
        challenge.status = "expired"
        raise TokenError("pairing challenge has expired")
    if challenge.nonce != nonce:
        await _mark_challenge(ctx, challenge, "failed")
        raise TokenError("pairing nonce does not match")
    if not device_name or not str(device_name).strip():
        raise TokenError("device_name is required")

    device_id = (device_id or f"dev-{secrets.token_urlsafe(10)}").strip()
    sub = f"device:{device_id}"
    device_token, device_exp = ctx.issue_device_credential(device_id)
    await record_session(
        ctx,
        jti=_jti_of(device_token, ctx),
        sub=sub,
        typ=TYP_DEVICE,
        expires_at=device_exp,
        source="pairing",
        device_id=device_id,
    )
    challenge.status = "confirmed"
    challenge.confirmed_at = now

    if ctx.session_factory is None:
        _memory_store.devices[device_id] = SimpleNamespace(
            device_id=device_id, name=device_name.strip(), platform=platform,
            status="active", attestation=attestation,
            created_at=datetime.now(UTC), last_seen_at=None, revoked_at=None,
        )
        return {
            "device": _device_view(_memory_store.devices[device_id]),
            "device_credential": device_token,
            "token_type": "Bearer",
            "expires_at": device_exp.isoformat(),
        }

    async with ctx.session_factory() as session:
        device = (
            await session.execute(
                select(AuthDevice).where(AuthDevice.device_id == device_id)
            )
        ).scalar_one_or_none()
        if device is None:
            device = AuthDevice(
                device_id=device_id,
                name=device_name.strip(),
                platform=platform,
                attestation=attestation,
            )
            session.add(device)
        device.status = "active"
        device.last_seen_at = datetime.now(UTC)
        device.revoked_at = None
        await session.commit()
        await session.refresh(device)
        return {
            "device": _device_view(device),
            "device_credential": device_token,
            "token_type": "Bearer",
            "expires_at": device_exp.isoformat(),
        }


async def _mark_challenge(ctx: AuthContext, challenge: Any, status: str) -> None:
    if ctx.session_factory is None:
        challenge.status = status
        return
    async with ctx.session_factory() as session:
        row = (
            await session.execute(
                select(PairingChallenge).where(
                    PairingChallenge.challenge_id == challenge.challenge_id
                )
            )
        ).scalar_one_or_none()
        if row is not None:
            row.status = status
            await session.commit()


async def list_devices(ctx: AuthContext) -> list[dict[str, Any]]:
    """List paired devices (backing store for the clients/approvals surfaces)."""
    if ctx.session_factory is None:
        return [_device_view(d) for d in _memory_store.devices.values()]
    async with ctx.session_factory() as session:
        rows = (await session.execute(select(AuthDevice))).scalars().all()
    return [_device_view(r) for r in rows]


async def revoke_device(ctx: AuthContext, device_id: str) -> bool:
    """Revoke a paired device and all of its sessions (break-glass)."""
    if ctx.session_factory is None:
        device = _memory_store.devices.get(device_id)
        if device is None:
            return False
        device.status = "revoked"
        device.revoked_at = datetime.now(UTC)
        for session in _memory_store.sessions.values():
            if getattr(session, "device_id", None) == device_id:
                session.revoked = True
                session.revoked_at = datetime.now(UTC)
        return True
    async with ctx.session_factory() as session:
        device = (
            await session.execute(
                select(AuthDevice).where(AuthDevice.device_id == device_id)
            )
        ).scalar_one_or_none()
        if device is None:
            return False
        device.status = "revoked"
        device.revoked_at = datetime.now(UTC)
        for row in (
            await session.execute(
                select(AuthSession).where(
                    AuthSession.device_id == device_id,
                    AuthSession.revoked.is_(False),
                )
            )
        ).scalars():
            row.revoked = True
            row.revoked_at = datetime.now(UTC)
        await session.commit()
        return True


# ─── Approval binding (AC-S05 / prompot §72) ──────────────────────────────────

def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def planned_change_fingerprint(change: dict[str, Any] | None) -> str:
    """SHA-256 over the binding-relevant content of a PlannedChange (§72)."""
    fields = {name: (change or {}).get(name) for name in FINGERPRINT_FIELDS}
    return hashlib.sha256(_canonical_json(fields).encode("utf-8")).hexdigest()


def _approval_view(approval: Any) -> dict[str, Any]:
    return {
        "approval_id": approval.id,
        "planned_change_id": approval.planned_change_id,
        "device_id": approval.device_id,
        "approver_sub": approval.approver_sub,
        "nonce": approval.nonce,
        "expires_at": approval.expires_at.isoformat(),
        "change_fingerprint": approval.change_fingerprint,
        "decision": approval.decision,
        "invalidation_reason": getattr(approval, "invalidation_reason", None),
        "created_at": approval.created_at.isoformat() if approval.created_at else None,
    }


async def create_approval(
    ctx: AuthContext,
    *,
    planned_change_id: str,
    change: dict[str, Any] | None,
    nonce: str,
    approver_sub: str,
    device_id: str,
    rationale: str | None = None,
) -> dict[str, Any]:
    """Bind an approval to ``device_id`` + ``nonce`` + ``expires_at`` + an exact
    ``planned_change_id`` (prompot §72).

    ``change`` is the *current* representation of the bound planned change; its
    fingerprint is captured now, so any later modification invalidates the
    approval (see :func:`verify_approval`).
    """
    expires_at = datetime.now(UTC) + APPROVAL_TTL
    fingerprint = planned_change_fingerprint(change)

    if ctx.session_factory is None:
        approval_id = secrets.token_urlsafe(16)
        approval = SimpleNamespace(
            id=approval_id, planned_change_id=planned_change_id,
            device_id=device_id, approver_sub=approver_sub, nonce=nonce,
            expires_at=expires_at, change_fingerprint=fingerprint,
            decision="pending", invalidation_reason=None,
            rationale=rationale, created_at=datetime.now(UTC), decided_at=None,
        )
        _memory_store.approvals[approval_id] = approval
        return _approval_view(approval)

    async with ctx.session_factory() as session:
        approval = ApprovalRecord(
            planned_change_id=planned_change_id,
            device_id=device_id,
            approver_sub=approver_sub,
            nonce=nonce,
            expires_at=expires_at,
            change_fingerprint=fingerprint,
            rationale=rationale,
        )
        session.add(approval)
        await session.commit()
        await session.refresh(approval)
        return _approval_view(approval)


async def _load_approval(ctx: AuthContext, approval_id: str) -> Any | None:
    if ctx.session_factory is None:
        return _memory_store.approvals.get(approval_id)
    async with ctx.session_factory() as session:
        return (
            await session.execute(
                select(ApprovalRecord).where(ApprovalRecord.id == approval_id)
            )
        ).scalar_one_or_none()


async def _device_revoked(ctx: AuthContext, device_id: str) -> bool:
    if ctx.session_factory is None:
        device = _memory_store.devices.get(device_id)
        return device is not None and device.status == "revoked"
    async with ctx.session_factory() as session:
        device = (
            await session.execute(
                select(AuthDevice).where(AuthDevice.device_id == device_id)
            )
        ).scalar_one_or_none()
    return device is not None and device.status == "revoked"


async def _set_invalid(ctx: AuthContext, approval: Any, reason: str) -> None:
    approval.decision = "invalidated"
    approval.invalidation_reason = reason
    if ctx.session_factory is not None:
        async with ctx.session_factory() as session:
            row = (
                await session.execute(
                    select(ApprovalRecord).where(ApprovalRecord.id == approval.id)
                )
            ).scalar_one_or_none()
            if row is not None:
                row.decision = "invalidated"
                row.invalidation_reason = reason
                await session.commit()


async def verify_approval(
    ctx: AuthContext,
    *,
    approval_id: str,
    change: dict[str, Any] | None,
    device_id: str | None = None,
) -> dict[str, Any]:
    """Check whether a bound approval is still valid for the *current* change.

    Invalidation order (§72, "any modification to the PlannedChange invalidates
    approval"):

    1. ``unknown`` — the approval does not exist.
    2. ``change_modified`` — the bound change's fingerprint no longer matches
       (any field in :data:`FINGERPRINT_FIELDS` changed).
    3. ``expired`` — past ``expires_at``.
    4. ``device_revoked`` — the bound device was revoked.
    5. ``device_mismatch`` — the presenting device differs from the binding.

    Returns ``{"valid": bool, "reason": str | None, "approval": {...}}``.
    """
    approval = await _load_approval(ctx, approval_id)
    if approval is None:
        return {
            "valid": False,
            "reason": "unknown",
            "detail": "approval does not exist",
            "approval": None,
        }

    if approval.change_fingerprint != planned_change_fingerprint(change):
        await _set_invalid(ctx, approval, "change_modified")
        return {
            "valid": False,
            "reason": "change_modified",
            "detail": "the bound planned change was modified; the approval is invalidated",
            "approval": _approval_view(approval),
        }

    if approval.expires_at <= datetime.now(UTC):
        await _set_invalid(ctx, approval, "expired")
        return {
            "valid": False,
            "reason": "expired",
            "detail": "the approval has expired",
            "approval": _approval_view(approval),
        }

    if device_id is not None and approval.device_id != device_id:
        return {
            "valid": False,
            "reason": "device_mismatch",
            "detail": "the presenting device does not match the approval binding",
            "approval": _approval_view(approval),
        }

    if await _device_revoked(ctx, approval.device_id):
        await _set_invalid(ctx, approval, "device_revoked")
        return {
            "valid": False,
            "reason": "device_revoked",
            "detail": "the approving device has been revoked",
            "approval": _approval_view(approval),
        }

    return {"valid": True, "reason": None, "approval": _approval_view(approval)}


async def decide_approval(
    ctx: AuthContext,
    *,
    approval_id: str,
    decision: str,
    change: dict[str, Any] | None,
    device_id: str | None = None,
    nonce: str | None = None,
    rationale: str | None = None,
) -> dict[str, Any]:
    """Record a device verdict (``approved`` / ``denied``) on a bound approval.

    The verdict only applies while the approval is still valid: the bound
    change must be unmodified, the approval unexpired, the presenting device
    must match the binding, and the presented nonce (if given) must match.
    """
    if decision not in ("approved", "denied"):
        raise TokenError("decision must be 'approved' or 'denied'")

    approval = await _load_approval(ctx, approval_id)
    if approval is None:
        raise TokenError("unknown approval")
    if nonce is not None and nonce != approval.nonce:
        raise TokenError("approval nonce does not match")

    check = await verify_approval(
        ctx, approval_id=approval_id, change=change, device_id=device_id
    )
    if not check["valid"]:
        raise TokenError(f"approval is not valid: {check['reason']}")

    approval.decision = decision
    approval.rationale = rationale or getattr(approval, "rationale", None)
    approval.decided_at = datetime.now(UTC)
    if ctx.session_factory is not None:
        async with ctx.session_factory() as session:
            row = (
                await session.execute(
                    select(ApprovalRecord).where(ApprovalRecord.id == approval_id)
                )
            ).scalar_one_or_none()
            if row is not None:
                row.decision = decision
                row.rationale = rationale
                row.decided_at = datetime.now(UTC)
                await session.commit()
    return _approval_view(approval)


async def list_approvals(ctx: AuthContext) -> list[dict[str, Any]]:
    """List approvals (``GET /api/approvals`` backing store for S101)."""
    if ctx.session_factory is None:
        return [_approval_view(a) for a in _memory_store.approvals.values()]
    async with ctx.session_factory() as session:
        rows = (await session.execute(select(ApprovalRecord))).scalars().all()
    return [_approval_view(r) for r in rows]
