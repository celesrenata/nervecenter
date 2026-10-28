"""Connection Center endpoints (spec ``personal-ops-recovery`` §3).

Contract (frozen route list):

- ``GET    /api/connections``                       → list (200, empty when unwired)
- ``GET    /api/connections/``                       → list (200, empty when unwired)
- ``POST   /api/connections``                        → create (201)
- ``GET    /api/connections/{connection_id}``         → one connection (404)
- ``PATCH  /api/connections/{connection_id}``         → update (200)
- ``DELETE /api/connections/{connection_id}``         → remove + purge secrets (204)
- ``POST   /api/connections/{connection_id}/auth``    → start OAuth/pairing flow (200)
- ``POST   /api/connections/{connection_id}/verify``  → run login probe (200)
- ``POST   /api/connections/{connection_id}/sync``     → trigger sync (200)
- ``POST   /api/connections/{connection_id}/import``   → initial-history import (200)
- ``POST   /api/connections/{connection_id}/reconnect``→ re-verify + re-sync (200)
- ``GET    /api/connections/{connection_id}/diagnostics`` → actionable fix (200)
- ``GET    /api/connections/{connection_id}/permissions`` → editable surface (200)
- ``PATCH  /api/connections/{connection_id}/permissions``→ allow-listed config (200)

The control plane lives on ``app.state.connection_control_plane`` (bound at
startup by :mod:`backend.api.app` when the durable registry is present). When
the registry is absent — no durable storage configured — reads degrade to
**200 with an empty list** and writes to **202 with ``executed: false``**,
matching the house degradation semantics (audit ``persistence-infra.md``
Area 4): the API stays bootable without dependencies.

Hard rule: no plaintext credential ever appears in a response body — the
control plane strips credential-like config keys and stores secrets only in
the encrypted vault.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from backend.api.auth import current_access
from backend.api.models import ApiResponseMeta, ListResponse
from backend.connectors.registry import (
    ConnectionControlPlane,
    ConnectionControlPlaneError,
    CONNECTION_KINDS,
)

router = APIRouter(prefix="/api/connections", tags=["connections"])


# -- helpers ------------------------------------------------------------------


def _meta(**pagination: int | None) -> ApiResponseMeta:
    return ApiResponseMeta(
        version="1.0.0",
        request_id=f"req-{uuid4().hex[:12]}",
        timestamp=datetime.now(UTC),
        pagination={k: v for k, v in pagination.items() if v is not None} or None,
    )


def _plane(request: Request) -> ConnectionControlPlane | None:
    """Resolve the control plane; ``None`` when the durable registry is absent."""
    return getattr(request.app.state, "connection_control_plane", None)


def _degraded_read() -> ListResponse:
    """Honest empty-list degradation (Area 4 read contract)."""
    return ListResponse(data=[], meta=_meta())


def _degraded_write(request: Request, detail: str) -> dict[str, Any]:
    """Honest 202 / executed:false degradation (Area 4 write contract)."""
    return {
        "accepted": True,
        "executed": False,
        "request_id": f"req-{uuid4().hex[:12]}",
        "timestamp": datetime.now(UTC).isoformat(),
        "detail": detail,
    }


def _http_error(exc: ConnectionControlPlaneError) -> HTTPException:
    code = exc.code
    if code == "connection_not_found":
        return HTTPException(status_code=404, detail={"error_code": code, "message": exc.message})
    if code in ("bad_request", "kind_not_supported", "consent_required"):
        return HTTPException(status_code=422, detail={"error_code": code, "message": exc.message})
    if code == "vault_unavailable":
        return HTTPException(status_code=503, detail={"error_code": code, "message": exc.message})
    return HTTPException(status_code=500, detail={"error_code": code, "message": exc.message})


# -- request models -----------------------------------------------------------


class ConnectionCreateRequest(BaseModel):
    """Create a connection. ``config`` must be non-secret (passwords/tokens are
    stored via the vault, not here)."""

    kind: str = Field(description=f"one of {list(CONNECTION_KINDS)}")
    label: str | None = None
    config: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True


class ConnectionUpdateRequest(BaseModel):
    """Partial update: any subset of label / config / enabled."""

    label: str | None = None
    config: dict[str, Any] | None = None
    enabled: bool | None = None


class PermissionPatchRequest(BaseModel):
    """Allow-listed config keys only (secrets are denied)."""

    config: dict[str, Any] = Field(default_factory=dict)


class AuthFlowRequest(BaseModel):
    """Start an OAuth / pairing flow (body optional)."""

    code: str | None = None
    state: str | None = None


# -- list / get ---------------------------------------------------------------


@router.get("", response_model=ListResponse)
@router.get("/", response_model=ListResponse)
async def list_connections(
    request: Request,
    claims: dict[str, Any] = Depends(current_access),
    enabled_only: bool = False,
    limit: int | None = None,
    offset: int = 0,
) -> ListResponse:
    """List connections with derived ``state`` (both axes always present).

    When the durable registry is absent this returns 200 with an empty list —
    the honest house read degradation, never a 5xx.
    """
    plane = _plane(request)
    if plane is None or not plane.registry_configured:
        return _degraded_read()
    items = await plane.list_connections(enabled_only=enabled_only)
    if offset:
        items = items[offset:]
    if limit is not None:
        items = items[: max(limit, 0)]
    return ListResponse(
        data=items,
        meta=_meta(limit=len(items), offset=offset),
    )


@router.get("/{connection_id}")
async def get_connection(
    request: Request,
    connection_id: str,
    claims: dict[str, Any] = Depends(current_access),
) -> dict[str, Any]:
    """Fetch one connection (404 when absent)."""
    plane = _plane(request)
    if plane is None or not plane.registry_configured:
        # House read degradation: unknown id → synthetic 200 representation.
        return {"id": connection_id, "resource": "connection"}
    row = await plane.get_connection(connection_id)
    if row is None:
        raise HTTPException(status_code=404, detail={"error_code": "connection_not_found"})
    return row


# -- create / update / delete --------------------------------------------------


@router.post("", status_code=201)
async def create_connection(
    request: Request,
    body: ConnectionCreateRequest,
    claims: dict[str, Any] = Depends(current_access),
) -> dict[str, Any]:
    """Create a connection (201)."""
    plane = _plane(request)
    if plane is None or not plane.registry_configured:
        return _degraded_write(request, "durable storage is not configured")
    try:
        row = await plane.create_connection(
            kind=body.kind,
            label=body.label,
            config=body.config,
            enabled=body.enabled,
        )
    except ConnectionControlPlaneError as exc:
        raise _http_error(exc) from exc
    return await plane.get_connection(row.id)


@router.patch("/{connection_id}")
async def update_connection(
    request: Request,
    connection_id: str,
    body: ConnectionUpdateRequest,
    claims: dict[str, Any] = Depends(current_access),
) -> dict[str, Any]:
    """Update label / config / enabled (200)."""
    plane = _plane(request)
    if plane is None or not plane.registry_configured:
        return _degraded_write(request, "durable storage is not configured")
    try:
        return await plane.update_connection(
            connection_id,
            label=body.label,
            config=body.config,
            enabled=body.enabled,
        )
    except ConnectionControlPlaneError as exc:
        raise _http_error(exc) from exc


@router.delete("/{connection_id}", status_code=204)
async def delete_connection(
    request: Request,
    connection_id: str,
    claims: dict[str, Any] = Depends(current_access),
) -> None:
    """Remove the connection and purge every secret it owned (204)."""
    plane = _plane(request)
    if plane is None or not plane.registry_configured:
        return None
    try:
        await plane.delete_connection(connection_id)
    except ConnectionControlPlaneError as exc:
        raise _http_error(exc) from exc
    return None


# -- auth / verify / sync / import / reconnect ----------------------------------


@router.post("/{connection_id}/auth")
async def start_auth_flow(
    request: Request,
    connection_id: str,
    body: AuthFlowRequest | None = None,
    claims: dict[str, Any] = Depends(current_access),
) -> dict[str, Any]:
    """Start the OAuth / pairing flow for a connection.

    - ``gmail`` / ``graph_mail`` → ``{authorization_url, state, provider}``.
    - ``relay_device`` → mint a pairing token (returned once).
    - when a ``code`` is supplied, complete the OAuth exchange and store the
      token via the vault.
    """
    plane = _plane(request)
    if plane is None or not plane.registry_configured:
        return _degraded_write(request, "durable storage is not configured")
    try:
        row = await plane.get_connection(connection_id)
        if row is None:
            raise HTTPException(
                status_code=404, detail={"error_code": "connection_not_found"}
            )
        if body is not None and body.code:
            return await plane.complete_oauth(
                connection_id, code=body.code, state=body.state or ""
            )
        if row["kind"] == "relay_device":
            # Pairing is provisioned through POST /api/relay/pair, which mints
            # the token once. This route only points there — it never re-mints
            # or re-returns a pairing token.
            return {
                "ok": True,
                "detail": "relay devices are paired via POST /api/relay/pair",
                "pair_endpoint": "/api/relay/pair",
                "auth_status": row["auth_status"],
            }
        if row["kind"] in ("gmail", "graph_mail"):
            return await plane.start_oauth(connection_id)
        raise ConnectionControlPlaneError(
            "bad_request", f"kind {row['kind']!r} has no auth flow"
        )
    except ConnectionControlPlaneError as exc:
        raise _http_error(exc) from exc


@router.post("/{connection_id}/verify")
async def verify_connection(
    request: Request,
    connection_id: str,
    claims: dict[str, Any] = Depends(current_access),
) -> dict[str, Any]:
    """Run the provider login probe.

    A successful probe sets ``auth_status=authenticated`` but **never**
    advances ``operational_status`` past ``never_synced`` (frozen hard rule).
    """
    plane = _plane(request)
    if plane is None or not plane.registry_configured:
        return _degraded_write(request, "durable storage is not configured")
    try:
        return await plane.verify(connection_id)
    except ConnectionControlPlaneError as exc:
        raise _http_error(exc) from exc


@router.post("/{connection_id}/sync")
async def sync_connection(
    request: Request,
    connection_id: str,
    claims: dict[str, Any] = Depends(current_access),
) -> dict[str, Any]:
    """Trigger an incremental sync (delegates to the registered ingest trigger)."""
    plane = _plane(request)
    if plane is None or not plane.registry_configured:
        return _degraded_write(request, "durable storage is not configured")
    try:
        return await plane.sync_now(connection_id)
    except ConnectionControlPlaneError as exc:
        raise _http_error(exc) from exc


@router.post("/{connection_id}/import")
async def import_connection(
    request: Request,
    connection_id: str,
    claims: dict[str, Any] = Depends(current_access),
) -> dict[str, Any]:
    """Trigger an initial-history import (recorded in ``import_jobs``)."""
    plane = _plane(request)
    if plane is None or not plane.registry_configured:
        return _degraded_write(request, "durable storage is not configured")
    try:
        return await plane.import_now(connection_id)
    except ConnectionControlPlaneError as exc:
        raise _http_error(exc) from exc


@router.post("/{connection_id}/reconnect")
async def reconnect_connection(
    request: Request,
    connection_id: str,
    claims: dict[str, Any] = Depends(current_access),
) -> dict[str, Any]:
    """Re-verify the login, then re-sync."""
    plane = _plane(request)
    if plane is None or not plane.registry_configured:
        return _degraded_write(request, "durable storage is not configured")
    try:
        return await plane.reconnect(connection_id)
    except ConnectionControlPlaneError as exc:
        raise _http_error(exc) from exc


# -- diagnostics / permissions ----------------------------------------------------


@router.get("/{connection_id}/diagnostics")
async def diagnostics_connection(
    request: Request,
    connection_id: str,
    claims: dict[str, Any] = Depends(current_access),
) -> dict[str, Any] | ListResponse:
    """Actionable plain-language fix for the last error code + job state."""
    plane = _plane(request)
    if plane is None or not plane.registry_configured:
        return _degraded_read()
    try:
        return await plane.diagnostics(connection_id)
    except ConnectionControlPlaneError as exc:
        raise _http_error(exc) from exc


@router.get("/{connection_id}/permissions")
async def get_permissions(
    request: Request,
    connection_id: str,
    claims: dict[str, Any] = Depends(current_access),
) -> dict[str, Any] | ListResponse:
    """The effective permission/config surface (editable vs denied keys)."""
    plane = _plane(request)
    if plane is None or not plane.registry_configured:
        return _degraded_read()
    try:
        row = await plane.get_connection(connection_id)
        if row is None:
            raise HTTPException(status_code=404, detail={"error_code": "connection_not_found"})
        return plane.permissions_for(row["kind"])
    except ConnectionControlPlaneError as exc:
        raise _http_error(exc) from exc


@router.patch("/{connection_id}/permissions")
async def patch_permissions(
    request: Request,
    connection_id: str,
    body: PermissionPatchRequest,
    claims: dict[str, Any] = Depends(current_access),
) -> dict[str, Any]:
    """Apply allow-listed config keys only (secrets are denied)."""
    plane = _plane(request)
    if plane is None or not plane.registry_configured:
        return _degraded_write(request, "durable storage is not configured")
    try:
        return await plane.patch_permissions(connection_id, body.config)
    except ConnectionControlPlaneError as exc:
        raise _http_error(exc) from exc
