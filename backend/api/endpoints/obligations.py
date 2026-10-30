"""Obligation endpoints (P2-A — Personal Reliability).

Contract (frozen routes, ``/api/obligations`` — spec:
``docs/contracts/obligation.md``):

- ``GET /`` — list obligations; optional ``status`` / ``type`` /
  ``actionable`` / ``due_before`` filters.
- ``GET /needs-you`` — the Today "Needs You" projection (actionable,
  inferred-flagged, priority-sorted).
- ``GET /coming-up?days=14`` — due-within-window obligations.
- ``GET /waiting-on`` — obligations awaiting others.
- ``GET /stale`` — stale-flagged obligations (source-health signal).
- ``POST /`` — create an explicit obligation (user-created).
- ``POST /bridge`` — bridge a plane record (bill / calendar event /
  follow-up) into an obligation via dedup upsert.
- ``GET /{id}`` — one obligation.
- ``POST /{id}/accept`` — accept an inferred obligation (origin → explicit).
- ``POST /{id}/snooze?until=`` — snooze (reversible).
- ``POST /{id}/complete`` — mark done (terminal).
- ``POST /{id}/dismiss`` — dismiss (terminal).
- ``POST /{id}/reverify`` — refresh last_verified_at; stale → active.
- ``POST /sweep-stale`` — mark unverified obligations stale.

House degradation (same as bills / routines): without a durable
``repository_registry`` on ``app.state``, reads return 200 with empty
shapes and writes return 202 ``{"executed": false, "reason":
"repository_unavailable"}``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from backend.obligations.service import (
    COMING_UP_WINDOW_DAYS,
    ObligationsService,
)
from backend.persistence.repositories.obligations_repo import ObligationsRepository

router = APIRouter(prefix="/api/obligations", tags=["obligations"])

#: Status values accepted on the ``?status=`` query filter.
_STATUSES = Literal[
    "active",
    "scheduled",
    "in_progress",
    "waiting",
    "done",
    "dismissed",
    "snoozed",
    "stale",
]
#: Obligation type values accepted on the ``?type=`` query filter.
_TYPES = Literal[
    "obligation",
    "commitment",
    "task",
    "deadline",
    "appointment",
    "bill",
    "routine",
    "follow_up",
    "waiting_on",
]


# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------


class ObligationCreateRequest(BaseModel):
    """Create an explicit (user-created) obligation."""

    obligation_type: str = Field(description="Obligation type (frozen set)")
    title: str = Field(min_length=1, description="Human-readable title")
    due_at: str | None = Field(default=None, description="ISO-8601 due date")
    next_action: str | None = Field(
        default=None, description="What the user should do"
    )
    consequence: str | None = Field(
        default=None, description="What happens if not done"
    )
    priority: int = Field(default=1, ge=0, le=4)
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    source_record_ref: dict[str, Any] | None = Field(
        default=None,
        description='Pointer back to the concrete plane record, e.g. {"plane": "bills", "id": "..."}',
    )


class BridgeRequest(BaseModel):
    """Bridge a plane record into an obligation.

    ``source`` must carry a ``plane`` key and a nested ``record`` key (or
    the record itself as the body).
    """

    plane: Literal["bills", "calendar", "communications", "followups"]
    record: dict[str, Any]


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------


def _registry(request: Request) -> Any:
    """The durable repository registry, or ``None`` (degraded app)."""
    return getattr(request.app.state, "repository_registry", None)


def _service(request: Request) -> ObligationsService | None:
    """Build the obligation service from the durable registry, or ``None``."""
    registry = _registry(request)
    if registry is None:
        return None
    return ObligationsService(ObligationsRepository(registry.session_factory))


def _degraded_read(default: dict[str, Any]) -> dict[str, Any]:
    """House degradation: empty shape when the registry is absent."""
    return default


def _parse_due(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(
            status_code=422, detail=f"invalid due_at: {value!r}"
        ) from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/")
async def list_obligations(
    request: Request,
    status: _STATUSES | None = None,
    type: _TYPES | None = None,  # noqa: A002 — query param named "type"
    actionable: bool = Query(default=False),
    due_before: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
):
    """List obligations with optional filters."""
    registry = _registry(request)
    if registry is None:
        return {"obligations": []}
    svc = _service(request)
    if svc is None:
        return {"obligations": []}
    rows = await svc._repo.list(
        status=status,
        obligation_type=type,
        due_before=_parse_due(due_before),
        actionable_only=actionable,
        limit=limit,
    )
    return {"obligations": [svc._to_dict(r) for r in rows]}


@router.get("/needs-you")
async def needs_you(request: Request):
    """The Today "Needs You" projection."""
    registry = _registry(request)
    if registry is None:
        return {"needs_you": []}
    svc = _service(request)
    if svc is None:
        return {"needs_you": []}
    return {"needs_you": await svc.needs_you()}


@router.get("/coming-up")
async def coming_up(
    request: Request,
    days: int = Query(
        default=COMING_UP_WINDOW_DAYS, ge=1, le=90
    ),
):
    """Due-within-window obligations."""
    registry = _registry(request)
    if registry is None:
        return {"coming_up": []}
    svc = _service(request)
    if svc is None:
        return {"coming_up": []}
    return {"coming_up": await svc.coming_up(days=days)}


@router.get("/waiting-on")
async def waiting_on(request: Request):
    """Obligations awaiting others."""
    registry = _registry(request)
    if registry is None:
        return {"waiting_on": []}
    svc = _service(request)
    if svc is None:
        return {"waiting_on": []}
    return {"waiting_on": await svc.waiting_on()}


@router.get("/stale")
async def stale(request: Request):
    """Stale-flagged obligations (source-health signal)."""
    registry = _registry(request)
    if registry is None:
        return {"stale": []}
    svc = _service(request)
    if svc is None:
        return {"stale": []}
    return {"stale": await svc.stale()}


@router.post("/", status_code=201)
async def create_obligation(
    request: Request, body: ObligationCreateRequest
) -> JSONResponse:  # noqa: F811
    """Create an explicit (user-created) obligation."""
    registry = _registry(request)
    if registry is None:
        return JSONResponse(
            status_code=202,
            content={
                "executed": False,
                "reason": "repository_unavailable",
            },
        )
    svc = _service(request)
    if svc is None:
        return JSONResponse(
            status_code=202,
            content={"executed": False, "reason": "service_unavailable"},
        )
    try:
        out = await svc.create(
            obligation_type=body.obligation_type,
            title=body.title,
            origin="explicit",
            due_at=_parse_due(body.due_at),
            next_action=body.next_action,
            consequence=body.consequence,
            priority=body.priority,
            evidence=body.evidence,
            source_record_ref=body.source_record_ref or {},
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return JSONResponse(status_code=201, content=out)


@router.post("/bridge", status_code=201)
async def bridge_obligation(
    request: Request, body: BridgeRequest
) -> JSONResponse:  # noqa: F811
    """Bridge a plane record into an obligation (dedup upsert)."""
    registry = _registry(request)
    if registry is None:
        return JSONResponse(
            status_code=202,
            content={
                "executed": False,
                "reason": "repository_unavailable",
            },
        )
    svc = _service(request)
    if svc is None:
        return JSONResponse(
            status_code=202,
            content={"executed": False, "reason": "service_unavailable"},
        )
    source = {"plane": body.plane, "record": body.record}
    try:
        out = await svc.upsert_from_source(source, registry=registry)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return JSONResponse(status_code=201, content=out)


@router.get("/{obligation_id}")
async def get_obligation(
    request: Request, obligation_id: str
):
    """One obligation."""
    registry = _registry(request)
    if registry is None:
        return JSONResponse(
            status_code=200, content={"obligation": None}
        )
    svc = _service(request)
    if svc is None:
        return JSONResponse(
            status_code=200, content={"obligation": None}
        )
    out = await svc.get(obligation_id)
    if out is None:
        raise HTTPException(status_code=404, detail="obligation not found")
    return {"obligation": out}


@router.post("/{obligation_id}/accept")
async def accept_obligation(
    request: Request, obligation_id: str
):
    """Accept an inferred obligation (origin → explicit, actionable)."""
    registry = _registry(request)
    if registry is None:
        return JSONResponse(
            status_code=202,
            content={"executed": False, "reason": "repository_unavailable"},
        )
    svc = _service(request)
    if svc is None:
        return JSONResponse(
            status_code=202,
            content={"executed": False, "reason": "service_unavailable"},
        )
    out = await svc.accept(obligation_id)
    if out is None:
        raise HTTPException(status_code=404, detail="obligation not found")
    return {"obligation": out}


@router.post("/{obligation_id}/snooze")
async def snooze_obligation(
    request: Request,
    obligation_id: str,
    until: str = Query(description="ISO-8601 snooze-until"),
):
    """Snooze an obligation until the given time (reversible)."""
    registry = _registry(request)
    if registry is None:
        return JSONResponse(
            status_code=202,
            content={"executed": False, "reason": "repository_unavailable"},
        )
    svc = _service(request)
    if svc is None:
        return JSONResponse(
            status_code=202,
            content={"executed": False, "reason": "service_unavailable"},
        )
    until_dt = _parse_due(until)
    if until_dt is None:
        raise HTTPException(status_code=422, detail="until must be a valid ISO-8601 date")
    out = await svc.snooze(obligation_id, until_dt)
    if out is None:
        raise HTTPException(status_code=404, detail="obligation not found")
    return {"obligation": out}


@router.post("/{obligation_id}/complete")
async def complete_obligation(
    request: Request, obligation_id: str
):
    """Mark an obligation done (terminal)."""
    registry = _registry(request)
    if registry is None:
        return JSONResponse(
            status_code=202,
            content={"executed": False, "reason": "repository_unavailable"},
        )
    svc = _service(request)
    if svc is None:
        return JSONResponse(
            status_code=202,
            content={"executed": False, "reason": "service_unavailable"},
        )
    out = await svc.complete(obligation_id)
    if out is None:
        raise HTTPException(status_code=404, detail="obligation not found")
    return {"obligation": out}


@router.post("/{obligation_id}/dismiss")
async def dismiss_obligation(
    request: Request, obligation_id: str
):
    """Dismiss an obligation (terminal)."""
    registry = _registry(request)
    if registry is None:
        return JSONResponse(
            status_code=202,
            content={"executed": False, "reason": "repository_unavailable"},
        )
    svc = _service(request)
    if svc is None:
        return JSONResponse(
            status_code=202,
            content={"executed": False, "reason": "service_unavailable"},
        )
    out = await svc.dismiss(obligation_id)
    if out is None:
        raise HTTPException(status_code=404, detail="obligation not found")
    return {"obligation": out}


@router.post("/{obligation_id}/reverify")
async def reverify_obligation(
    request: Request, obligation_id: str
):
    """Refresh last_verified_at; stale → active."""
    registry = _registry(request)
    if registry is None:
        return JSONResponse(
            status_code=202,
            content={"executed": False, "reason": "repository_unavailable"},
        )
    svc = _service(request)
    if svc is None:
        return JSONResponse(
            status_code=202,
            content={"executed": False, "reason": "service_unavailable"},
        )
    out = await svc.reverify(obligation_id)
    if out is None:
        raise HTTPException(status_code=404, detail="obligation not found")
    return {"obligation": out}


@router.post("/sweep-stale")
async def sweep_stale(request: Request):
    """Mark unverified obligations stale."""
    registry = _registry(request)
    if registry is None:
        return JSONResponse(
            status_code=202,
            content={"executed": False, "reason": "repository_unavailable"},
        )
    svc = _service(request)
    if svc is None:
        return JSONResponse(
            status_code=202,
            content={"executed": False, "reason": "service_unavailable"},
        )
    rows = await svc.sweep_stale()
    return {"stale": rows}
