"""Apple Health bridge endpoints (NS-ADR-100).

``POST /api/health/ingest`` is the only SSO-free route (exact ingress path).
It authenticates itself before reading the body: the paired device id comes in
the ``X-Nerve-Device`` header and its relay token as a Bearer credential. Any
failure is one generic 401, and nothing is parsed or stored before that check.
``GET /api/health/samples`` and ``/summary`` stay behind SSO.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Request, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import ValidationError

from backend.api.endpoints.relay import _plane, _require_relay_token
from backend.health import MAX_BODY_BYTES, HealthBatch, ingest_batch, list_samples, summarize

router = APIRouter(prefix="/api/health", tags=["health"])
_bearer = HTTPBearer(auto_error=False)


def _engine(request: Request) -> Any:
    engine = getattr(request.app.state, "db_engine", None)
    if engine is None:
        raise HTTPException(status_code=503, detail="durable storage is not configured")
    return engine


@router.post("/ingest")
async def health_ingest(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Security(_bearer)] = None,
) -> dict[str, int]:
    plane = _plane(request)
    device_id = request.headers.get("x-nerve-device", "")
    if plane is None or not plane.registry_configured:
        raise HTTPException(status_code=503, detail="durable storage is not configured")
    await _require_relay_token(plane, device_id, credentials)
    engine = _engine(request)
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="body too large")
    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="body too large")
    try:
        batch = HealthBatch.model_validate_json(body)
    except ValidationError:
        raise HTTPException(status_code=422, detail="invalid health batch") from None
    if batch.device_id != device_id:
        raise HTTPException(status_code=422, detail="invalid health batch")
    result = await ingest_batch(engine, batch)
    return result.as_dict()


@router.get("/samples")
async def health_samples(
    request: Request,
    kind: str | None = Query(default=None, max_length=32),
    days: int = Query(default=30, ge=1, le=365),
    limit: int = Query(default=500, ge=1, le=2000),
) -> dict[str, Any]:
    engine = getattr(request.app.state, "db_engine", None)
    if engine is None:
        return {"samples": [], "configured": False}
    return {"samples": await list_samples(engine, kind=kind, days=days, limit=limit),
            "configured": True}


@router.get("/summary")
async def health_summary(
    request: Request, days: int = Query(default=30, ge=1, le=365)
) -> dict[str, Any]:
    engine = getattr(request.app.state, "db_engine", None)
    if engine is None:
        return {"configured": False, "days": days, "last_sync_at": None,
                "total_samples": 0, "kinds": {}}
    return {"configured": True, **await summarize(engine, days=days)}
