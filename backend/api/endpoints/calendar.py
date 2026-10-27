"""Calendar endpoints (prompt2 Phase P2 — Calendar Integration + Conflict Reasoning).

Contract (read-only context plane, §42):
- ``GET  /api/calendar``                       → upcoming events (default 7-day horizon)
- ``POST /api/calendar/ingest``                → upsert one normalized event (201/200)
- ``GET  /api/calendar/{event_id}``           → one event
- ``DELETE /api/calendar/{event_id}``         → remove an event (sync delete)
- ``POST /api/calendar/availability``         → scheduling guard: is [start,end) free?
                                                       what blocks it? earliest it could fit?
- ``POST /api/calendar/free-time``            → earliest free slot of a duration
- ``POST /api/calendar/suggest``              → §43 reply: given a desired start +
                                                       duration, "I can do after 19:30"

The store lives in the process-wide ``get_calendar_service`` singleton (bound onto
``app.state.calendar_service`` at startup). It is *always* available — an empty
calendar returns an empty list. NerveCenter never mutates the user's real
calendar; it only holds read-only copies for availability reasoning (§42).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field

from backend.api.models import ApiResponseMeta, CalendarEvent, ListResponse
from backend.calendar.service import CalendarService, get_calendar_service
from backend.calendar.sync import (
    SECRET_KIND,
    SECRET_OWNER_KIND,
    CalendarSyncRunner,
    HttpxTransport,
)

router = APIRouter(prefix="/api/calendar", tags=["calendar"])


def _meta(total: int, per_page: int) -> ApiResponseMeta:
    return ApiResponseMeta(
        version="1.0.0",
        request_id=f"req-{uuid4().hex[:12]}",
        timestamp=datetime.now(UTC),
        pagination={"total": total, "page": 1, "per_page": per_page},
    )


def _service(request: Request) -> CalendarService:
    svc = getattr(request.app.state, "calendar_service", None)
    if svc is None:
        svc = get_calendar_service()
        request.app.state.calendar_service = svc
    return svc


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


def _fmt_time(dt: datetime) -> str:
    return dt.strftime("%-H:%M")


def _parse(value: str | None, field: str) -> datetime | None:
    """Parse an ISO-8601 timestamp (``Z``-safe) or 422 — the store would
    otherwise raise and surface as a 500."""
    if value is None:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError) as exc:
        raise HTTPException(
            status_code=422,
            detail=f"{field} is not a valid ISO-8601 timestamp: {value}",
        ) from exc


def _not_found(event_id: str) -> HTTPException:
    return HTTPException(status_code=404, detail=f"Event {event_id} not found.")


# -- request models ----------------------------------------------------------
class EventIngestRequest(BaseModel):
    """One normalized event, as posted by a calendar sync (or the user)."""

    event_id: str
    title: str
    description: str | None = None
    start: str | None = None
    end: str | None = None
    all_day: bool | None = None
    location: str | None = None
    participants: list[str] = Field(default_factory=list)
    reminder_at: str | None = None
    source: str = "calendar-sync"
    sync_id: str | None = None


class WindowRequest(BaseModel):
    start: str
    end: str


class FreeTimeRequest(BaseModel):
    start: str
    duration_minutes: int = Field(default=60, ge=1)
    horizon_minutes: int | None = Field(default=None, ge=60)


class SuggestRequest(BaseModel):
    """§43: 'Dinner tomorrow?' → 'I can do after 19:30'."""

    start: str
    duration_minutes: int = Field(default=60, ge=1)


# -- routes -----------------------------------------------------------------
@router.get("", response_model=ListResponse)
@router.get("/", response_model=ListResponse)
async def list_events(
    request: Request,
    start: str | None = Query(default=None),
    end: str | None = Query(default=None),
    limit: int = Query(default=200, ge=1),
) -> ListResponse:
    """Upcoming events in [start, end) — defaults to the next 7 days."""
    svc = _service(request)
    ws = _parse(start, "start")
    we = _parse(end, "end")
    events = svc.list_events(start=ws, end=we, limit=limit)
    return ListResponse(
        data=[e.model_dump(mode="json") for e in events],
        meta=_meta(total=len(events), per_page=limit),
    )


@router.post("/ingest", response_model=CalendarEvent)
async def ingest_event(
    request: Request, payload: EventIngestRequest, response: Response
) -> CalendarEvent:
    """Upsert one event by id (idempotent sync replay → 200; new → 201)."""
    svc = _service(request)
    p = payload.model_dump()
    # Validate timestamps up front so a malformed date is a 422, not a 500.
    for field in ("start", "end", "reminder_at"):
        raw = p.get(field)
        parsed = _parse(raw, field)
        p[field] = parsed.isoformat() if parsed is not None else None
    is_new = svc.get_event(payload.event_id) is None
    event = svc.ingest(p)
    response.status_code = 201 if is_new else 200
    return event


@router.post("/availability", response_model=dict)
async def availability(request: Request, payload: WindowRequest) -> dict[str, Any]:
    """Scheduling guard (§42): does this window conflict, and when is it free?"""
    svc = _service(request)
    start = _parse(payload.start, "start")
    end = _parse(payload.end, "end")
    assert start is not None and end is not None
    result = svc.availability(start, end)
    return {
        "available": result["available"],
        "start": _iso(result["start"]),
        "end": _iso(result["end"]),
        "conflicts": [c.model_dump(mode="json") for c in result["conflicts"]],
        "earliest_free_after": _iso(result["earliest_free_after"]),
    }


@router.post("/free-time", response_model=dict)
async def free_time(request: Request, payload: FreeTimeRequest) -> dict[str, Any]:
    """Earliest start in the horizon where a slot of the given duration fits."""
    svc = _service(request)
    start = _parse(payload.start, "start")
    assert start is not None
    duration = timedelta(minutes=payload.duration_minutes)
    horizon = (
        timedelta(minutes=payload.horizon_minutes)
        if payload.horizon_minutes
        else None
    )
    slot = svc.free_slot(start, duration, horizon=horizon)
    return {
        "start": _iso(start),
        "duration_minutes": payload.duration_minutes,
        "free_at": _iso(slot),
        "found": slot is not None,
    }


@router.post("/suggest", response_model=dict)
async def suggest(request: Request, payload: SuggestRequest) -> dict[str, Any]:
    """§43 conflict-reasoning reply: 'Dinner tomorrow?' → 'after 19:30'."""
    svc = _service(request)
    start = _parse(payload.start, "start")
    assert start is not None
    duration = timedelta(minutes=payload.duration_minutes)
    window = svc.availability(start, start + duration)
    if window["available"]:
        return {
            "available": True,
            "start": _iso(start),
            "earliest_free_after": _iso(start),
            "suggested_at": _iso(start),
            "suggestion": f"Yes — {start:%A %H:%M} works.",
            "conflicts": [],
        }
    earliest = window["earliest_free_after"]
    if earliest.date() == start.date():
        suggestion = f"I can do after {_fmt_time(earliest)}."
    else:
        suggestion = f"I can do after {earliest:%A} {_fmt_time(earliest)}."
    return {
        "available": False,
        "start": _iso(start),
        "earliest_free_after": _iso(earliest),
        "suggested_at": _iso(earliest),
        "suggestion": suggestion,
        "conflicts": [c.model_dump(mode="json") for c in window["conflicts"]],
    }


# -- calendar account management (Personal-Ops Recovery §8 — workstream E) ----
#
# These routes are *additive*; every route above stays byte-compatible.
# They are registered before the ``/{event_id}`` catch-all so the literal
# ``/accounts`` / ``/conflicts`` prefixes win route matching.

#: Provider values accepted for a calendar account (freeze §8).
_CALENDAR_PROVIDERS = ("google", "caldav", "apple")


def _registry(request: Request) -> Any:
    """The durable repository registry, or ``None`` when not configured."""
    return getattr(request.app.state, "repository_registry", None)


def _runner(request: Request) -> CalendarSyncRunner | None:
    """Resolve a sync runner over the durable registry; ``None`` when the
    durable plane is not configured (honest degradation, never a fake run)."""
    registry = _registry(request)
    if registry is None:
        return None
    runner = getattr(request.app.state, "calendar_sync_runner", None)
    if isinstance(runner, CalendarSyncRunner):
        return runner
    # No supervisor installed (e.g. NERVE_CALENDAR_SYNC_ENABLED off): build a
    # one-shot runner over the same in-memory service.
    runner = CalendarSyncRunner(
        registry,
        calendar_service=_service(request),
        transport=getattr(request.app.state, "calendar_transport", None)
        or HttpxTransport(),
    )
    return runner


def _degraded_write(request: Request, detail: str) -> dict[str, Any]:
    """Honest 202 / executed:false degradation (house write contract)."""
    return {
        "accepted": True,
        "executed": False,
        "request_id": f"req-{uuid4().hex[:12]}",
        "timestamp": datetime.now(UTC).isoformat(),
        "detail": detail,
    }


def _account_dict(row: Any) -> dict[str, Any]:
    """Serialize an account row — credentials are never included (they live
    only in the secrets vault; the row carries at most a ``secret_ref``)."""
    sync_state = row.sync_state or {}
    return {
        "id": row.id,
        "provider": row.provider,
        "label": row.label,
        "account_ref": row.account_ref,
        "secret_ref": row.secret_ref,
        "timezone": row.timezone,
        "config": row.config or {},
        "enabled": row.enabled,
        "operational_status": row.operational_status,
        "last_success_at": row.last_success_at.isoformat() if row.last_success_at else None,
        "last_attempt_at": row.last_attempt_at.isoformat() if row.last_attempt_at else None,
        "last_error_code": row.last_error_code,
        "last_error_message": row.last_error_message,
        "last_sync_at": sync_state.get("last_sync_at"),
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def _row_payload(row: Any) -> dict[str, Any]:
    """Shape a durable event row into the in-memory ingest payload (the same
    dual-plane bridge the sync runner uses — keeps both planes in step)."""
    return {
        "event_id": f"{row.account_id}:{row.ical_uid}",
        "title": row.title,
        "description": row.description,
        "start": row.start.isoformat() if (row.start and not row.all_day) else None,
        "end": row.end.isoformat() if (row.end and not row.all_day) else None,
        "all_day": row.all_day,
        "location": row.location,
        "participants": (row.extra or {}).get("attendees") or [],
        "source": f"calendar-sync:{row.timezone or 'utc'}",
        "sync_id": row.ical_uid,
    }


def _durable_event_dict(row: Any) -> dict[str, Any]:
    """A durable event in the in-memory ``CalendarEvent`` shape (so workstream
    L's Today endpoint and the conflict endpoint read one shape)."""
    return {
        "event_id": f"{row.account_id}:{row.ical_uid}",
        "title": row.title,
        "description": row.description,
        "start": row.start.isoformat() if row.start else None,
        "end": row.end.isoformat() if row.end else None,
        "all_day": row.all_day,
        "location": row.location,
        "participants": (row.extra or {}).get("attendees") or [],
        "reminder_at": None,
        "source": f"calendar-sync:{row.timezone or 'utc'}",
        "sync_id": row.ical_uid,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


class AccountCreateRequest(BaseModel):
    """Create a calendar account. Credentials ride in ``credential`` and are
    written to the connector secrets vault (workstream B) — never stored on
    the account row or echoed back."""

    provider: str
    label: str | None = None
    account_ref: str | None = None
    timezone: str = "UTC"
    config: dict[str, Any] = Field(default_factory=dict)
    credential: str | None = None
    enabled: bool = True


class AccountUpdateRequest(BaseModel):
    label: str | None = None
    account_ref: str | None = None
    timezone: str | None = None
    config: dict[str, Any] | None = None
    credential: str | None = None
    enabled: bool | None = None


@router.get("/accounts", response_model=ListResponse)
async def list_accounts(request: Request) -> ListResponse:
    """Connected calendar accounts (200 empty when not configured)."""
    registry = _registry(request)
    if registry is None:
        return ListResponse(data=[], meta=_meta(total=0, per_page=0))
    rows = await registry.calendar.list_accounts()
    data = [_account_dict(r) for r in rows]
    return ListResponse(data=data, meta=_meta(total=len(data), per_page=len(data)))


@router.post("/accounts", status_code=201)
async def create_account(request: Request, body: AccountCreateRequest) -> dict[str, Any]:
    """Connect a calendar account. Credentials are stored in the vault only."""
    if body.provider not in _CALENDAR_PROVIDERS:
        raise HTTPException(
            status_code=422,
            detail=f"provider must be one of {_CALENDAR_PROVIDERS}",
        )
    registry = _registry(request)
    if registry is None:
        return _degraded_write(request, "durable storage is not configured")

    account = await registry.calendar.create_account(
        provider=body.provider,
        label=body.label,
        account_ref=body.account_ref,
        timezone=body.timezone,
        config=body.config,
        enabled=body.enabled,
    )
    if body.credential is not None:
        secret = await registry.connections.upsert_secret(
            kind=SECRET_KIND,
            owner_kind=SECRET_OWNER_KIND,
            owner_id=account.id,
            plaintext=body.credential,
        )
        account = await registry.calendar.update_account(
            account.id, secret_ref=secret.id
        )
    return _account_dict(account)


@router.get("/accounts/{account_id}")
async def get_account(request: Request, account_id: str) -> dict[str, Any]:
    registry = _registry(request)
    if registry is None:
        # House read degradation: unknown id → synthetic representation.
        return {"id": account_id, "resource": "calendar_account"}
    row = await registry.calendar.get_account(account_id)
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={"error_code": "calendar_account_not_found"},
        )
    return _account_dict(row)


@router.patch("/accounts/{account_id}")
async def update_account(
    request: Request, account_id: str, body: AccountUpdateRequest
) -> dict[str, Any]:
    registry = _registry(request)
    if registry is None:
        return _degraded_write(request, "durable storage is not configured")
    row = await registry.calendar.get_account(account_id)
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={"error_code": "calendar_account_not_found"},
        )
    row = await registry.calendar.update_account(
        account_id,
        label=body.label,
        account_ref=body.account_ref,
        timezone=body.timezone,
        config=body.config,
        enabled=body.enabled,
    )
    if body.credential is not None:
        secret = await registry.connections.upsert_secret(
            kind=SECRET_KIND,
            owner_kind=SECRET_OWNER_KIND,
            owner_id=account_id,
            plaintext=body.credential,
        )
        row = await registry.calendar.update_account(account_id, secret_ref=secret.id)
    return _account_dict(row)


