"""Contact thumbnails (NS-ADR-097), behind the normal SSO like every route.

- ``GET /api/contact-photos/{person_id}`` -> the person's thumbnail
- ``GET /api/contact-photos?handle=+15551234567`` -> by phone/email handle

404 when there is no photo. The image comes from the encrypted object store;
responses are private and cacheable for an hour.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request, Response

from backend.contacts.durable import photo_for

router = APIRouter(prefix="/api/contact-photos", tags=["contacts"])

_CACHE = "private, max-age=3600"


async def _respond(request: Request, *, person_id: str | None = None,
                   handle: str | None = None) -> Response:
    from backend.ingest.supervisor import _object_store_from_env

    found = await photo_for(
        getattr(request.app.state, "repository_registry", None),
        _object_store_from_env(),
        person_id=person_id,
        handle=handle,
    )
    if found is None:
        raise HTTPException(status_code=404, detail="no photo")
    data, ptype = found
    return Response(content=data, media_type=ptype,
                    headers={"Cache-Control": _CACHE, "X-Content-Type-Options": "nosniff"})


@router.get("")
async def photo_by_handle(
    request: Request, handle: str = Query(min_length=1, max_length=320)
) -> Response:
    return await _respond(request, handle=handle)


@router.get("/{person_id}")
async def photo_by_person(request: Request, person_id: str) -> Response:
    return await _respond(request, person_id=person_id)
