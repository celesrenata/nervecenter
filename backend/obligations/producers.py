"""Producers that feed the durable obligation plane from live sources.

The bridges in :mod:`backend.obligations.service` only build specs. This
module is the caller side: it decides whether a source record deserves an
obligation, keeps one obligation per source record (a rescheduled event
moves its obligation, it does not add a second one) and retires the
obligation when the source disappears.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from backend.obligations.service import (
    COMING_UP_WINDOW_DAYS,
    ObligationsService,
)
from backend.persistence.repositories.obligations_repo import ObligationsRepository

#: Statuses that still represent open work for a source record.
_OPEN = ("active", "scheduled", "in_progress", "waiting", "snoozed", "stale")


def _as_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


async def _open_rows_for_ref(
    repo: ObligationsRepository, obligation_type: str, plane: str, record_id: str
) -> list[Any]:
    rows = await repo.list(obligation_type=obligation_type, limit=500)
    return [
        r
        for r in rows
        if r.status in _OPEN
        and (r.source_record_ref or {}).get("plane") == plane
        and (r.source_record_ref or {}).get("id") == record_id
    ]


async def bridge_calendar_event(
    registry: Any,
    *,
    account_id: str,
    ical_uid: str,
    title: str,
    start: datetime | None,
    deleted: bool = False,
    status: str = "confirmed",
    appt: bool = True,
    location: str | None = None,
    now: datetime | None = None,
    horizon_days: int = COMING_UP_WINDOW_DAYS,
) -> dict[str, Any]:
    """Create, move or retire the obligation for one calendar event.

    Returns ``{"action": created|merged|moved|retired|skipped, ...}``.
    Source identity is ``calendar:{account_id}:{ical_uid}``, so the same
    event arriving on every sync pass never produces a second row.
    """
    now = _as_utc(now) or datetime.now(UTC)
    start = _as_utc(start)
    record_id = f"{account_id}:{ical_uid}"
    repo = ObligationsRepository(registry.session_factory)
    open_rows = await _open_rows_for_ref(repo, "appointment", "calendar", record_id)

    gone = deleted or status == "cancelled" or not appt or start is None
    if gone or start < now - timedelta(hours=1):
        for row in open_rows:
            await repo.dismiss(row.id)
        return {"action": "retired" if open_rows else "skipped", "count": len(open_rows)}
    if start > now + timedelta(days=horizon_days):
        return {"action": "skipped", "reason": "beyond_horizon"}

    svc = ObligationsService(repo)
    record = {
        "id": record_id,
        "title": title,
        "start": start,
        "prep": [],
        "required_docs": [],
    }
    # A rescheduled event changes the date part of the dedup key. Retire the
    # old row first so the responsibility stays a single obligation.
    new_key = svc_key(record)
    moved = False
    for row in open_rows:
        if row.dedup_key != new_key:
            await repo.dismiss(row.id)
            moved = True
    out = await svc.upsert_from_source(
        {"plane": "calendar", "record": record}, registry=registry
    )
    evidence = {
        "source_kind": "calendar_event",
        "source_id": record_id,
        "snippet": f"{title} at {start.isoformat()}" + (f" ({location})" if location else ""),
    }
    await repo.upsert_by_dedup_key(
        dedup_key=new_key,
        obligation_type="appointment",
        title=f"Appointment: {title}",
        origin="explicit",
        due_at=start,
        evidence=[evidence],
        source_record_ref={"plane": "calendar", "id": record_id},
    )
    action = "moved" if moved else ("created" if out.get("created") else "merged")
    return {"action": action, "obligation_id": out.get("id")}


def svc_key(record: dict[str, Any]) -> str:
    from backend.obligations.service import build_obligation_from_appointment

    return build_obligation_from_appointment(record)["dedup_key"]
