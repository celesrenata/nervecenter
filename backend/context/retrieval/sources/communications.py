"""Calendar, mail, messages, voicemail, contacts, waiting-on: read through the
same services the screens use (so the Email screen's NerveShield gate applies)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from backend.context.retrieval.intent import Intent
from backend.context.retrieval.sources._util import make, newest
from backend.context.retrieval.types import Record


async def calendar(state: Any, intent: Intent, limit: int) -> list[Record] | None:
    now = intent.now or datetime.now(UTC)
    start, end = intent.window if intent.window else (now - timedelta(days=30), now + timedelta(days=180))
    registry = getattr(state, "repository_registry", None)
    rows: list[Any] = []
    if registry is not None and hasattr(registry, "calendar"):
        rows = list(await registry.calendar.list_events(start=start, end=end, limit=limit))
    else:
        service = getattr(state, "calendar_service", None)
        if service is None:
            return None
        rows = list(service.list_events(start=start, end=end, limit=limit))
    out = []
    for r in rows:
        ref = f"{getattr(r, 'account_id', '')}:{getattr(r, 'ical_uid', None) or getattr(r, 'event_id', '')}"
        parts = [getattr(r, "location", None), getattr(r, "description", None)]
        out.append(make("calendar", ref, r.title, " | ".join(str(p) for p in parts if p), r.start))
    return out


async def mail(state: Any, intent: Intent, limit: int) -> list[Record] | None:
    service = getattr(state, "email_service", None)
    if service is None:
        return None
    out = []
    for t in newest(service.list_threads(), lambda t: t.last_timestamp, limit):
        who = t.sender_name or t.from_address
        out.append(make("mail", t.thread_id, t.subject, f"from {who}: {t.last_body or ''}", t.last_timestamp))
    return out


async def messages(state: Any, intent: Intent, limit: int) -> list[Record] | None:
    service = getattr(state, "messages_service", None)
    if service is None:
        return None
    out = []
    for conv in newest(service.list_conversations(), lambda c: c.last_timestamp, 60):
        who = conv.peer_name or conv.peer_identifier
        for m in service.get_messages(conv.conversation_id)[-30:]:
            direction = "to" if str(getattr(m.direction, "value", m.direction)) == "outbound" else "from"
            out.append(make("messages", m.message_id, f"Text {direction} {who}", m.body, m.timestamp))
    return newest(out, lambda r: r.ts, limit)


async def voicemail(state: Any, intent: Intent, limit: int) -> list[Record] | None:
    service = getattr(state, "voicemail_service", None)
    if service is None:
        return None
    out = []
    for t in newest(service.list_threads(), lambda t: t.last_received_at, 100):
        for v in service.get_voicemails(t.thread_id):
            out.append(make("voicemail", v.voicemail_id, f"Voicemail from {v.caller_name or v.caller_phone}",
                            v.transcript, v.received_at))
    return newest(out, lambda r: r.ts, limit)


async def contacts(state: Any, intent: Intent, limit: int) -> list[Record] | None:
    service = getattr(state, "contacts_service", None)
    if service is None:
        return None
    out = []
    for c in service.list_contacts()[:limit]:
        detail = "; ".join([*c.emails, *c.phones, c.notes or ""]).strip("; ")
        out.append(make("contacts", c.person_id, c.display_name, detail, c.last_updated_at))
    return out


async def waiting_on(state: Any, intent: Intent, limit: int) -> list[Record] | None:
    service = getattr(state, "waiting_on_service", None)
    if service is None:
        return None
    out = []
    for w in service.list_all()[:limit]:
        status = getattr(w.status, "value", w.status)
        out.append(make("waiting_on", w.request_id, f"Waiting on {w.person_display_name}",
                        f"{w.description} ({status})", w.requested_at))
    return out
