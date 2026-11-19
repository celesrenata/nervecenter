"""Briefing for Ask: the daily briefing and "needs attention" view the Today screen shows.

Reads through ``BriefingService.compose`` (the call behind ``GET /api/briefing``). The briefing
is a read-only projection over the attention, waiting-on, calendar, email, message, and
voicemail planes, so no excluded store is read. Each action, waiting-on item, and event today
becomes one record, and the overnight activity counts become one more.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

from backend.context.retrieval.intent import Intent
from backend.context.retrieval.sources._util import make_all
from backend.context.retrieval.types import Record


log = logging.getLogger(__name__)
async def briefing(state: Any, intent: Intent, limit: int) -> list[Record] | None:
    svc = getattr(state, "briefing_service", None)
    if svc is None:
        return None
    now = (intent.now or datetime.now(UTC)).astimezone(UTC)
    try:
        brief = await asyncio.to_thread(svc.compose, "morning", now=now)
        return await _records(brief, now, limit)
    except Exception as exc:  # noqa: BLE001 - briefing is auxiliary; never fail retrieval
        log.warning("ask.retrieval.briefing_failed error=%s: %s", type(exc).__name__, exc)
        return []
async def _records(brief: Any, now: datetime, limit: int) -> list[Record]:
    day = now.astimezone(UTC).date().isoformat()
    specs: list[tuple[Any, ...]] = []
    for a in brief.actions:
        due = f", due {a.due.isoformat()}" if a.due else ""
        specs.append(("briefing", f"action:{a.attention_id}", f"Needs attention: {a.label}",
                      f"needs action{due}", a.due or brief.generated_at))
    for w in brief.waiting_on:
        specs.append(("briefing", f"waiting:{w.request_id}",
                      f"Waiting on {w.person}: {w.description}",
                      f"waiting {w.days_elapsed} days", brief.generated_at))
    for e in brief.today:
        start = e.start
        specs.append(("briefing", f"event:{e.event_id}", f"Today: {e.title}",
                      f"starts {start.isoformat()}" if start else "today (no start time)",
                      start or brief.generated_at))
    o = brief.overnight
    specs.append(("briefing", f"overnight:{day}", "Overnight activity",
                  f"last 24 hours: {o.emails} email threads, {o.messages} conversations, "
                  f"{o.voicemails} voicemails", brief.generated_at))
    return await make_all(specs[:limit])
