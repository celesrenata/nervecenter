"""Mac Messages relay control-plane endpoints (spec ``personal-ops-recovery`` §5).

Contract (frozen route list):

- ``POST /api/relay/pair``            → issue a pairing code + device token
  (created ONCE, never in subsequent responses/logs)
- ``POST /api/relay/register``        → device authenticates with its token
  (sets ``auth_status=authenticated``; operational stays ``never_synced``)
- ``POST /api/relay/messages``        → durable batch ingest (idempotent by
  ``(device_id, message_id)``, dedup via the bounded ``relay_seq`` cursor)
- ``GET  /api/relay/health``          → per-device status + backlog counters
- ``POST /api/relay/import-request``  → initial history import (explicit
  ``consent: true`` required → 422 stable code otherwise)

The control plane lives on ``app.state.connection_control_plane``. When the
durable registry is absent — no durable storage configured — reads degrade to
**200 with an empty list** and writes to **202 with ``executed: false``**
(house degradation semantics, audit ``persistence-infra.md`` Area 4).

Hard rules
----------
- The pairing token is returned exactly once (the ``pair`` response). It is
  stored encrypted in the vault and never echoed by any other endpoint,
  response body, or log line.
- Message bodies are stored behind a stable, non-empty ``content_ref``; the
  durable rows carry only the ref (spec §7). A live mirror is re-fed into the
  in-memory messages service through
  :class:`~backend.messages.ingest_bridge.MessagesIngestBridge` — the bridge
  wraps that service without modifying it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from backend.api.models import ApiResponseMeta, ListResponse
from backend.connectors.registry import ConnectionControlPlane, ConnectionControlPlaneError
from backend.messages.ingest_bridge import MessagesIngestBridge

router = APIRouter(prefix="/api/relay", tags=["relay"])

# Ingest / import authenticate with the device's own pairing token (an opaque
# secret minted by ``/pair``, not a signed JWT) presented as a Bearer.
_bearer = HTTPBearer(auto_error=False)

# One generic 401 detail for every relay-auth failure mode (missing header,
# unknown device, wrong token) so the error body never leaks which check failed.
_REDACTED_RELAY = "relay request is not authorized"


# -- helpers ------------------------------------------------------------------


async def _require_relay_token(
    plane: ConnectionControlPlane,
    device_id: str,
    credentials: HTTPAuthorizationCredentials | None,
) -> None:
    """Constant-time bearer check against the exact device's stored relay token.

    Every failure mode (missing header, unknown device, token mismatch) raises
    the same generic 401 — the response never reveals which check failed.
    """
    token = credentials.credentials if credentials else ""
    if not await plane.relay_verify_token(device_id=device_id, token=token):
        raise HTTPException(status_code=401, detail=_REDACTED_RELAY)


def _meta(**pagination: int | None) -> ApiResponseMeta:
    return ApiResponseMeta(
        version="1.0.0",
        request_id=f"req-{uuid4().hex[:12]}",
        timestamp=datetime.now(UTC),
        pagination={k: v for k, v in pagination.items() if v is not None} or None,
    )


def _plane(request: Request) -> ConnectionControlPlane | None:
    return getattr(request.app.state, "connection_control_plane", None)


def _degraded_read() -> ListResponse:
    return ListResponse(data=[], meta=_meta())


def _degraded_write(request: Request, detail: str) -> dict[str, Any]:
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
    if code == "relay_token_invalid":
        return HTTPException(status_code=401, detail={"error_code": code, "message": exc.message})
    if code in ("bad_request", "consent_required", "kind_not_supported"):
        return HTTPException(status_code=422, detail={"error_code": code, "message": exc.message})
    if code == "vault_unavailable":
        return HTTPException(status_code=503, detail={"error_code": code, "message": exc.message})
    return HTTPException(status_code=500, detail={"error_code": code, "message": exc.message})


# -- request models -----------------------------------------------------------


class PairRequest(BaseModel):
    """Pairing request (body optional)."""

    device_name: str | None = None


class RegisterRequest(BaseModel):
    """Register a relay device with its pairing token (token is never echoed)."""

    device_id: str
    token: str = Field(min_length=1, description="Pairing token from POST /api/relay/pair")


class RelayMessage(BaseModel):
    """One relay message (body lives behind the durable content_ref)."""

    message_id: str
    channel: str = "imessage"
    direction: str = "inbound"
    peer_identifier: str = ""
    peer_name: str | None = None
    body: str = ""
    received_at: str | None = None
    source: str = "personal-relay"


class MessagesRequest(BaseModel):
    """Durable batch ingest."""

    device_id: str
    messages: list[RelayMessage] = Field(default_factory=list)


class ImportRequest(BaseModel):
    """Initial history import — explicit consent is mandatory (spec §5)."""

    device_id: str
    consent: bool = Field(default=False, description="Must be true; history import requires explicit consent")


# -- routes ---------------------------------------------------------------------


@router.post("/pair")
async def relay_pair(request: Request, body: PairRequest | None = None) -> dict[str, Any]:
    """Mint a pairing code + device token (returned ONCE).

    The token is stored encrypted in the vault and appears in no other
    response, log line, or error message.
    """
    plane = _plane(request)
    if plane is None or not plane.registry_configured:
        return _degraded_write(request, "durable storage is not configured")
    try:
        return await plane.relay_pair(device_name=body.device_name if body else None)
    except ConnectionControlPlaneError as exc:
        raise _http_error(exc) from exc


@router.post("/register")
async def relay_register(request: Request, body: RegisterRequest) -> dict[str, Any]:
    """Authenticate a relay device with its pairing token.

    On success ``auth_status=authenticated``; ``operational_status``
    intentionally stays ``never_synced`` until a real ingest marks it healthy.
    """
    plane = _plane(request)
    if plane is None or not plane.registry_configured:
        return _degraded_write(request, "durable storage is not configured")
    try:
        return await plane.relay_register(device_id=body.device_id, token=body.token)
    except ConnectionControlPlaneError as exc:
        raise _http_error(exc) from exc


@router.post("/messages")
async def relay_messages(
    request: Request,
    body: MessagesRequest,
    credentials: HTTPAuthorizationCredentials | None = Security(_bearer),
) -> dict[str, Any]:
    """Durable batch ingest with ``(device_id, message_id)`` dedup.

    Requires the device's pairing token as a Bearer credential; a missing or
    wrong token is a generic 401 before anything durable is touched. Each new
    message is persisted (person → communication → conversation), the
    connection is marked healthy, and the message is mirrored into the
    in-memory messages service for live reads. Re-sending an already-seen
    ``message_id`` is a no-op (``skipped``).
    """
    plane = _plane(request)
    if plane is None or not plane.registry_configured:
        return _degraded_write(request, "durable storage is not configured")
    await _require_relay_token(plane, body.device_id, credentials)
    try:
        result = await plane.relay_ingest_batch(
            device_id=body.device_id,
            messages=[m.model_dump() for m in body.messages],
        )
    except ConnectionControlPlaneError as exc:
        raise _http_error(exc) from exc
    # Live mirror (does not affect the durable result; the bridge dedups by
    # message_id, so replayed messages are harmless here).
    bridge = MessagesIngestBridge()
    mirrored = 0
    for message in body.messages:
        outcome = bridge.mirror_message(message.model_dump())
        if outcome is not None and not outcome.get("duplicate"):
            mirrored += 1
    return {**result, "mirrored": mirrored}


@router.get("/health")
async def relay_health(request: Request) -> ListResponse:
    """Per relay device: auth/operational status, last seen, backlog count."""
    plane = _plane(request)
    if plane is None or not plane.registry_configured:
        return _degraded_read()
    items = await plane.relay_health()
    return ListResponse(data=items, meta=_meta(limit=len(items)))


@router.post("/import-request")
async def relay_import_request(
    request: Request,
    body: ImportRequest,
    credentials: HTTPAuthorizationCredentials | None = Security(_bearer),
) -> dict[str, Any]:
    """Request the initial history import.

    Requires the device's pairing token as a Bearer credential. Without
    explicit ``consent: true`` this returns **422** with the stable code
    ``consent_required`` (history import requires explicit user consent per the
    spec freeze).
    """
    plane = _plane(request)
    if plane is None or not plane.registry_configured:
        return _degraded_write(request, "durable storage is not configured")
    await _require_relay_token(plane, body.device_id, credentials)
    try:
        return await plane.relay_import_request(device_id=body.device_id, consent=body.consent)
    except ConnectionControlPlaneError as exc:
        raise _http_error(exc) from exc
