"""Waiting-On endpoints (prompt2 §19 — Waiting-On Detection / §31).

Contract:
- ``GET  /api/waiting-on``                       → waiting-on requests; query
                                                     ``?status=open|resolved|
                                                     cancelled|all`` (default
                                                     ``open``), most-waiting
                                                     first
- ``POST /api/waiting-on/ingest``                → track one outgoing request
                                                     the relay detected (201
                                                     new / 200 replayed dup)
- ``GET  /api/waiting-on/{request_id}``          → one request
- ``POST /api/waiting-on/{request_id}/resolve``  → mark RESOLVED (+ note) and
                                                     resolve its AttentionItem
- ``POST /api/waiting-on/{request_id}/follow-up``→ bump the follow-up count
                                                     and return a rule-based
                                                     *draft* follow-up
                                                     (§13: AI drafts, human
                                                     sends — never sent)

The store lives in the process-wide
:func:`backend.waiting_on.get_waiting_on_service` singleton (bound onto
``app.state.waiting_on_service`` at startup). It is *always* available — an
empty store returns an empty list. Ingestion produces exactly one live
AttentionItem per OPEN request through the shared P0
:class:`~backend.attention.service.AttentionService` scorer, so each open
request surfaces on the Now screen with an explainable
"waiting on {person} for {description} ({n} days)" factor (§21/§22).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import BaseModel, field_validator

from backend.api.models import (
    ApiResponseMeta,
    AttentionItem,
    ListResponse,
    WaitingOnItem,
    WaitingOnSource,
    WaitingOnStatus,
)
from backend.waiting_on.service import WaitingOnService, get_waiting_on_service

router = APIRouter(prefix="/api/waiting-on", tags=["waiting-on"])


def _meta(**pagination: int | None) -> ApiResponseMeta:
    return ApiResponseMeta(
        version="1.0.0",
        request_id=f"req-{uuid4().hex[:12]}",
        timestamp=datetime.now(UTC),
        pagination={k: v for k, v in pagination.items() if v is not None} or None,
    )


def _service(request: Request) -> WaitingOnService:
    svc = getattr(request.app.state, "waiting_on_service", None)
    if svc is None:
        # Defensive: the singleton is always resolvable even if startup wiring
        # has not run yet (e.g. unit tests hitting the router directly).
        svc = get_waiting_on_service()
        request.app.state.waiting_on_service = svc
    return svc


def _request_not_found(request_id: str) -> HTTPException:
    return HTTPException(status_code=404, detail=f"Waiting-on request {request_id} not found.")


# -- request models ----------------------------------------------------------
class IngestWaitingOnRequest(BaseModel):
    """One outgoing request the relay detected went unanswered (prompt2 §19).

    The relay/personal-ops layers detect the outgoing request and post a
    normalized record here; NerveCenter only tracks it (§46/§47 out of scope,
    no sending in-process). ``request_id`` is the stable key for idempotent
    ingest (a replay re-posts the same id → no-op). ``source`` is typed as the
    ``WaitingOnSource`` enum so an unknown provenance is a Pydantic 422, not a
    stored error.
    """

    request_id: str
    person_display_name: str
    person_id: str | None = None
    description: str
    requested_at: datetime | None = None
    source: WaitingOnSource = WaitingOnSource.MESSAGES
    source_thread_id: str | None = None
    # Optional escalation policy (mirrors the P0 AttentionItem contract,
    # §22/§45): e.g. ``{"priority": "urgent"}`` → "follow up within 24h, or
    # escalate to a VIP if unresolved". Producer-supplied; the service only
    # stores and surfaces it (the UI renders an "Urgent" badge for it).
    escalation_policy: dict[str, Any] | None = None

    @field_validator("request_id", "person_display_name", "description")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("must be a non-empty string")
        return value.strip()


class ResolveRequest(BaseModel):
    """Optional note recorded when a request is resolved (prompt2 §19)."""

    note: str | None = None


# -- response models ---------------------------------------------------------
class IngestWaitingOnResponse(BaseModel):
    item: WaitingOnItem
    new_attention: AttentionItem | None = None
    duplicate: bool = False


class ResolveWaitingOnResponse(BaseModel):
    item: WaitingOnItem
    attention_id: str | None = None
    resolved: bool = False


class FollowUpResponse(BaseModel):
    request_id: str
    draft: str
    follow_up_count: int
    last_follow_up_at: datetime | None = None
    status: WaitingOnStatus = WaitingOnStatus.OPEN
    draft_only: bool = True
    basis: str
    new_attention: AttentionItem | None = None


# -- routes -----------------------------------------------------------------
@router.get("", response_model=ListResponse)
@router.get("/", response_model=ListResponse)
async def list_waiting_on(
    request: Request,
    status: str = Query(
        default="open",
        description=(
            "Filter by lifecycle status: 'open' (default) | 'resolved' | "
            "'cancelled' | 'all' (everything). Open results are ordered by "
            "days elapsed descending, then requested_at ascending."
        ),
    ),
) -> ListResponse:
    """List waiting-on requests (default: OPEN, most-waiting first)."""
    svc = _service(request)
    status_key = status.lower().strip() or "open"
    if status_key == "all":
        items = svc.list_all()
    else:
        try:
            status_enum = WaitingOnStatus(status_key)
        except ValueError:
            raise HTTPException(
                status_code=422,
                detail=f"Invalid status '{status}'. Use open|resolved|cancelled|all.",
            )
        items = (
            svc.list_open()
            if status_enum is WaitingOnStatus.OPEN
            else svc.list_all(status=status_enum)
        )
    total = len(items)
    return ListResponse(
        data=[i.model_dump(mode="json") for i in items],
        meta=_meta(total=total, page=1, per_page=total or 0),
    )


@router.post("/ingest", response_model=IngestWaitingOnResponse)
async def ingest_waiting_on(
    request: Request, payload: IngestWaitingOnRequest, response: Response
) -> IngestWaitingOnResponse:
    """Track one outgoing request. 201 on a new ``request_id``, 200 on a
    replayed duplicate (the relay replays queued events on reconnect — §48)."""
    svc = _service(request)
    result = svc.ingest(payload.model_dump())
    response.status_code = 200 if result["duplicate"] else 201
    return IngestWaitingOnResponse(
        item=result["item"],
        new_attention=result["new_attention"],
        duplicate=result["duplicate"],
    )


@router.get("/{request_id}", response_model=WaitingOnItem)
async def get_waiting_on(request: Request, request_id: str) -> WaitingOnItem:
    """One waiting-on request."""
    svc = _service(request)
    item = svc.get(request_id)
    if item is None:
        raise _request_not_found(request_id)
    return item


@router.post("/{request_id}/resolve", response_model=ResolveWaitingOnResponse)
async def resolve_waiting_on(
    request: Request, request_id: str, payload: ResolveRequest | None = None
) -> ResolveWaitingOnResponse:
    """Mark the request RESOLVED and resolve its live AttentionItem.

    The human acted (the counterpart answered, or the matter closed), so the
    item leaves the Now screen — mirroring email's acknowledge.
    """
    svc = _service(request)
    if svc.get(request_id) is None:
        raise _request_not_found(request_id)
    item = svc.resolve(request_id, note=(payload.note if payload else None))
    return ResolveWaitingOnResponse(
        item=item,
        attention_id=item.attention_id,
        resolved=item.attention_id is not None,
    )


@router.post("/{request_id}/follow-up", response_model=FollowUpResponse)
async def follow_up(request: Request, request_id: str) -> FollowUpResponse:
    """Bump the follow-up count and return a rule-based *draft* follow-up
    (§13: AI drafts, human sends). Nothing is sent from here; the item stays
    OPEN."""
    svc = _service(request)
    if svc.get(request_id) is None:
        raise _request_not_found(request_id)
    result: dict[str, Any] = svc.follow_up(request_id)
    return FollowUpResponse(**result)