@router.delete("/accounts/{account_id}", status_code=204)
async def delete_account(request: Request, account_id: str) -> None:
    registry = _registry(request)
    if registry is None:
        raise HTTPException(
            status_code=404,
            detail={"error_code": "calendar_account_not_found"},
        )
    deleted = await registry.calendar.delete_account(account_id)
    if not deleted:
        raise HTTPException(
            status_code=404,
            detail={"error_code": "calendar_account_not_found"},
        )
    return None


@router.post("/accounts/{account_id}/sync")
async def sync_account_endpoint(
    request: Request, account_id: str
) -> dict[str, Any]:
    """Run one sync pass for the account (both planes). 202/executed:false
    when the durable plane is not configured."""
    runner = _runner(request)
    if runner is None:
        return _degraded_write(request, "durable storage is not configured")
    summary = await runner.sync_account(account_id)
    return summary


@router.get("/accounts/{account_id}/events", response_model=ListResponse)
async def account_events(
    request: Request,
    account_id: str,
    from_: str | None = Query(default=None, alias="from"),
    to: str | None = Query(default=None),
    limit: int = Query(default=200, ge=1),
) -> ListResponse:
    """Durable events for one account in [from, to) — the row workstream L's
    Today endpoint reads (row 7). 200 empty when not configured."""
    registry = _registry(request)
    if registry is None:
        return ListResponse(data=[], meta=_meta(total=0, per_page=0))
    row = await registry.calendar.get_account(account_id)
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={"error_code": "calendar_account_not_found"},
        )
    ws = _parse(from_, "from")
    we = _parse(to, "to")
    rows = await registry.calendar.list_events(
        start=ws, end=we, account_id=account_id, limit=limit
    )
    data = [_durable_event_dict(r) for r in rows]
    return ListResponse(data=data, meta=_meta(total=len(data), per_page=limit))


