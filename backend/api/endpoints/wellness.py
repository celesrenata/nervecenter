"""Wellness Journal endpoints (Personal-Ops Recovery §10 — workstream I).

**Privacy isolation — deliberate design decision:** wellness is private and
user-controlled (spec §10). It registers **no Today provider** — wellness
never surfaces in the Today/Now feed as an action item or projected
section, and nothing here (or in :mod:`backend.wellness`) sends wellness
rows to inference, OmniRoute, or any cloud transport. Unlike
mail/calendar, wellness has *no external-model path at all*. The
import-graph test in ``tests/backend/test_wellness.py`` enforces this.

Contract (frozen routes, ``/api/wellness``):

- ``GET    /entries?from&to``   — windowed list, newest first.
- ``POST   /entries``           — create / upsert by day (one check-in per
  day: re-saving the same day updates the row in place).
- ``PATCH  /entries/{id}``      — field-level update.
- ``DELETE /entries/{id}``     — delete one entry.
- ``DELETE /entries``           — wipe ALL entries (hard delete — the
  §10 "easy to delete" rule).
- ``GET    /trends?days=30``    — deterministic :class:`TrendEngine`
  output (statistics only) + sleep↔mood correlation block.
- ``GET    /export?format&from&to`` — explicit clinician export of the
  windowed raw entries. **403 when ``wellness.enabled=false``** — the
  export is an explicit opt-in gate: tracking can be paused while keeping
  data, and exporting it is a separate, deliberate act. Every export
  writes a stable-code audit row (``wellness_export``).
- ``GET    /status``            — {enabled, paused_until, entry_count,
  last_entry_at, sensitivity, privacy_statement}.
- ``PATCH  /status``            — {enabled?, paused_until?, clear_pause?}.

House degradation: without a durable ``repository_registry`` every route
degrades to an honest 200/empty or disabled state — never a 500, never a
synthetic record. The workstream-H sleep seam (``app.state.
wellness_sleep_provider``, resolved at request time — no module-import
coupling) degrades to an empty "no sleep data" state when absent.
"""

from __future__ import annotations

import csv
import io
import json
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field

from backend.persistence.repositories.wellness_repo import WellnessRepository
from backend.wellness.service import CollectionBlockedError, WellnessService

router = APIRouter(prefix="/api/wellness", tags=["wellness"])

#: Trend window bounds (days).
_TREND_MIN_DAYS = 1
_TREND_MAX_DAYS = 365


# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------


class CheckInRequest(BaseModel):
    """A daily wellness check-in (all fields optional — partial is fine)."""

    at: datetime | None = None
    mood: int | None = Field(default=None, ge=1, le=5)
    sleep_minutes: int | None = Field(default=None, ge=0)
    sleep_consistency: dict[str, Any] | None = None
    energy: int | None = Field(default=None, ge=1, le=10)
    stress: int | None = Field(default=None, ge=1, le=10)
    journal_entry: str | None = None
    observations: dict[str, Any] | None = None


class CheckInPatchRequest(BaseModel):
    """Field-level edit of an existing entry (``None`` = leave as-is)."""

    mood: int | None = Field(default=None, ge=1, le=5)
    sleep_minutes: int | None = Field(default=None, ge=0)
    sleep_consistency: dict[str, Any] | None = None
    energy: int | None = Field(default=None, ge=1, le=10)
    stress: int | None = Field(default=None, ge=1, le=10)
    journal_entry: str | None = None
    observations: dict[str, Any] | None = None


class StatusPatchRequest(BaseModel):
    """Enable/pause control. Unsent fields are left as-is."""

    enabled: bool | None = None
    paused_until: datetime | None = None
    clear_pause: bool = False


# ---------------------------------------------------------------------------
# House plumbing
# ---------------------------------------------------------------------------


def _service(request: Request) -> WellnessService | None:
    """Build the wellness service from the durable registry, or ``None``.

    The sleep seam is resolved **at request time** from app state (no
    module-import coupling to workstream H): an async callable ``(days) ->
    list[record]`` or ``None`` (no sleep data → graceful empty state).
    """
    registry = getattr(request.app.state, "repository_registry", None)
    if registry is None:
        return None
    repo = WellnessRepository(registry.session_factory)
    sleep_provider = getattr(request.app.state, "wellness_sleep_provider", None)
    return WellnessService(repo, sleep_provider=sleep_provider)


def _parse_dt(value: str | None, *, param: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail=f"invalid ISO-8601 date for {param}: {value!r}",
        ) from exc


