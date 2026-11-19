"""Routines for Ask: each routine with its check-in state for one day.
Reads through ``RoutinesService`` (the service behind the Routines screen and Today page). The
day is the first day of the question's window, or today. Each enabled routine yields one record
saying whether it was completed, skipped, or not checked in that day. Check-in notes are left
out. Mental-health form answers and the other excluded stores are never read.
"""
from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from typing import Any

from backend.context.retrieval.intent import Intent
from backend.context.retrieval.sources._util import aware, make_all
from backend.context.retrieval.types import Record


def _day_start(intent: Intent) -> datetime:
    base = (aware(intent.window[0]) if intent.window else None) or intent.now or datetime.now(UTC)
    return datetime.combine(base.astimezone(UTC).date(), time.min, tzinfo=UTC)


def _schedule_text(schedule: dict[str, Any]) -> str:
    times = schedule.get("times") or []
    return f"scheduled {', '.join(str(t) for t in times)}" if times else "no set time"


async def routines(state: Any, intent: Intent, limit: int) -> list[Record] | None:
    registry = getattr(state, "repository_registry", None)
    if registry is None or not callable(getattr(registry, "session_factory", None)):
        return None
    from backend.persistence.repositories.routines_repo import RoutinesRepository
    from backend.routines.service import RoutinesService
    repo = RoutinesRepository(registry.session_factory)
    svc = RoutinesService(repo)
    start = _day_start(intent)
    end = start + timedelta(days=1)
    day = start.date().isoformat()
    specs: list[tuple[Any, ...]] = []
    for r in await svc.list_routines(enabled_only=True):
        check_ins = [c for c in await repo.list_check_ins(r["id"], since=start)
                     if (at := aware(c.at)) is not None and start <= at < end]
        if any(c.completed for c in check_ins):
            state_text = "completed"
        elif check_ins:
            state_text = "skipped"
        else:
            state_text = "not checked in"
        last = aware(check_ins[-1].at) if check_ins else None
        specs.append(("routines", f"{r['id']}:{day}", f"Routine: {r['title']}",
                      f"{r['kind']}, {_schedule_text(r['schedule'])}, {state_text} on {day}",
                      last or start))
    return await make_all(specs[:limit])
