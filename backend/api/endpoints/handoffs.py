"""Coding handoff endpoints (NS-ADR-106).

Owner/agent routes (behind SSO): ``/api/handoffs*``.
Mac worker routes: ``POST /api/handoffs/device/claim`` and
``POST /api/handoffs/device/result``. They authenticate with the paired relay
device token (``X-Nerve-Device`` + Bearer, one generic 401, nothing read
before the check) and cap the body at 64 KiB. They are the only two SSO-free
handoff paths (Ingress ``nervcenter-handoff`` in relay-ingress.yaml, NS-ADR-109;
owner approved, https on the public host).
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Request, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field, ValidationError

from backend.api.endpoints.relay import _plane, _require_relay_token
from backend.handoffs import get_service
from backend.handoffs.central import central_status
from backend.handoffs.service import (
    HandoffInputError,
    HandoffNotFoundError,
    HandoffStateError,
)

router = APIRouter(prefix="/api/handoffs", tags=["handoffs"])
_bearer = HTTPBearer(auto_error=False)
MAX_DEVICE_BODY = 64 * 1024


class HandoffIn(BaseModel):
    model_config = {"extra": "forbid"}
    target: str = Field(pattern="^(opencode|menagerie)$")
    title: str = Field(min_length=1, max_length=200)
    brief: str = Field(min_length=1, max_length=20_000)
    repo: str | None = Field(default=None, max_length=200)
    project_slug: str | None = Field(default=None, max_length=64)
    capabilities: list[str] = Field(default_factory=list, max_length=10)
    runner: str | None = Field(default=None, pattern="^(central|mac)$")
    requested_by: str = Field(default="owner", pattern="^(owner|agent(:[a-z0-9_-]{1,40})?)$")


class DeviceResult(BaseModel):
    model_config = {"extra": "forbid"}
    handoff_id: str = Field(min_length=1, max_length=64)
    ok: bool
    summary: str | None = Field(default=None, max_length=8000)
    error: str | None = Field(default=None, max_length=2000)


def _service(request: Request):
    service = get_service(request.app)
    if service is None:
        raise HTTPException(status_code=503, detail="durable storage is not configured")
    return service


async def _guard(coro: Any) -> Any:
    try:
        return await coro
    except HandoffNotFoundError:
        raise HTTPException(status_code=404, detail="Handoff not found") from None
    except HandoffStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except HandoffInputError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


@router.get("/status")
async def handoff_status(request: Request) -> dict[str, Any]:
    executor = getattr(request.app.state, "handoff_executor", None)
    return {
        "central": {
            **central_status(),
            "running": executor is not None,
            "busy": bool(executor and executor.busy),
        },
        "mac": {"pull_path_enabled": True, "note": "Mac agent polls over https (NS-ADR-109)"},
    }


@router.post("", status_code=201)
async def enqueue(body: HandoffIn, request: Request) -> dict[str, Any]:
    return await _guard(
        _service(request).enqueue(
            target=body.target,
            title=body.title,
            brief=body.brief,
            repo=body.repo,
            project_slug=body.project_slug,
            capabilities=body.capabilities,
            runner=body.runner,
            requested_by=body.requested_by,
        )
    )


@router.get("")
async def list_handoffs(request: Request, status: str | None = None) -> dict[str, Any]:
    return {"handoffs": await _service(request).list(status)}


async def _device(request: Request, credentials: HTTPAuthorizationCredentials | None) -> str:
    plane = _plane(request)
    device_id = request.headers.get("x-nerve-device", "")
    if plane is None or not plane.registry_configured:
        raise HTTPException(status_code=503, detail="durable storage is not configured")
    await _require_relay_token(plane, device_id, credentials)
    return f"mac:{device_id}"


async def _capped_body(request: Request) -> bytes:
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_DEVICE_BODY:
        raise HTTPException(status_code=413, detail="body too large")
    body = await request.body()
    if len(body) > MAX_DEVICE_BODY:
        raise HTTPException(status_code=413, detail="body too large")
    return body


@router.post("/device/claim")
async def device_claim(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Security(_bearer)] = None,
) -> dict[str, Any]:
    claimer = await _device(request, credentials)
    await _capped_body(request)
    job = await _service(request).claim_next("mac", claimer)
    if job is None:
        return {"handoff": None}
    keep = ("id", "target", "title", "brief", "repo", "project_slug", "push_approved")
    return {"handoff": {k: job[k] for k in keep}}


@router.post("/device/result")
async def device_result(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Security(_bearer)] = None,
) -> dict[str, Any]:
    claimer = await _device(request, credentials)
    raw = await _capped_body(request)
    try:
        body = DeviceResult.model_validate_json(raw)
    except ValidationError:
        raise HTTPException(status_code=422, detail="invalid result") from None
    row = await _guard(
        _service(request).finish(
            body.handoff_id, ok=body.ok, summary=body.summary, error=body.error, claimer=claimer
        )
    )
    return {"id": row["id"], "status": row["status"]}


@router.get("/{handoff_id}")
async def detail(handoff_id: str, request: Request) -> dict[str, Any]:
    return await _guard(_service(request).get(handoff_id))


@router.post("/{handoff_id}/approve-push")
async def approve_push(handoff_id: str, request: Request) -> dict[str, Any]:
    return await _guard(_service(request).approve_push(handoff_id))


@router.post("/{handoff_id}/cancel")
async def cancel(handoff_id: str, request: Request) -> dict[str, Any]:
    return await _guard(_service(request).cancel(handoff_id))


@router.post("/{handoff_id}/retry")
async def retry(handoff_id: str, request: Request) -> dict[str, Any]:
    return await _guard(_service(request).retry(handoff_id))
