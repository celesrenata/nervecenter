"""Follow-ups endpoints (R104 — AC-R07, spec §57).

Contract:
- ``GET  /api/followups``                       → follow-ups; query
                                                     ``?status=OPEN|SNOOZED|
                                                     COMPLETED|FORGOTTEN|all``
                                                     (default ``all``),
                                                     most-due first
- ``POST /api/followups``                       → create one follow-up (201)
- ``GET  /api/followups/{followup_id}``         → one follow-up
- ``PATCH /api/followups/{followup_id}``        → edit a live follow-up
                                                     (reason / person / due);
                                                     409 on a terminal
                                                     follow-up
- ``POST /api/followups/{followup_id}/snooze``  → OPEN → SNOOZED (defers the
                                                     due time; the live
                                                     AttentionItem is
                                                     suppressed)
- ``POST /api/followups/{followup_id}/complete``→ OPEN|SNOOZED → COMPLETED
                                                     (resolves the live
                                                     AttentionItem, emits
                                                     ``followup.completed``)
- ``POST /api/followups/{followup_id}/forget``  → OPEN|SNOOZED → FORGOTTEN
                                                     (resolves the live
                                                     AttentionItem, emits
                                                     ``followup.forgotten``)
- ``POST /api/followups/evaluate``              → run the deterministic
                                                     due-evaluation hook:
                                                     every due OPEN/SNOOZED
                                                     follow-up gets one live
                                                     AttentionItem (source
                                                     ``followups``) and a
                                                     ``followup.triggered``
                                                     event

The store lives in the process-wide
:func:`backend.followups.get_followup_service` singleton (bound onto
``app.state.followup_service`` at startup). It is *always* available — an
empty store returns an empty list. Terminal states (``COMPLETED`` /
``FORGOTTEN``) are immutable: any further transition is a 409.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field

from backend.api.models import ApiResponseMeta, ListResponse
from backend.followups.models import FollowUp, FollowUpStatus, FollowUpTransitionError
from backend.followups.service import FollowUpService, get_followup_service

router = APIRouter(prefix="/api/followups", tags=["followups"])


def _meta(**pagination: int | None) -> ApiResponseMeta:
    return ApiResponseMeta(
        version="1.0.0",
        request_id=f"req-{uuid4().hex[:12]}",
        timestamp=datetime.now(UTC),
        pagination={k: v for k, v in pagination.items() if v is not None} or None,
    )


def _service(request: Request) -> FollowUpService:
    svc = getattr(request.app.state, "followup_service", None)
    if svc is None:
        # Defensive: the singleton is always resolvable even if startup wiring
        # has not run yet (e.g. unit tests hitting the router directly).
        svc = get_followup_service()
        request.app.state.followup_service = svc
    return svc


def _not_found(followup_id: str) -> HTTPException:
    return HTTPException(
        status_code=404, detail=f"Follow-up {followup_id} not found."
    )


def _conflict(followup_id: str, err: FollowUpTransitionError) -> HTTPException:
    return HTTPException(status_code=409, detail=str(err))


# -- request models ----------------------------------------------------------


class FollowUpCreateRequest(BaseModel):
    """One follow-up to track (R104 — §57).

    ``target_kind`` / ``target_id`` name the originating record
    (``waiting_on`` + request id, ``commitment`` + finding id, ...);
    ``due_at`` defaults to now + the configurable default delay.
    """

    target_kind: str = Field(..., min_length=1)
    target_id: str = Field(..., min_length=1)
    reason: str = Field(..., min_length=1)
    person_id: str | None = None
    due_at: datetime | None = None
    created_from: str | None = None


class FollowUpUpdateRequest(BaseModel):
    """Editable fields for a live follow-up; terminal follow-ups reject all
    updates (409)."""

    reason: str | None = Field(default=None, min_length=1)
    person_id: str | None = None
    due_at: datetime | None = None


class SnoozeRequest(BaseModel):
    """Snooze body: an absolute ``until`` or a relative ``for_hours``
    (default 24h)."""

    until: datetime | None = None
    for_hours: float | None = Field(default=None, ge=0)
    detail: str | None = None


class NoteRequest(BaseModel):
    """Optional human note recorded in the follow-up's audit history."""

    note: str | None = None


class TriggeredFollowUp(BaseModel):
    """One record from the due-evaluation: the follow-up, its triggered
    event type, and the AttentionItem it (re)raised."""

    followup: FollowUp
    attention_id: str | None = None
    attention_reason: str | None = None
    event_type: str
    days_overdue: int = 0


class EvaluateResponse(BaseModel):
    """Outcome of one ``POST /api/followups/evaluate`` run."""

    triggered: list[TriggeredFollowUp]
    events_emitted: int


# -- endpoints ---------------------------------------------------------------


