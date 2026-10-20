"""Drafts endpoints (gaps1 G-1.4 — R105, prompt2 §40–§41, §63).

Contract:
- ``GET  /api/drafts``                    → drafts (``?status=draft|discarded|
                                                  sent|all`` — default ``all``),
                                                  most recently updated first
- ``POST /api/drafts``                    → create one draft (201) — the only
                                                  state the API can create is
                                                  ``DRAFT``
- ``GET  /api/drafts/{draft_id}``         → one draft (basis/evidence
                                                  always included)
- ``PATCH /api/drafts/{draft_id}``        → edit a live draft (text / basis /
                                                  evidence); 409 on a
                                                  non-DRAFT draft
- ``POST /api/drafts/{draft_id}/discard``→ the only status transition the
                                                  API performs (DRAFT →
                                                  DISCARDED); 409 otherwise

The store lives in the process-wide
:func:`backend.drafts.get_drafts_service` singleton (bound onto
``app.state.drafts_service`` at startup). It is *always* available — an
empty store returns an empty list.

**No sending, ever.** The drafts plane never claims a send occurred: there
is no send endpoint, and ``sent`` is unreachable through these routes — it
is set only by the relay send path (out of scope for this feature, §41).
Every response carries the draft's ``basis`` and ``evidence`` refs so any
draft is explainable (§71/§72), and no response carries raw third-party
communication content.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from backend.api.models import ApiResponseMeta, ListResponse
from backend.drafts.models import Draft, DraftStatus
from backend.drafts.service import (
    DraftNotFound,
    DraftsService,
    InvalidDraftTransition,
    get_drafts_service,
)

router = APIRouter(prefix="/api/drafts", tags=["drafts"])


def _meta(**pagination: int | None) -> ApiResponseMeta:
    return ApiResponseMeta(
        version="1.0.0",
        request_id=f"req-{uuid4().hex[:12]}",
        timestamp=datetime.now(UTC),
        pagination={k: v for k, v in pagination.items() if v is not None} or None,
    )


def _service(request: Request) -> DraftsService:
    svc = getattr(request.app.state, "drafts_service", None)
    if svc is None:
        # Defensive: the singleton is always resolvable even if startup
        # wiring has not run yet (e.g. unit tests hitting the router
        # directly).
        svc = get_drafts_service()
        request.app.state.drafts_service = svc
    return svc


def _draft_not_found(draft_id: str) -> HTTPException:
    return HTTPException(status_code=404, detail=f"Draft {draft_id} not found.")


def _conflict(draft: Draft, to_status: str) -> HTTPException:
    return HTTPException(
        status_code=409,
        detail=(
            f"Draft {draft.draft_id} is {draft.status}; only DRAFT drafts can be "
            f"moved to {to_status}. SENT is unreachable through the drafts API — "
            "sending happens only in the relay."
        ),
    )


# -- request models -----------------------------------------------------------


class CreateDraftRequest(BaseModel):
    """One new draft. The API only ever creates ``DRAFT`` state."""

    channel: str = Field(..., description="email|imessage|sms|rcs|voicemail")
    text: str = Field(..., min_length=1, description="Draft body (local-only, human-reviewed).")
    conversation_id: str | None = None
    communication_ref: str | None = Field(
        default=None,
        description="Reference into the communications plane (refs only, no raw content).",
    )
    basis: list[str] = Field(default_factory=list, description="Why this draft exists.")
    evidence: list[str] = Field(
        default_factory=list,
        description="Communication / evidence references backing the draft.",
    )
    model_used: str = Field(
        default="manual",
        description="What produced the text: 'manual' / 'deterministic' / drafter name.",
    )
    classification: str = Field(
        default="unclassified",
        description="Sensitivity class of the draft's context.",
    )
    source: str = Field(default="api", description="Where the draft came from.")


class UpdateDraftRequest(BaseModel):
    """Edit a live draft. ``sent`` is not settable here — no field exists
    for it, by design (§41 no impersonation)."""

    text: str | None = Field(default=None, min_length=1)
    basis: list[str] | None = None
    evidence: list[str] | None = None


class DiscardDraftRequest(BaseModel):
    """Optional reason for the DRAFT -> DISCARDED transition."""

    reason: str | None = None


# -- routes --------------------------------------------------------------------


@router.get("", response_model=ListResponse)
@router.get("/", response_model=ListResponse)
async def list_drafts(
    request: Request,
    status: str = Query(
        default="all",
        description=(
            "Filter by lifecycle status: 'all' (default) | 'draft' | "
            "'discarded' | 'sent'. Results are ordered by updated_at "
            "descending."
        ),
    ),
) -> ListResponse:
    """List drafts (basis/evidence included on every record)."""
    svc = _service(request)
    status_key = status.lower().strip() or "all"
    if status_key != "all":
        try:
            DraftStatus.parse(status_key)
        except ValueError:
            raise HTTPException(
                status_code=422,
                detail=f"Invalid status '{status}'. Use all|draft|discarded|sent.",
            )
    items = svc.list(status=None if status_key == "all" else status_key)
    total = len(items)
    return ListResponse(
        data=[d.model_dump(mode="json") for d in items],
        meta=_meta(total=total, page=1, per_page=total or 0),
    )


@router.post("", response_model=Draft, status_code=201)
@router.post("/", response_model=Draft, status_code=201)
async def create_draft(request: Request, payload: CreateDraftRequest) -> Draft:
    """Create one draft. ``DRAFT`` is the only status the API can create."""
    svc = _service(request)
    try:
        return svc.create(**payload.model_dump())
    except ValueError as exc:  # e.g. unknown channel
        raise HTTPException(status_code=422, detail=str(exc))


@router.get("/{draft_id}", response_model=Draft)
async def get_draft(request: Request, draft_id: str) -> Draft:
    """One draft (basis/evidence always included)."""
    svc = _service(request)
    try:
        return svc.get(draft_id)
    except DraftNotFound:
        raise _draft_not_found(draft_id)


@router.patch("/{draft_id}", response_model=Draft)
async def update_draft(
    request: Request, draft_id: str, payload: UpdateDraftRequest
) -> Draft:
    """Edit a live draft's text / basis / evidence (human-controlled).

    409 when the draft is not ``DRAFT`` — discarded drafts are frozen and
    ``SENT`` is unreachable through this plane.
    """
    svc = _service(request)
    try:
        return svc.update(
            draft_id,
            text=payload.text,
            basis=payload.basis,
            evidence=payload.evidence,
        )
    except DraftNotFound:
        raise _draft_not_found(draft_id)
    except InvalidDraftTransition:
        draft = svc.get(draft_id)
        raise _conflict(draft, "updated")


@router.post("/{draft_id}/discard", response_model=Draft)
async def discard_draft(
    request: Request, draft_id: str, payload: DiscardDraftRequest
) -> Draft:
    """The only lifecycle transition the drafts API performs:
    ``DRAFT -> DISCARDED``. 409 for any other state; ``sent`` is
    unreachable through this plane, so a send can never be (re)discarded
    or otherwise mutated here.
    """
    svc = _service(request)
    try:
        return svc.discard(draft_id, reason=payload.reason)
    except DraftNotFound:
        raise _draft_not_found(draft_id)
    except InvalidDraftTransition:
        draft = svc.get(draft_id)
        raise _conflict(draft, DraftStatus.DISCARDED)