def _clamp_days(days: int) -> int:
    return max(_TREND_MIN_DAYS, min(_TREND_MAX_DAYS, days))


def _require_entry(entry: dict[str, Any] | None) -> dict[str, Any]:
    if entry is None:
        raise HTTPException(status_code=404, detail="wellness entry not found")
    return entry


# ---------------------------------------------------------------------------
# Entries
# ---------------------------------------------------------------------------


@router.get("/entries")
async def list_entries(
    request: Request,
    from_q: str | None = Query(default=None, alias="from", description="ISO-8601 lower bound"),
    to_q: str | None = Query(default=None, alias="to", description="ISO-8601 upper bound"),
) -> dict[str, Any]:
    """Windowed check-in list, newest first (empty when no data)."""
    svc = _service(request)
    if svc is None:
        return {"data": [], "durable": False}
    rows = await svc.list_entries(
        from_dt=_parse_dt(from_q, param="from"),
        to_dt=_parse_dt(to_q, param="to"),
    )
    return {"data": rows, "durable": True}


@router.post("/entries", status_code=201)
async def create_entry(request: Request, body: CheckInRequest) -> dict[str, Any]:
    """Create the day's check-in — or update it in place (upsert by day)."""
    svc = _service(request)
    if svc is None:
        # House degradation: no durable store → not recorded, said honestly.
        return {"recorded": False, "reason": "no durable store configured"}
    try:
        entry, created = await svc.upsert_checkin(
            at=body.at or datetime.now().astimezone(),
            mood=body.mood,
            sleep_minutes=body.sleep_minutes,
            sleep_consistency=body.sleep_consistency,
            energy=body.energy,
            stress=body.stress,
            journal_entry=body.journal_entry,
            observations=body.observations,
        )
    except CollectionBlockedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"entry": entry, "created": created}


@router.patch("/entries/{entry_id}")
async def update_entry(request: Request, entry_id: str, body: CheckInPatchRequest) -> dict[str, Any]:
    """Field-level update; unknown ids are a 404 (no synthetic rows)."""
    svc = _service(request)
    if svc is None:
        return {"recorded": False, "reason": "no durable store configured"}
    entry = await svc.update_entry(
        entry_id=entry_id,
        mood=body.mood,
        sleep_minutes=body.sleep_minutes,
        sleep_consistency=body.sleep_consistency,
        energy=body.energy,
        stress=body.stress,
        journal_entry=body.journal_entry,
        observations=body.observations,
    )
    return _require_entry(entry)


@router.delete("/entries/{entry_id}")
async def delete_entry(request: Request, entry_id: str) -> dict[str, Any]:
    svc = _service(request)
    if svc is None:
        return {"deleted": 0, "reason": "no durable store configured"}
    deleted = await svc.delete_entry(entry_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="wellness entry not found")
    return {"deleted": 1}


@router.delete("/entries")
async def delete_all_entries(request: Request) -> dict[str, Any]:
    """Wipe ALL wellness entries (hard delete — spec §10, easy to delete)."""
    svc = _service(request)
    if svc is None:
        return {"deleted": 0, "reason": "no durable store configured"}
    deleted = await svc.delete_all()
    return {"deleted": deleted}


# ---------------------------------------------------------------------------
# Trends
# ---------------------------------------------------------------------------


@router.get("/trends")
async def get_trends(
    request: Request,
    days: int = Query(default=30, ge=_TREND_MIN_DAYS, le=_TREND_MAX_DAYS),
) -> dict[str, Any]:
    """Deterministic statistics only (no inference, no diagnosis).

    Sleep duration/consistency merges the workstream-H sleep seam when
    present; without it the sleep section degrades to an empty /
    "no sleep data" state.
    """
    svc = _service(request)
    if svc is None:
        return {
            "window_days": _clamp_days(days),
            "n_entries": 0,
            "trends": {},
            "sleep": {
                "n": 0,
                "avg_minutes": None,
                "stdev_minutes": None,
                "regularity": "unknown",
                "description": "no sleep data",
            },
            "correlation": {
                "metric": "sleep_vs_mood",
                "n": 0,
                "r": None,
                "status": "insufficient",
                "description": "insufficient data",
            },
            "sleep_data_present": False,
            "durable": False,
        }
    return await svc.trends(days=_clamp_days(days))


# ---------------------------------------------------------------------------
# Clinician export (explicit request — opt-in gate)
# ---------------------------------------------------------------------------