@router.get("/conflicts", response_model=dict)
async def conflicts(
    request: Request,
    at: str = Query(..., description="ISO-8601 start of the proposed window"),
    duration: int = Query(default=60, ge=1, description="window length, minutes"),
) -> dict[str, Any]:
    """Conflict lookup over *durable* events, answered with the existing
    in-memory reasoning (reused, not reimplemented): the durable rows in the
    window are replayed into the in-memory service (idempotent) and its
    conflict engine is asked."""
    start = _parse(at, "at")
    assert start is not None  # _parse 422s on bad input
    end = start + timedelta(minutes=duration)

    registry = _registry(request)
    durable: list[dict[str, Any]] = []
    if registry is not None:
        rows = await registry.calendar.list_events(
            start=start, end=end, include_deleted=False, limit=500
        )
        for row in rows:
            _service(request).ingest(_row_payload(row))  # keep planes in step
            durable.append(_durable_event_dict(row))
    svc = _service(request)
    availability = svc.availability(start, end)
    return {
        "at": start.isoformat(),
        "end": end.isoformat(),
        "duration_minutes": duration,
        "available": availability.get("available", not durable),
        "conflicts": durable,
        "availability": availability,
    }


@router.get("/{event_id}", response_model=CalendarEvent)
async def get_event(request: Request, event_id: str) -> CalendarEvent:
    svc = _service(request)
    ev = svc.get_event(event_id)
    if ev is None:
        raise _not_found(event_id)
    return ev


@router.delete("/{event_id}", status_code=204)
async def delete_event(request: Request, event_id: str) -> None:
    svc = _service(request)
    if not svc.delete(event_id):
        raise _not_found(event_id)
    return None
