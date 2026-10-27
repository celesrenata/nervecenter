"""Relationship endpoints wired to the real RelationshipService.

Contract (spec §46 + Section 13 Temporal Relationships):
- ``GET  /api/relationships``            → list active relationships
- ``POST /api/relationships``            → create a new relationship (201)
- ``GET  /api/relationships/{rel_id}``   → get one relationship
- ``DELETE /api/relationships/{rel_id}`` → delete (204)

When the service is not configured (local dev without NERVE_DB_URL) the
list endpoint returns an empty collection, get-by-id returns 503, and all
write endpoints return 503. Honest degradation, no fabricated records,
no fake 202 acceptance.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import ValidationError

from backend.api.models import (
    ApiResponseMeta,
    ListResponse,
    RelationshipCreateRequest,
)
from backend.knowledge.relationship import UnknownEntityError
from backend.persistence.repositories.registry import RepositoryRegistry

router = APIRouter(prefix="/api/relationships", tags=["relationships"])


def _relationships_service(request: Request):
    """The durable People & Relationships service (§11, workstream J),
    lazily built from the durable repository registry and cached on
    ``app.state``.

    The relationships plane is durable-backed (workstream-F repositories);
    without ``NERVE_DB_URL`` there is no registry and the routes answer an
    honest ``503 relationships_unavailable`` — never fabricated data.
    """
    cached = getattr(request.app.state, "relationships_service", None)
    if cached is not None:
        return cached
    registry = getattr(request.app.state, "repository_registry", None)
    if not isinstance(registry, RepositoryRegistry):
        raise HTTPException(
            status_code=503,
            detail="relationships_unavailable: the durable database is not configured",
        )
    from backend.communications.relationships_service import RelationshipsService

    from backend.communications import get_communications_service

    svc = RelationshipsService(
        communications=registry.communications,
        commitments=registry.commitments,
        calendar=registry.calendar,
        session_factory=registry.session_factory,
        in_memory=get_communications_service(),
    )
    request.app.state.relationships_service = svc
    return svc


def _meta(**pagination: int | None) -> ApiResponseMeta:
    return ApiResponseMeta(
        version="1.0.0",
        request_id=f"req-{uuid4().hex[:12]}",
        timestamp=datetime.now(UTC),
        pagination={k: v for k, v in pagination.items() if v is not None} or None,
    )


def _service(request: Request):
    services = getattr(request.app.state, "services", {})
    service = services.get("relationships") if isinstance(services, dict) else None
    if service is None:
        raise HTTPException(
            status_code=503,
            detail="Relationships service not configured (NERVE_DB_URL unset).",
        )
    return service


@router.get("", response_model=ListResponse)
@router.get("/", response_model=ListResponse)
async def list_relationships(
    request: Request,
    limit: int = Query(default=100, ge=1),
    offset: int = Query(default=0, ge=0),
) -> ListResponse:
    """List relationships active at the current instant."""
    services = getattr(request.app.state, "services", {})
    service = services.get("relationships") if isinstance(services, dict) else None
    if service is None:
        return ListResponse(
            data=[],
            meta=_meta(total=0, page=1, per_page=limit),
        )
    rows = await service.active_snapshot()
    data = [row.model_dump(mode="json") for row in rows[:limit][offset : offset + limit]]
    return ListResponse(
        data=data,
        meta=_meta(total=len(rows), page=offset // max(limit, 1) + 1, per_page=limit),
    )


@router.post("", status_code=201)
async def create_relationship(
    request: Request, payload: RelationshipCreateRequest
) -> dict[str, Any]:
    """Create a relationship between two existing entities."""
    service = _service(request)
    try:
        rel = await service.create(
            source_id=payload.source_entity_id,
            target_id=payload.target_entity_id,
            relation_type=payload.relation_type,
            properties=payload.properties,
            valid_from=payload.valid_from,
            valid_to=payload.valid_to,
            confidence=payload.confidence,
            evidence=payload.evidence,
        )
    except UnknownEntityError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except ValidationError as exc:
        raise HTTPException(
            status_code=422,
            detail=f"Relationship validation failed: {exc.errors()}",
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return rel.model_dump(mode="json")


# -- People & Relationships plane (spec §11 — frozen routes) ------------------


@router.get("/unanswered")
async def list_unanswered(request: Request, person_id: str | None = None):
    """Factual unanswered-conversation list (spec §11).

    A (person, channel) conversation is surfaced when its newest inbound
    message is at least the configured number of days old with no outbound
    reply after it. Message-counting facts only — never a judgment about the
    person, and the body is never returned (subject + content_ref at most).
    """
    svc = _relationships_service(request)
    entries = await svc.unanswered_all(limit=200)
    if person_id is not None:
        entries = [e for e in entries if e.get("person_id") == person_id]
    return {"data": entries, "meta": _meta(total=len(entries))}


@router.get("/social")
async def list_social_plans(
    request: Request, person_id: str | None = None, days: int = Query(default=14, ge=1, le=90)
):
    """Upcoming social calendar plans (spec §11).

    An event is a plan for the person when the user marked it social
    (``extra["social"] = true``) or the person's identity handle appears
    verbatim in ``extra["attendees"]``. No fuzzy description/location
    matching — the calendar's explicit attendees only.
    """
    svc = _relationships_service(request)
    plans = await svc.social_plans(person_id=person_id, days=days)
    return {"data": plans, "meta": _meta(total=len(plans))}


@router.get("/{relationship_id}")
async def get_relationship(request: Request, relationship_id: str) -> dict[str, Any]:
    """Get a single relationship by ID."""
    service = _service(request)
    try:
        rel = await service.get(relationship_id)
    except KeyError:
        raise HTTPException(
            status_code=404,
            detail=f"Relationship {relationship_id!r} not found",
        )
    return rel.model_dump(mode="json")


@router.delete("/{relationship_id}", status_code=204)
async def delete_relationship(request: Request, relationship_id: str) -> None:
    """Delete a relationship by ID."""
    service = _service(request)
    try:
        await service.delete(relationship_id)
    except KeyError:
        raise HTTPException(
            status_code=404,
            detail=f"Relationship {relationship_id!r} not found",
        )
    return None