def _csv_bytes(rows: list[dict[str, Any]]) -> bytes:
    buf = io.StringIO()
    writer = csv.DictWriter(
        buf,
        fieldnames=[
            "id",
            "at",
            "mood",
            "sleep_minutes",
            "energy",
            "stress",
            "journal_entry",
            "observations",
        ],
        extrasaction="ignore",
    )
    writer.writeheader()
    for row in rows:
        record = dict(row)
        if row.get("observations") is not None:
            record["observations"] = json.dumps(row["observations"], sort_keys=True)
        writer.writerow(record)
    return buf.getvalue().encode("utf-8")


@router.get("/export")
async def export_entries(
    request: Request,
    format: str = Query(default="json", pattern="^(csv|json)$"),
    from_q: str | None = Query(default=None, alias="from", description="ISO-8601 lower bound"),
    to_q: str | None = Query(default=None, alias="to", description="ISO-8601 upper bound"),
) -> Response:
    """Explicit clinician export of the windowed raw entries.

    **Opt-in gate:** returns 403 when the durable
    ``wellness.enabled`` preference is ``false`` — disabling tracking stops
    collection *and* the export; exporting is a separate, deliberate act.
    Every successful export writes a stable-code (``wellness_export``)
    audit row so exports are auditable.
    """
    svc = _service(request)
    if svc is None:
        # House degradation: nothing durable to export — empty, not 500.
        payload = {"format": format, "entries": 0, "rows": [], "window": "*..*"}
        if format == "csv":
            return Response(
                content=_csv_bytes([]),
                media_type="text/csv",
                headers={"Content-Disposition": 'attachment; filename="wellness-export.csv"'},
            )
        return Response(content=json.dumps(payload, indent=2), media_type="application/json")
    result = await svc.export(
        fmt=format,
        from_dt=_parse_dt(from_q, param="from"),
        to_dt=_parse_dt(to_q, param="to"),
    )
    if result is None:
        raise HTTPException(
            status_code=403,
            detail=(
                "wellness tracking is disabled — the clinician export is an "
                "explicit opt-in and requires wellness.enabled=true"
            ),
        )
    disposition = (
        'attachment; filename="wellness-export.csv"'
        if format == "csv"
        else 'attachment; filename="wellness-export.json"'
    )
    if format == "csv":
        return Response(
            content=_csv_bytes(result["rows"]),
            media_type="text/csv",
            headers={"Content-Disposition": disposition},
        )
    return Response(
        content=json.dumps(result, indent=2, default=str),
        media_type="application/json",
        headers={"Content-Disposition": disposition},
    )


# ---------------------------------------------------------------------------
# Status / control
# ---------------------------------------------------------------------------


@router.get("/status")
async def get_status(request: Request) -> dict[str, Any]:
    svc = _service(request)
    if svc is None:
        return {
            "enabled": True,
            "paused_until": None,
            "entry_count": 0,
            "last_entry_at": None,
            "sensitivity": "personal",
            "privacy_statement": (
                "Wellness data stays in NerveCenter and is never sent to "
                "other services or models."
            ),
            "durable": False,
        }
    status = await svc.status()
    status["durable"] = True
    return status


@router.patch("/status")
async def patch_status(request: Request, body: StatusPatchRequest) -> dict[str, Any]:
    """Enable/pause control (durable ``preferences`` table)."""
    svc = _service(request)
    if svc is None:
        return {
            "recorded": False,
            "reason": "no durable store configured",
            **await _degraded_status(),
        }
    status = await svc.set_status(
        enabled=body.enabled,
        paused_until=body.paused_until.isoformat() if body.paused_until else None,
        clear_pause=body.clear_pause,
    )
    return status


async def _degraded_status() -> dict[str, Any]:
    return {
        "enabled": True,
        "paused_until": None,
        "entry_count": 0,
        "last_entry_at": None,
        "sensitivity": "personal",
        "privacy_statement": (
            "Wellness data stays in NerveCenter and is never sent to "
            "other services or models."
        ),
        "durable": False,
    }


# ---------------------------------------------------------------------------
# Sleep seam (workstream H) — registered at request time, never imported
# ---------------------------------------------------------------------------
#
# ``app.state.wellness_sleep_provider`` is an async callable
# ``(days) -> list[{bed_at, wake_at, duration_minutes, source}]`` (or
# ``None`` when H's plane is absent). The integrator may bind it, e.g.::
#
#     async def _sleep_provider(days: int):
#         # call H's sleep store / in-memory service and return records
#         ...
#
# No module-level import of H's sleep endpoint happens in this module —
# the seam is the only coupling, so the wellness import graph stays clean.
