"""Signed-JWT auth endpoints for the NerveCenter API (spec sso-integration, S101).

This module is the HTTP layer over :mod:`backend.api.auth_service`.  It exposes:

- ``GET  /api/auth/sso/config`` — AC-S01: the SSO/OIDC discovery document.
- ``POST /api/auth/login``      — AC-S03: break-glass local-admin login
  (no default password; disabled unless ``NERVE_LOCAL_ADMIN`` is enabled).
- ``GET  /api/auth/me``         — introspect the presenting access token.
- ``POST /api/auth/logout``     — revoke the presenting token's session and
  its refresh family, and clear the refresh cookie.
- ``POST /api/auth/logout-all`` — revoke every session of the caller.
- ``POST /api/auth/refresh``    — rotate the ``nc_rt`` refresh cookie and mint a
  new access token (NS-ADR-123).
- ``POST /api/auth/pairing/initiate`` — AC-S04 (§71): start a device pairing
  (``POST /api/auth/pairing`` is retained as a compatibility alias).
- ``POST /api/auth/pairing/confirm`` — AC-S04 (§71): confirm + mint device credential.
- ``POST /api/approvals/bind``  — AC-S05 (§72): bind an approval to a change.
- ``GET  /api/approvals/{id}/verify`` — AC-S05 (§72): read a bound approval.
- ``POST /api/approvals/{id}/verify`` — AC-S05 (§72): re-check approval validity.
- ``POST /api/approvals/{id}/decide`` — AC-S05 (§72): record a device verdict.

The old mock ``admin`` / ``password`` user store is **gone** (AC-S03).  There is
no default credential anywhere in the backend: the break-glass path only
resolves a session when both ``NERVE_LOCAL_ADMIN`` and
``NERVE_LOCAL_ADMIN_PASSWORD`` are configured, and both are compared in
constant time.
"""

from __future__ import annotations

import hmac
import secrets
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, Response, Security
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from backend.api import auth_service, auth_sessions
from backend.api.auth_service import (
    TYP_ACCESS,
    TokenError,
)

router = APIRouter(prefix="/api/auth", tags=["authentication"])
approvals_router = APIRouter(prefix="/api/approvals", tags=["approvals"])

bearer = HTTPBearer(auto_error=False)
log = structlog.get_logger(__name__)

#: Refresh cookie (HttpOnly, SameSite=Strict, scoped to ``/api/auth``).
REFRESH_COOKIE = "nc_rt"
REFRESH_COOKIE_PATH = "/api/auth"
#: Header the web client sends to ``/api/auth/refresh`` as a CSRF guard.
CSRF_HEADER = "X-NC-Refresh"


# ─── Request models ───────────────────────────────────────────────────────────


class LoginRequest(BaseModel):
    """Body for the break-glass local-admin login."""

    username: str = Field(
        default="local-admin",
        description="Only ``local-admin`` is supported.",
    )
    password: str = Field(
        min_length=1,
        description="The operator's break-glass password.",
    )


class SsoTokenRequest(BaseModel):
    """Body for ``POST /api/auth/sso/token`` (server-side PKCE token exchange).

    The authorization-code → token exchange happens *server-side* because the
    ``nervecenter`` OIDC client is confidential (its token endpoint requires the
    client secret, which never reaches the browser).  ``code`` and
    ``code_verifier`` are exactly the values the browser captured from the
    issuer's redirect; ``redirect_uri`` must match the authorization request.
    """

    code: str = Field(min_length=1, description="Authorization code from the issuer redirect.")
    code_verifier: str = Field(
        min_length=1,
        description="PKCE ``code_verifier`` (S256) matching the authorization request.",
    )
    redirect_uri: str = Field(
        default="",
        description="The exact ``redirect_uri`` used in the authorization request.",
    )


class PairingInitiateRequest(BaseModel):
    """Body for ``POST /api/auth/pairing/initiate`` (requires a valid access token)."""

    device_name: str = Field(
        min_length=1,
        max_length=120,
        description="Human-readable device name.",
    )
    platform: str = Field(
        default="ios",
        description="Device platform (ios, macos, android, web, cli).",
    )


