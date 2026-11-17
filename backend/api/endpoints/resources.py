"""Owner API for resources and agent links (NS-ADR-125).

Signed-in people only (never devices), like ``/api/infra``. Uploads are a raw-body
``PUT`` (no multipart dependency). Secret resources can be downloaded or deleted by the
owner but never linked to an agent.
"""

from __future__ import annotations

import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from backend.api.auth import current_access
from backend.api.endpoints.mail_security import _eligible_for_server_scopes
from backend.resources.service import (
    MAX_BYTES,
    ResourceInputError,
    ResourceService,
    ResourceStoreUnavailable,
    ResourceTooLarge,
    get_service,
)

router = APIRouter(tags=["resources"])
Claims = Annotated[dict[str, Any], Depends(current_access)]
MAX_LINK_BODY = 16 * 1024


def _actor(claims: dict[str, Any]) -> str:
    if not _eligible_for_server_scopes(claims):
        raise HTTPException(status_code=403, detail="A signed-in person is required.")
    return str(claims.get("preferred_username") or claims.get("email") or claims.get("sub")
               or "sso-user")


def _svc(request: Request) -> ResourceService:
    svc = get_service(request.app)
    if svc is None:
        raise HTTPException(status_code=503, detail="durable storage is not configured")
    return svc


def _bad(exc: Exception) -> HTTPException:
    if isinstance(exc, KeyError):
        return HTTPException(status_code=404, detail="Not found.")
    if isinstance(exc, ResourceStoreUnavailable):
        return HTTPException(status_code=503, detail="The object store is not configured.")
    if isinstance(exc, ResourceTooLarge):
        return HTTPException(status_code=413, detail=str(exc))
    return HTTPException(status_code=422, detail=str(exc))


_EXPECTED = (KeyError, ResourceInputError, ResourceStoreUnavailable)


async def _raw_body(request: Request, limit: int) -> bytes:
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        raise HTTPException(status_code=413, detail="File is larger than 20 MB.")
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > limit:
            raise HTTPException(status_code=413, detail="File is larger than 20 MB.")
        chunks.append(chunk)
    return b"".join(chunks)


@router.get("/api/resources")
async def list_resources(request: Request, claims: Claims) -> dict[str, Any]:
    _actor(claims)
    return {"resources": await _svc(request).list_all()}


@router.put("/api/resources", status_code=201)
async def upload_resource(
    request: Request, claims: Claims, name: str, sensitivity: str = "normal"
) -> dict[str, Any]:
    actor = _actor(claims)
    svc = _svc(request)
    data = await _raw_body(request, MAX_BYTES)
    try:
        return await svc.put(name, data, sensitivity=sensitivity, uploaded_by=actor)
    except _EXPECTED as exc:
        raise _bad(exc) from None


@router.get("/api/resources/{resource_id}/download")
async def download_resource(request: Request, resource_id: str, claims: Claims) -> Response:
    _actor(claims)
    try:
        view, data = await _svc(request).download(resource_id)
    except _EXPECTED as exc:
        raise _bad(exc) from None
    filename = view["name"].replace('"', "")
    return Response(content=data, media_type="application/octet-stream", headers={
        "Content-Disposition": f'attachment; filename="{filename}"',
        "X-Content-Type-Options": "nosniff",
    })


@router.delete("/api/resources/{resource_id}", status_code=204)
async def delete_resource(request: Request, resource_id: str, claims: Claims) -> Response:
    _actor(claims)
    try:
        await _svc(request).delete(resource_id)
    except _EXPECTED as exc:
        raise _bad(exc) from None
    return Response(status_code=204)


@router.get("/api/agents/{agent_id}/resources")
async def get_agent_resources(request: Request, agent_id: str, claims: Claims
                              ) -> dict[str, Any]:
    _actor(claims)
    return {"resources": await _svc(request).get_links(agent_id)}


@router.put("/api/agents/{agent_id}/resources")
async def set_agent_resources(request: Request, agent_id: str, claims: Claims
                              ) -> dict[str, Any]:
    _actor(claims)
    svc = _svc(request)
    raw = await _raw_body(request, MAX_LINK_BODY)
    try:
        body = json.loads(raw or b"{}")
    except ValueError:
        raise HTTPException(status_code=422, detail="Send a JSON object.") from None
    ids = body.get("resource_ids") if isinstance(body, dict) else None
    if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
        raise HTTPException(status_code=422, detail="resource_ids must be a list of strings")
    try:
        return {"resources": await svc.set_links(agent_id, ids)}
    except _EXPECTED as exc:
        raise _bad(exc) from None