@router.get("", response_model=ListResponse)
async def list_followups(
    request: Request,
    status: str = Query(
        default="all",
        description=(
            "Filter by lifecycle status: 'all' (default) | 'OPEN' | 'SNOOZED' "
            "| 'COMPLETED' | 'FORGOTTEN'. Ordered by due_at ascending "
            "(most-due first)."
        ),
    ),
) -> ListResponse:
    """List follow-ups (default: all statuses, most-due first)."""
    svc = _service(request)
    status_key = status.strip() or "all"
    if status_key.lower() == "all":
        items = svc.list()
    else:
        try:
            status_enum = FollowUpStatus.parse(status_key)
        except ValueError as err:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Invalid status '{status}'. Use OPEN|SNOOZED|COMPLETED|"
                    f"FORGOTTEN|all."
                ),
            ) from err
        items = svc.list(status=status_enum)
    total = len(items)
    return ListResponse(
        data=[f.model_dump(mode="json") for f in items],
        meta=_meta(total=total, page=1, per_page=total or 0),
    )


@router.post("", response_model=FollowUp, status_code=201)
async def create_followup(
    request: Request, payload: FollowUpCreateRequest, response: Response
) -> FollowUp:
    """Create one follow-up (201). Terminal states are not creatable —
    ``complete`` / ``forget`` are the only way to reach them."""
    svc = _service(request)
    result = svc.create(
        target_kind=payload.target_kind,
        target_id=payload.target_id,
        reason=payload.reason,
        person_id=payload.person_id,
        due_at=payload.due_at,
        created_from=payload.created_from,
    )
    return result["followup"]


@router.post("/evaluate", response_model=EvaluateResponse)
async def evaluate_due(request: Request) -> EvaluateResponse:
    """Run the deterministic due-evaluation hook (R104 step 4).

    Every due OPEN/SNOOZED follow-up gets exactly one live AttentionItem
    (source ``followups``) and one ``followup.triggered`` event; a re-run
    refreshes the same item instead of stacking duplicates.
    """
    svc = _service(request)
    results = svc.evaluate_due()
    return EvaluateResponse(
        triggered=[
            TriggeredFollowUp(
                followup=r["followup"],
                attention_id=(
                    r["attention"].attention_id if r["attention"] else None
                ),
                attention_reason=(
                    r["attention"].reason if r["attention"] else None
                ),
                event_type=r["event"].event_type,
                days_overdue=r["days_overdue"],
            )
            for r in results
        ],
        events_emitted=len(results),
    )


@router.get("/{followup_id}", response_model=FollowUp)
async def get_followup(request: Request, followup_id: str) -> FollowUp:
    """Fetch one follow-up (with its full audit history)."""
    svc = _service(request)
    fu = svc.get(followup_id)
    if fu is None:
        raise _not_found(followup_id)
    return fu


@router.patch("/{followup_id}", response_model=FollowUp)
async def update_followup(
    request: Request, followup_id: str, payload: FollowUpUpdateRequest
) -> FollowUp:
    """Edit a live follow-up; 409 on a terminal one, 422 on an empty body."""
    svc = _service(request)
    if svc.get(followup_id) is None:
        raise _not_found(followup_id)
    if payload.model_dump(exclude_unset=True) == {}:
        raise HTTPException(
            status_code=422, detail="No fields to update."
        )
    try:
        return svc.update(
            followup_id,
            reason=payload.reason,
            person_id=payload.person_id,
            due_at=payload.due_at,
        )
    except FollowUpTransitionError as err:
        raise _conflict(followup_id, err) from err
    except ValueError as err:
        raise HTTPException(status_code=422, detail=str(err)) from err


@router.post("/{followup_id}/snooze", response_model=FollowUp)
async def snooze_followup(
    request: Request, followup_id: str, payload: SnoozeRequest
) -> FollowUp:
    """Snooze a live follow-up (OPEN → SNOOZED); its live AttentionItem is
    suppressed until it is re-opened or re-triggers. 409 if already snoozed
    or terminal."""
    svc = _service(request)
    if svc.get(followup_id) is None:
        raise _not_found(followup_id)
    for_delta = timedelta(hours=payload.for_hours) if payload.for_hours else None
    try:
        return svc.snooze(
            followup_id,
            until=payload.until,
            for_delta=for_delta,
            detail=payload.detail,
        )
    except FollowUpTransitionError as err:
        raise _conflict(followup_id, err) from err


@router.post("/{followup_id}/complete", response_model=FollowUp)
async def complete_followup(
    request: Request, followup_id: str, payload: NoteRequest
) -> FollowUp:
    """Mark a live follow-up COMPLETED; its AttentionItem is resolved and
    ``followup.completed`` is emitted. 409 otherwise."""
    svc = _service(request)
    if svc.get(followup_id) is None:
        raise _not_found(followup_id)
    try:
        return svc.complete(followup_id, note=payload.note)
    except FollowUpTransitionError as err:
        raise _conflict(followup_id, err) from err


@router.post("/{followup_id}/forget", response_model=FollowUp)
async def forget_followup(
    request: Request, followup_id: str, payload: NoteRequest
) -> FollowUp:
    """Mark a live follow-up FORGOTTEN; its AttentionItem is resolved and
    ``followup.forgotten`` is emitted. 409 otherwise."""
    svc = _service(request)
    if svc.get(followup_id) is None:
        raise _not_found(followup_id)
    try:
        return svc.forget(followup_id, note=payload.note)
    except FollowUpTransitionError as err:
        raise _conflict(followup_id, err) from err
