"""Calendar, mail, messages, voicemail, contacts, waiting-on: read through the
same services the screens use (so the Email screen's NerveShield gate applies)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from backend.context.retrieval.intent import Intent
from backend.context.retrieval.sources._util import aware, make_all, newest
from backend.context.retrieval.types import Record


_UPCOMING_WORDS = ("upcoming", "next", "coming", "future", "soon", "later")


def _when_text(row: Any, tz: Any = None) -> str:
    """"Mon Nov 20, 10:00-11:00" (or the date alone for all-day events),
    shown in ``tz`` (the user's zone) when given."""
    start, end = getattr(row, "start", None), getattr(row, "end", None)
    if start is None:
        return ""
    if getattr(row, "all_day", False):
        return f"{start:%a %b %d} (all day)"
    if tz is not None:
        start = aware(start).astimezone(tz)
        end = aware(end).astimezone(tz) if end is not None else None
    text = f"{start:%a %b %d, %H:%M}"
    if end is not None:
        text += f"-{end:%H:%M}" if end.date() == start.date() else f" to {end:%a %b %d %H:%M}"
    return text


async def calendar(state: Any, intent: Intent, limit: int) -> list[Record] | None:
    now = intent.now or datetime.now(UTC)
    if intent.window:
        start, end = intent.window
    elif any(w in intent.question.lower() for w in _UPCOMING_WORDS):
        start, end = now.replace(hour=0, minute=0, second=0, microsecond=0), now + timedelta(days=365)
    else:
        start, end = now - timedelta(days=30), now + timedelta(days=180)
    registry = getattr(state, "repository_registry", None)
    rows: list[Any] = []
    if registry is not None and hasattr(registry, "calendar"):
        rows = list(await registry.calendar.list_events(start=start, end=end, limit=limit))
    else:
        service = getattr(state, "calendar_service", None)
        if service is None:
            return None
        rows = list(service.list_events(start=start, end=end, limit=limit))
    rows = [r for r in rows
            if not getattr(r, "deleted", False)
            and str(getattr(r, "status", "") or "").lower() != "cancelled"]
    rows.sort(key=lambda r: (aware(r.start) or datetime.max.replace(tzinfo=UTC)))
    specs: list[tuple[Any, ...]] = []
    for r in rows:
        ref = f"{getattr(r, 'account_id', '')}:{getattr(r, 'ical_uid', None) or getattr(r, 'event_id', '')}"
        parts = [_when_text(r, intent.now.tzinfo if intent.now else None), getattr(r, "location", None), getattr(r, "description", None)]
        specs.append(("calendar", ref, r.title, " | ".join(str(p) for p in parts if p), r.start))
    return await make_all(specs)


async def mail(state: Any, intent: Intent, limit: int) -> list[Record] | None:
    service = getattr(state, "email_service", None)
    if service is None:
        return None
    specs: list[tuple[Any, ...]] = []
    for t in newest(service.list_threads(), lambda t: t.last_timestamp, limit):
        who = t.sender_name or t.from_address
        specs.append(("mail", t.thread_id, t.subject, f"from {who}: {t.last_body or ''}", t.last_timestamp))
    return await make_all(specs)


async def messages(state: Any, intent: Intent, limit: int) -> list[Record] | None:
    service = getattr(state, "messages_service", None)
    if service is None:
        return None
    specs: list[tuple[Any, ...]] = []
    for conv in newest(service.list_conversations(), lambda c: c.last_timestamp, 60):
        who = conv.peer_name or conv.peer_identifier
        for m in service.get_messages(conv.conversation_id)[-30:]:
            direction = "to" if str(getattr(m.direction, "value", m.direction)) == "outbound" else "from"
            specs.append(("messages", m.message_id, f"Text {direction} {who}", m.body, m.timestamp))
    return newest(await make_all(specs), lambda r: r.ts, limit)


async def voicemail(state: Any, intent: Intent, limit: int) -> list[Record] | None:
    service = getattr(state, "voicemail_service", None)
    if service is None:
        return None
    specs: list[tuple[Any, ...]] = []
    for t in newest(service.list_threads(), lambda t: t.last_received_at, 100):
        for v in service.get_voicemails(t.thread_id):
            specs.append(("voicemail", v.voicemail_id, f"Voicemail from {v.caller_name or v.caller_phone}",
                            v.transcript, v.received_at))
    return newest(await make_all(specs), lambda r: r.ts, limit)


async def contacts(state: Any, intent: Intent, limit: int) -> list[Record] | None:
    service = getattr(state, "contacts_service", None)
    if service is None:
        return None
    specs: list[tuple[Any, ...]] = []
    for c in service.list_contacts()[:limit]:
        detail = "; ".join([*c.emails, *c.phones, c.notes or ""]).strip("; ")
        specs.append(("contacts", c.person_id, c.display_name, detail, c.last_updated_at))
    return await make_all(specs)


async def waiting_on(state: Any, intent: Intent, limit: int) -> list[Record] | None:
    service = getattr(state, "waiting_on_service", None)
    if service is None:
        return None
    specs: list[tuple[Any, ...]] = []
    for w in service.list_all()[:limit]:
        status = getattr(w.status, "value", w.status)
        specs.append(("waiting_on", w.request_id, f"Waiting on {w.person_display_name}",
                        f"{w.description} ({status})", w.requested_at))
    return await make_all(specs)
