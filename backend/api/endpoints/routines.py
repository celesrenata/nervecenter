"""Routine Engine endpoints (Personal-Ops Recovery §9 — workstream H).

Contract (frozen routes, ``/api/routines``):

- ``GET  /api/routines``            — list routines (optionally
  ``?enabled_only=true``).
- ``POST /api/routines``            — create (201); validates ``kind``
  against the frozen :data:`ROUTINE_KINDS` and the schedule shape (HH:MM
  times, ISO weekdays 0–6, IANA timezone).
- ``PATCH /api/routines/{id}``      — field-level update
  (``title`` / ``kind`` / ``schedule`` / ``enabled``; ``None`` leaves
  values alone).
- ``POST /api/routines/{id}/checkin`` — idempotent per (routine, UTC day)
  check-in (option ``completed`` — an explicit skip is still "handled" —
  and ``note``).
- ``POST /api/routines/{id}/complete`` — complete = a completed check-in +
  the completion stamp; clears the matching window flag if present.
- ``GET  /api/routines/today``      — the Today projection:
  ``{due_now, upcoming, missed_notes, completed_today,
  sleep: {regularity, avg_minutes, n}}`` with ``sleep.regularity ==
  "unknown"`` whenever the record window is sparse (missing data never
  proves asleep or awake).

House degradation (Area 4): without a durable ``repository_registry`` the
GET routes answer **200 with empty shapes** and the write routes answer
**202** with ``{"executed": false, "reason": "persistence_not_configured"}``
— an honest "accepted but not recorded", never a fake success and never a
5xx.

The engine semantics live in :mod:`backend.routines.service` — the one-time
gentle missed-window note (never a streak), the deterministic clock-based
wake fallback alarm (persisted to ``deterministic_fallback_alarm``), and the
durable attention mirroring — so this module stays a thin HTTP surface.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field

from backend.persistence.models.routines import ROUTINE_KINDS
from backend.persistence.repositories.routines_repo import RoutinesRepository
from backend.routines.service import RoutinesService

router = APIRouter(prefix="/api/routines", tags=["routines"])

_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def _service(request: Request) -> RoutinesService | None:
    """Build the routines service from the durable registry, or ``None``
    (house degradation — the route answers an honest empty / 202 shape)."""
    registry = getattr(request.app.state, "repository_registry", None)
    if registry is None:
        return None
    return RoutinesService(RoutinesRepository(registry.session_factory))


def _degraded(response: Response) -> dict[str, Any]:
    """The honest degradation write shape (202 — not recorded, said so)."""
    response.status_code = 202
    return {"executed": False, "reason": "persistence_not_configured"}


def _require_routine(routine_id: str, routine: dict[str, Any] | None) -> None:
    if routine is None:
        raise HTTPException(status_code=404, detail="routine not found")


def _validate_kind(kind: str) -> str:
    if kind not in ROUTINE_KINDS:
        raise HTTPException(
            status_code=422,
            detail=f"invalid kind: {kind!r} (must be one of {', '.join(ROUTINE_KINDS)})",
        )
    return kind


def _validate_schedule(schedule: dict[str, Any] | None) -> dict[str, Any] | None:
    """Validate the schedule shape: HH:MM times, ISO weekdays 0–6, IANA zone.

    ``None`` / empty schedule is allowed (a routine with no slots simply has
    nothing to evaluate). A present schedule must have well-formed fields;
    anything unparseable is a 422, not a silent drop.
    """
    if schedule is None:
        return None
    if not isinstance(schedule, dict):
        raise HTTPException(status_code=422, detail="schedule must be an object")
    times = schedule.get("times", [])
    if not isinstance(times, list):
        raise HTTPException(
            status_code=422, detail="schedule.times must be a list of HH:MM"
        )
    for t in times:
        if not isinstance(t, str) or not _TIME_RE.match(t):
            raise HTTPException(
                status_code=422,
                detail=f"invalid schedule time: {t!r} (expected HH:MM)",
            )
    weekdays = schedule.get("weekdays", [])
    if not isinstance(weekdays, list):
        raise HTTPException(
            status_code=422, detail="schedule.weekdays must be a list of 0-6"
        )
    for w in weekdays:
        if not isinstance(w, int) or not 0 <= w <= 6:
            raise HTTPException(
                status_code=422,
                detail=f"invalid weekday: {w!r} (expected 0=Mon … 6=Sun)",
            )
    timezone = schedule.get("timezone")
    if timezone is not None and not isinstance(timezone, str) or not timezone:
        if timezone is not None:
            raise HTTPException(
                status_code=422, detail=f"invalid IANA timezone: {timezone!r}"
            )
    cleaned: dict[str, Any] = {}
    if times:
        cleaned["times"] = times
    if weekdays:
        cleaned["weekdays"] = weekdays
    if timezone:
        cleaned["timezone"] = timezone
    return cleaned or None


# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------


class RoutineCreateRequest(BaseModel):
    """A new routine: ``kind`` + ``title`` + an optional schedule."""

    kind: str = Field(..., description="One of the frozen ROUTINE_KINDS")
    title: str = Field(..., min_length=1, max_length=512)
    schedule: dict[str, Any] | None = None
    enabled: bool = True


class RoutinePatchRequest(BaseModel):
    """Field-level edit of an existing routine (``None`` = leave as-is)."""

    title: str | None = Field(default=None, min_length=1, max_length=512)
    kind: str | None = None
    schedule: dict[str, Any] | None = None
    enabled: bool | None = None


class CheckInRequest(BaseModel):
    """A check-in (option ``completed`` — a skip is still handled — and
    ``note``); idempotent per (routine, UTC day)."""

    at: datetime | None = None
    completed: bool = True
    note: str | None = None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/")
async def list_routines(
    request: Request,
    enabled_only: bool = Query(default=False),
) -> dict[str, Any]:
    """The routine list (optionally enabled-only); empty when degraded."""
    svc = _service(request)
    if svc is None:
        return {"data": [], "durable": False}
    rows = await svc.list_routines(enabled_only=enabled_only)
    return {"data": rows, "durable": True}


@router.post("/", status_code=201)
async def create_routine(
    request: Request, body: RoutineCreateRequest, response: Response
) -> dict[str, Any]:
    """Create a routine (201); 202 ``executed: false`` without a store."""
    svc = _service(request)
    if svc is None:
        return _degraded(response)
    _validate_kind(body.kind)
    schedule = _validate_schedule(body.schedule)
    row = await svc._repo.create_routine(
        kind=body.kind,
        title=body.title,
        schedule=schedule,
        enabled=body.enabled,
    )
    return {"routine": svc._routine_dict(row), "created": True}


@router.patch("/{routine_id}")
async def patch_routine(
    request: Request, routine_id: str, body: RoutinePatchRequest, response: Response
) -> dict[str, Any]:
    """Field-level update (``None`` fields are left alone); 202 when
    degraded; 404 when the routine does not exist."""
    svc = _service(request)
    if svc is None:
        return _degraded(response)
    if body.kind is not None:
        _validate_kind(body.kind)
    schedule = _validate_schedule(body.schedule) if body.schedule is not None else None
    _require_routine(routine_id, await svc.get_routine(routine_id))
    fields: dict[str, Any] = {}
    if body.title is not None:
        fields["title"] = body.title
    if body.kind is not None:
        fields["kind"] = body.kind
    if body.schedule is not None:
        fields["schedule"] = schedule if schedule is not None else {}
    if body.enabled is not None:
        fields["enabled"] = body.enabled
    row = await svc._repo.update_routine(routine_id, **fields)
    if row is None:
        raise HTTPException(status_code=404, detail="routine not found")
    return {"routine": svc._routine_dict(row)}


@router.post("/{routine_id}/checkin")
async def check_in(
    request: Request, routine_id: str, body: CheckInRequest, response: Response
) -> dict[str, Any]:
    """Idempotent per (routine, UTC day) check-in; 202 when degraded."""
    svc = _service(request)
    if svc is None:
        return _degraded(response)
    _require_routine(routine_id, await svc.get_routine(routine_id))
    ci = await svc.check_in(
        routine_id, at=body.at, completed=body.completed, note=body.note
    )
    return {"check_in": ci, "executed": True}


@router.post("/{routine_id}/complete")
async def complete(
    request: Request, routine_id: str, response: Response
) -> dict[str, Any]:
    """Complete = a completed check-in + the completion stamp; clears the
    matching window flag if present. 202 when degraded."""
    svc = _service(request)
    if svc is None:
        return _degraded(response)
    _require_routine(routine_id, await svc.get_routine(routine_id))
    row = await svc.complete(routine_id)
    if row is None:
        raise HTTPException(status_code=404, detail="routine not found")
    return {"routine": row, "executed": True}


@router.get("/today")
async def today_view(request: Request) -> dict[str, Any]:
    """The routines contribution to the Today page: due-now slots, upcoming
    slots, the one-time gentle missed notes, today's completed check-ins,
    and the sleep regularity block (``unknown`` when sparse — never an
    asleep/awake assertion)."""
    svc = _service(request)
    if svc is None:
        return {
            "due_now": [],
            "upcoming": [],
            "missed_notes": [],
            "completed_today": [],
            "sleep": {"regularity": "unknown", "avg_minutes": None, "n": 0},
            "durable": False,
        }
    view = await svc.today_view()
    view["durable"] = True
    return view