class PairingConfirmRequest(BaseModel):
    """Body for ``POST /api/auth/pairing/confirm``.

    Carries the pairing challenge's ``nonce`` and the new device's identity.
    ``attestation`` is an opaque object (e.g. Secure Enclave / KeyChain
    attestation payload) stored opaquely on the device record.
    """

    challenge_id: str
    nonce: str
    device_name: str = Field(min_length=1, max_length=120)
    platform: str = Field(default="ios")
    attestation: dict[str, Any] | None = None
    device_id: str | None = Field(
        default=None, description="Optional stable device identifier."
    )


class ApprovalBindRequest(BaseModel):
    """Body for ``POST /api/approvals/bind`` (prompot §72).

    Binds the approval to the *current* content of ``planned_change_id`` so any
    later modification invalidates it.
    """

    planned_change_id: str
    nonce: str
    device_id: str
    rationale: str | None = None
    change: dict[str, Any] | None = Field(
        default=None,
        description="Current representation of the planned change (fingerprint source).",
    )


class ApprovalVerifyRequest(BaseModel):
    """Body for ``POST /api/approvals/{id}/verify`` (prompot §72).

    The caller presents the *current* change representation; the service
    recomputes its fingerprint and compares it against the bound one.
    """

    change: dict[str, Any] | None = None
    device_id: str | None = None


class ApprovalDecisionRequest(BaseModel):
    """Body for ``POST /api/approvals/{id}/decide`` (device verdict)."""

    decision: str
    nonce: str | None = None
    device_id: str | None = None
    change: dict[str, Any] | None = None
    rationale: str | None = None


# ─── Auth context (per-request) ────────────────────────────────────────────────


def _get_ctx(request: Request) -> "auth_service.AuthContext":
    """Return the app-scoped :class:`AuthContext`, building it on first use.

    ``request.app.state.db_engine`` (set by
    :func:`backend.api.wiring.install_dependency_wiring`) is wrapped in an
    ``async_sessionmaker`` when present; otherwise the in-process session store
    is used so the API stays bootable without a database.
    """
    ctx = getattr(request.app.state, "auth_ctx", None)
    if ctx is not None:
        return ctx
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    engine = getattr(request.app.state, "db_engine", None)
    session_factory = (
        async_sessionmaker(engine, expire_on_commit=False)
        if engine is not None
        else None
    )
    ctx = auth_service.AuthContext.from_env(session_factory=session_factory)
    try:
        request.app.state.auth_ctx = ctx
    except Exception:  # noqa: BLE001 - state may be read-only in some test harnesses
        pass
    return ctx


async def current_access(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Security(bearer),
) -> dict[str, Any]:
    """Dependency: return the verified claims of the presenting access token.

    Raises ``401`` for missing/invalid/expired/revoked tokens.
    """
    ctx = _get_ctx(request)
    if credentials is None or not credentials.credentials:
        raise HTTPException(status_code=401, detail="missing bearer token")
    token = credentials.credentials
    try:
        if auth_sessions.looks_like_pat(token):
            claims = await auth_sessions.authenticate_pat(ctx, token)
            route = request.scope.get("route")
            log.info(
                "auth.pat_used",
                token_id=claims["token_id"],
                token_name=claims["token_name"],
                method=request.method,
                route=getattr(route, "path", None) or "unknown",
            )
            return claims
        return await ctx.verify_token(token, expected_typ=TYP_ACCESS)
    except TokenError as exc:
        raise HTTPException(status_code=401, detail=exc.detail) from exc


