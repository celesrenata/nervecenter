"""Signed-JWT auth endpoints for the NerveCenter API (spec sso-integration, S101).

This module is the HTTP layer over :mod:`backend.api.auth_service`.  It exposes:

- ``GET  /api/auth/sso/config`` — AC-S01: the SSO/OIDC discovery document.
- ``POST /api/auth/login``      — AC-S03: break-glass local-admin login
  (no default password; disabled unless ``NERVE_LOCAL_ADMIN`` is enabled).
- ``GET  /api/auth/me``         — introspect the presenting access token.
- ``POST /api/auth/logout``     — revoke the presenting token's session.
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
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from backend.api import auth_service
from backend.api.auth_service import (
    TYP_ACCESS,
    TokenError,
)

router = APIRouter(prefix="/api/auth", tags=["authentication"])
approvals_router = APIRouter(prefix="/api/approvals", tags=["approvals"])

bearer = HTTPBearer(auto_error=False)


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
    try:
        return await ctx.verify_token(credentials.credentials, expected_typ=TYP_ACCESS)
    except TokenError as exc:
        raise HTTPException(status_code=401, detail=exc.detail) from exc


def _constant_time_equal(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


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
    body: SsoTokenRequest, request: Request
) -> dict[str, Any]:
    """Server-side PKCE authorization-code → token exchange.

    The ``nervecenter`` OIDC client is **confidential** (its token endpoint
    requires the client secret, which never reaches the browser), so the
    code→token exchange cannot run client-side.  This endpoint performs it
    server-side: it POSTs the captured ``code`` + ``code_verifier`` (+ the exact
    ``redirect_uri`` from the authorization request) to the issuer's token
    endpoint with the client secret (Basic auth) and returns the token response.

    The returned ``access_token`` is a standard Keycloak RS256 access token that
    :func:`current_access` verifies statelessly against the issuer JWKS.  The
    issuer's raw error body is never echoed to the caller.

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
    return {
        "access_token": tokens.get("access_token", ""),
        "token_type": tokens.get("token_type", "Bearer"),
        "refresh_token": tokens.get("refresh_token"),
        "expires_in": tokens.get("expires_in"),
    }


# ─── Break-glass local admin (AC-S03) ─────────────────────────────────────────


@router.post("/login")
async def login(body: LoginRequest, request: Request) -> dict[str, Any]:
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
    access_token, access_exp = ctx.issue_access_token(sub, roles)
    refresh_token, refresh_exp = ctx.issue_refresh_token(sub, roles=roles)
    jti = auth_service._jti_of(access_token, ctx)
    await auth_service.record_session(
        ctx,
        jti=jti,
        sub=sub,
        typ=TYP_ACCESS,
        expires_at=access_exp,
        source="local-admin",
    )
    return {
        "token_type": "Bearer",
        "access_token": access_token,
        "expires_at": access_exp.isoformat(),
        "refresh_token": refresh_token,
        "refresh_expires_at": refresh_exp.isoformat(),
        "sub": sub,
        "roles": roles,
    }


@router.get("/me")
async def me(claims: dict[str, Any] = Depends(current_access)) -> dict[str, Any]:
    """Introspect the presenting access token."""
    return {
        "sub": claims.get("sub"),
        "roles": claims.get("roles", []),
        "device_id": claims.get("device_id"),
        "exp": claims.get("exp"),
        "iat": claims.get("iat"),
    }


@router.post("/logout")
async def logout(
    request: Request, claims: dict[str, Any] = Depends(current_access)
) -> dict[str, Any]:
    """Revoke the presenting token's session."""
    ctx = _get_ctx(request)
    jti = claims.get("jti")
    if not jti:
        raise HTTPException(status_code=401, detail="token has no session identifier")
    revoked = await auth_service.revoke_session(ctx, jti)
    if not revoked:
        raise HTTPException(status_code=404, detail="session not found")
    return {"revoked": True, "jti": jti}


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