def _constant_time_equal(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def _set_refresh_cookie(response: Response, ctx: auth_service.AuthContext, raw: str) -> None:
    """Deliver the refresh token only as an HttpOnly cookie (never in a body)."""
    response.set_cookie(
        REFRESH_COOKIE,
        raw,
        max_age=int(ctx.settings.absolute_lifetime.total_seconds()),
        httponly=True,
        secure=ctx.settings.cookie_secure,
        samesite="strict",
        path=REFRESH_COOKIE_PATH,
    )


def _clear_refresh_cookie(response: Response, ctx: auth_service.AuthContext) -> None:
    response.delete_cookie(
        REFRESH_COOKIE,
        path=REFRESH_COOKIE_PATH,
        httponly=True,
        secure=ctx.settings.cookie_secure,
        samesite="strict",
    )


def _session_body(access_token: str, access_exp: Any, identity: Any) -> dict[str, Any]:
    from datetime import UTC, datetime

    seconds = max(0, int((access_exp - datetime.now(UTC)).total_seconds()))
    return {
        "access_token": access_token,
        "token_type": "Bearer",
        "expires_in": seconds,
        "expires_at": access_exp.isoformat(),
        "identity": identity or {},
    }


# ─── SSO discovery (AC-S01) ───────────────────────────────────────────────────


@router.get("/sso/config")
async def sso_config(request: Request) -> dict[str, Any]:
    """Return the SSO/OIDC discovery document for the configured issuer.

    When the Authentik provider is live, ``enabled`` is ``true`` and the
    endpoint URLs come from the provider's ``/.well-known/openid-configuration``.
    When it is not yet live (or no ``NERVE_SSO_CLIENT_ID`` is configured),
    ``enabled`` is ``false`` and the endpoint URLs are derived from the issuer
    so the shape is complete for clients that cache it before the provider
    exists.
    """
    ctx = _get_ctx(request)
    ok, discovery = await auth_service.probe_provider(ctx.issuer)
    return auth_service.sso_config(ctx, discovery if ok else None)


@router.post("/sso/token")
async def sso_token_exchange(
    body: SsoTokenRequest, request: Request, response: Response
) -> dict[str, Any]:
    """Server-side PKCE authorization-code → token exchange.

    The ``nervecenter`` OIDC client is **confidential** (its token endpoint
    requires the client secret, which never reaches the browser), so the
    code→token exchange cannot run client-side.  This endpoint performs it
    server-side: it POSTs the captured ``code`` + ``code_verifier`` (+ the exact
    ``redirect_uri`` from the authorization request) to the issuer's token
    endpoint with the client secret (Basic auth) and returns the token response.

    After the issuer exchange succeeds, NerveCenter creates its own session
    (NS-ADR-123): the response carries a NerveCenter access token and the
    verified identity, and the refresh token travels only in the ``nc_rt``
    HttpOnly cookie.  The issuer's tokens and raw error body are never echoed to
    the caller.

    Raises ``501`` when the exchange is not configured (no client secret), and
    ``401`` for a bad / expired / already-used authorization code, a mismatched
    PKCE verifier, or an unreachable issuer token endpoint.
    """
    ctx = _get_ctx(request)
    if not ctx.client_secret:
        raise HTTPException(
            status_code=501,
            detail="SSO token exchange is not configured",
        )
    try:
        tokens = await ctx.exchange_sso_code(
            body.code, body.code_verifier, body.redirect_uri
        )
    except auth_service.TokenError as exc:
        raise HTTPException(status_code=401, detail=exc.detail) from exc

    # The realm-minted *access* token carries no identity claims (only
    # exp/iat/jti/iss/typ/azp/sid/scope), so ``/api/auth/me`` would fall back to
    # the anonymous "sso-user" placeholder.  The authoritative identity lives in
    # the ``id_token`` from the OIDC token response, which is verified against
    # the issuer JWKS (signature + issuer) and surfaced to the UI.  When the
    # id_token is absent or fails verification, the helper degrades gracefully
    # to the access token's claims, then to an empty identity.
    identity = await ctx.resolve_sso_identity(tokens)
    auth_service.log.info(
        "auth.sso_identity_resolved",
        sub=str(identity.get("sub") or "")[:80],
        username=str(identity.get("username") or "")[:80],
        email=str(identity.get("email") or "")[:80],
    )

    # NerveCenter owns the session from here (NS-ADR-123): the issuer's tokens
    # are not handed to the browser.  The refresh token is opaque, hashed at
    # rest and delivered only as an HttpOnly cookie.
    sub = (
        identity.get("sub")
        or identity.get("username")
        or identity.get("email")
        or f"sso-session:{secrets.token_hex(6)}"
    )
    raw_refresh, row = await auth_sessions.start_session(
        ctx,
        sub=str(sub),
        identity=identity,
        source="sso",
        user_agent=request.headers.get("user-agent"),
    )
    access_token, access_exp = await auth_sessions.issue_session_access_token(ctx, row)
    _set_refresh_cookie(response, ctx, raw_refresh)
    return _session_body(access_token, access_exp, identity)


# ─── Break-glass local admin (AC-S03) ─────────────────────────────────────────


@router.post("/login")
async def login(
    body: LoginRequest, request: Request, response: Response
) -> dict[str, Any]:
    """Break-glass login for the local admin (no default password).

    Only the ``local-admin`` username is accepted.  The login succeeds only when
    the break-glass path is enabled (``NERVE_LOCAL_ADMIN``) *and* the supplied
    password matches ``NERVE_LOCAL_ADMIN_PASSWORD`` in constant time.  On
    success an access + refresh token are minted and an ``auth_sessions`` row is
    persisted so the session survives an API restart.
    """
    ctx = _get_ctx(request)
    if body.username != auth_service.LOCAL_ADMIN_SUB:
        raise HTTPException(status_code=401, detail="unknown user")
    if not ctx.local_admin_enabled:
        raise HTTPException(
            status_code=403,
            detail="local-admin login is disabled (set NERVE_LOCAL_ADMIN)",
        )
    if not ctx.local_admin_password or not _constant_time_equal(
        body.password, ctx.local_admin_password
    ):
        raise HTTPException(status_code=401, detail="invalid credentials")

    sub = auth_service.LOCAL_ADMIN_SUB
    roles = ["admin"]
    raw_refresh, row = await auth_sessions.start_session(
        ctx,
        sub=sub,
        identity={"sub": sub, "username": sub, "email": None},
        source="local-admin",
        user_agent=request.headers.get("user-agent"),
    )
    access_token, access_exp = await auth_sessions.issue_session_access_token(ctx, row)
    refresh_token, refresh_exp = ctx.issue_refresh_token(sub, roles=roles)
    _set_refresh_cookie(response, ctx, raw_refresh)
    return {
        "token_type": "Bearer",
        "access_token": access_token,
        "expires_at": access_exp.isoformat(),
        "refresh_token": refresh_token,
        "refresh_expires_at": refresh_exp.isoformat(),
        "sub": sub,
        "roles": roles,
    }


@router.post("/refresh")
async def refresh_session(request: Request, response: Response) -> Any:
    """Rotate the refresh cookie and mint a new access token (NS-ADR-123).

    Self-authenticating: the ``nc_rt`` cookie is the credential and the
    ``X-NC-Refresh`` header is a CSRF guard (a cross-site form cannot set it).
    A replayed rotated token revokes its whole session family.
    """
    ctx = _get_ctx(request)
    if request.headers.get(CSRF_HEADER) != "1":
        raise HTTPException(status_code=401, detail="missing refresh header")
    raw = request.cookies.get(REFRESH_COOKIE)
    if not raw:
        raise HTTPException(status_code=401, detail="no refresh session")
    try:
        new_raw, row = await auth_sessions.rotate_session(
            ctx, raw, user_agent=request.headers.get("user-agent")
        )
    except TokenError as exc:
        failure = JSONResponse({"detail": exc.detail}, status_code=401)
        _clear_refresh_cookie(failure, ctx)
        return failure
    access_token, access_exp = await auth_sessions.issue_session_access_token(ctx, row)
    _set_refresh_cookie(response, ctx, new_raw)
    return _session_body(access_token, access_exp, row.identity)


@router.get("/me")
async def me(claims: dict[str, Any] = Depends(current_access)) -> dict[str, Any]:
    """Introspect the presenting access token.

    ``username`` is the best-effort display identity: for locally-minted
    (HS256) tokens it is the subject, while for issuer-minted (RS256) tokens —
    which may carry no ``sub`` — it falls back to the OIDC identity claims
    (``preferred_username`` then ``email``).  ``email`` is surfaced so the UI
    can display the signed-in user instead of an anonymous placeholder.
    """
    sub = claims.get("sub")
    preferred = claims.get("preferred_username")
    email = claims.get("email")
    username = sub or preferred or (email.split("@")[0] if email else None)
    return {
        "sub": sub,
        "username": username,
        "email": email,
        "roles": claims.get("roles", []),
        "device_id": claims.get("device_id"),
        "exp": claims.get("exp"),
        "iat": claims.get("iat"),
    }


@router.post("/logout")
async def logout(
    request: Request,
    response: Response,
    claims: Annotated[dict[str, Any], Depends(current_access)],
) -> dict[str, Any]:
    """Revoke the presenting token's session and its refresh family."""
    ctx = _get_ctx(request)
    jti = claims.get("jti")
    if not jti:
        raise HTTPException(status_code=401, detail="token has no session identifier")
    revoked = await auth_service.revoke_session(ctx, jti)
    if not revoked:
        raise HTTPException(status_code=404, detail="session not found")
    family = claims.get(auth_sessions.FAMILY_CLAIM)
    raw = request.cookies.get(REFRESH_COOKIE)
    if family:
        await auth_sessions.revoke_family(ctx, str(family))
    elif raw:
        await auth_sessions.revoke_family_by_token(ctx, raw)
    _clear_refresh_cookie(response, ctx)
    return {"revoked": True, "jti": jti}


@router.post("/logout-all")
async def logout_all(
    request: Request,
    response: Response,
    claims: Annotated[dict[str, Any], Depends(current_access)],
) -> dict[str, Any]:
    """Revoke every session and refresh family of the caller (a person only)."""
    ctx = _get_ctx(request)
    sub = claims.get("sub")
    if claims.get(auth_service.SSO_SOURCE_CLAIM) == "pat" or not isinstance(sub, str) or not sub:
        raise HTTPException(status_code=403, detail="A signed-in person is required.")
    await auth_sessions.revoke_all_for_sub(ctx, sub)
    _clear_refresh_cookie(response, ctx)
    return {"revoked": True}


# ─── Device pairing (AC-S04 / prompot §71) ────────────────────────────────────


@router.post("/pairing/initiate")
@router.post("/pairing")
async def initiate_pairing(
    body: PairingInitiateRequest,
    request: Request,
    claims: dict[str, Any] = Depends(current_access),
) -> dict[str, Any]:
    """Start device pairing for an authenticated session (prompot §71).

    Returns the challenge the new device must present to
    ``/api/auth/pairing/confirm``; the challenge expires in ≤ 5 minutes.
    """
    ctx = _get_ctx(request)
    return await auth_service.initiate_pairing(ctx, by_sub=claims.get("sub", ""))


@router.post("/pairing/confirm")
async def confirm_pairing(body: PairingConfirmRequest, request: Request) -> dict[str, Any]:
    """Confirm a pairing challenge and mint the device credential (§71)."""
    ctx = _get_ctx(request)
    try:
        return await auth_service.confirm_pairing(
            ctx,
            challenge_id=body.challenge_id,
            nonce=body.nonce,
            device_name=body.device_name,
            platform=body.platform,
            attestation=body.attestation,
            device_id=body.device_id,
        )
    except TokenError as exc:
        raise HTTPException(status_code=400, detail=exc.detail) from exc


# ─── Approval binding (AC-S05 / prompot §72) ──────────────────────────────────


@approvals_router.post("/bind")
async def bind_approval(
    body: ApprovalBindRequest,
    request: Request,
    claims: dict[str, Any] = Depends(current_access),
) -> dict[str, Any]:
    """Bind an approval to a device + nonce + an exact planned change (§72)."""
    ctx = _get_ctx(request)
    return await auth_service.create_approval(
        ctx,
        planned_change_id=body.planned_change_id,
        change=body.change,
        nonce=body.nonce,
        approver_sub=claims.get("sub", ""),
        device_id=body.device_id,
        rationale=body.rationale,
    )


@approvals_router.get("/{approval_id}/verify")
async def read_approval(
    approval_id: str, request: Request
) -> dict[str, Any]:
    """Read the bound approval (for UI display); does not re-check validity."""
    ctx = _get_ctx(request)
    approval = await auth_service._load_approval(ctx, approval_id)
    if approval is None:
        raise HTTPException(status_code=404, detail="approval not found")
    return auth_service._approval_view(approval)


@approvals_router.post("/{approval_id}/verify")
async def verify_approval(
    approval_id: str,
    body: ApprovalVerifyRequest,
    request: Request,
) -> dict[str, Any]:
    """Re-check an approval against the *current* change (§72).

    Invalidates the approval when the bound change has been modified, when the
    approval has expired, when the presenting device does not match, or when
    the approving device has been revoked.
    """
    ctx = _get_ctx(request)
    return await auth_service.verify_approval(
        ctx,
        approval_id=approval_id,
        change=body.change,
        device_id=body.device_id,
    )


@approvals_router.post("/{approval_id}/decide")
async def decide_approval(
    approval_id: str,
    body: ApprovalDecisionRequest,
    request: Request,
) -> dict[str, Any]:
    """Record a device verdict (``approved`` / ``denied``) on a bound approval."""
    ctx = _get_ctx(request)
    try:
        return await auth_service.decide_approval(
            ctx,
            approval_id=approval_id,
            decision=body.decision,
            change=body.change,
            device_id=body.device_id,
            nonce=body.nonce,
            rationale=body.rationale,
        )
    except TokenError as exc:
        raise HTTPException(status_code=400, detail=exc.detail) from exc
