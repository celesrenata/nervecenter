"""Calendar, mail, messages, voicemail, contacts, waiting-on: read through the
same services the screens use (so the Email screen's NerveShield gate applies)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from backend.context.retrieval.aggregate import StructuredRow
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


def _attendee_list(row: Any) -> list[str]:
    """Every attendee name and email from ``extra`` (uncapped), de-duplicated in order."""
    extra = getattr(row, "extra", None)
    if not isinstance(extra, dict):
        return []
    seen: list[str] = []
    for key in ("attendee_names", "attendees"):
        value = extra.get(key) or []
        for item in (value if isinstance(value, (list, tuple)) else [value]):
            if isinstance(item, dict):
                item = item.get("name") or item.get("email")
            text = str(item or "").strip()
            if text and text not in seen:
                seen.append(text)
    return seen


def _attendees_text(row: Any) -> str:
    """"with Bob Smith, bob@example.com" from ``extra`` attendee names and emails (max 10)."""
    seen = _attendee_list(row)
    return f"with {', '.join(seen[:10])}" if seen else ""


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
        parts = [_when_text(r, intent.now.tzinfo if intent.now else None), getattr(r, "location", None), getattr(r, "description", None),
                 _attendees_text(r)]
        specs.append(("calendar", ref, r.title, " | ".join(str(p) for p in parts if p), r.start))
    return await make_all(specs)


async def calendar_structured(state: Any, intent: Intent, cap: int) -> list[StructuredRow] | None:
    """Events in the intent window (default: next 365 days for "next", else 30 days back to 180
    ahead) with typed ``ts`` (start), ``end`` and ``all_day`` fields, for next appointment,
    meeting counts in a window, and free/busy. Cancelled and deleted events are left out."""
    now = intent.now or datetime.now(UTC)
    op = intent.operator
    if intent.window:
        start, end = intent.window
    elif op is not None and op.kind == "next":
        start, end = now, now + timedelta(days=365)
    else:
        start, end = now - timedelta(days=30), now + timedelta(days=180)
    registry = getattr(state, "repository_registry", None)
    if registry is not None and hasattr(registry, "calendar"):
        rows = list(await registry.calendar.list_events(start=start, end=end, limit=cap))
    else:
        service = getattr(state, "calendar_service", None)
        if service is None:
            return None
        rows = list(service.list_events(start=start, end=end, limit=cap))
    rows = [r for r in rows
            if not getattr(r, "deleted", False)
            and str(getattr(r, "status", "") or "").lower() != "cancelled"]
    rows.sort(key=lambda r: (aware(r.start) or datetime.max.replace(tzinfo=UTC)))
    specs: list[tuple[Any, ...]] = []
    fields: list[dict[str, Any]] = []
    tz = intent.now.tzinfo if intent.now else None
    for r in rows[:cap]:
        ref = f"{getattr(r, 'account_id', '')}:{getattr(r, 'ical_uid', None) or getattr(r, 'event_id', '')}"
        parts = [_when_text(r, tz), getattr(r, "location", None), getattr(r, "description", None),
                 _attendees_text(r)]
        specs.append(("calendar", ref, r.title, " | ".join(str(p) for p in parts if p), r.start))
        fields.append({"status": "scheduled", "ts": aware(r.start), "end": aware(getattr(r, "end", None)),
                       "all_day": bool(getattr(r, "all_day", False)),
                       "attendees": _attendee_list(r),
                       "payee": " ".join(str(p) for p in (r.title, getattr(r, "location", None)) if p)})
    records = await make_all(specs)
    return [StructuredRow(rec, f) for rec, f in zip(records, fields, strict=True)]


calendar.structured = calendar_structured  # type: ignore[attr-defined]  # found by retrieve()


async def mail(state: Any, intent: Intent, limit: int) -> list[Record] | None:
    service = getattr(state, "email_service", None)
    if service is None:
        return None
    specs: list[tuple[Any, ...]] = []
    for t in newest(service.list_threads(), lambda t: t.last_timestamp, limit):
        who = t.sender_name or t.from_address
        specs.append(("mail", t.thread_id, t.subject, f"from {who}: {t.last_body or ''}", t.last_timestamp))
    return await make_all(specs)


async def mail_structured(state: Any, intent: Intent, cap: int) -> list[StructuredRow] | None:
    """Every thread in the mailbox window (up to ``cap``, newest first) with typed fields, so
    counts (unread) and "latest from X" cover more than the newest threads. Participants and
    subject are in ``payee``/title/snippet for name matching. Bodies are the service's own
    ``last_body``, so a message held by NerveShield stays a "Held by NerveShield" notice."""
    service = getattr(state, "email_service", None)
    if service is None:
        return None
    specs: list[tuple[Any, ...]] = []
    fields: list[dict[str, Any]] = []
    for t in newest(service.list_threads(), lambda t: t.last_timestamp, cap):
        who = t.sender_name or t.from_address
        participants = " ".join(p for p in (t.from_address, getattr(t, "to_address", None)) if p)
        unread = int(getattr(t, "unread", 0) or 0)
        state_text = f"{unread} unread" if unread else "read"
        specs.append(("mail", t.thread_id, t.subject,
                      f"from {who} ({participants}), {state_text}: {t.last_body or ''}", t.last_timestamp))
        fields.append({"status": "unread" if unread else "read", "ts": t.last_timestamp,
                       "payee": f"{who} {participants}", "unread": unread})
    records = await make_all(specs)
    return [StructuredRow(r, f) for r, f in zip(records, fields, strict=True)]
mail.structured = mail_structured  # type: ignore[attr-defined]  # found by retrieve()
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


async def messages_structured(state: Any, intent: Intent, cap: int) -> list[StructuredRow] | None:
    """Every text in the newest conversations (up to ``cap``) with typed ``ts``, ``direction`` and
    ``payee`` (the other party), so "last text from X" and counts cover all of them."""
    service = getattr(state, "messages_service", None)
    if service is None:
        return None
    specs: list[tuple[Any, ...]] = []
    fields: list[dict[str, Any]] = []
    for conv in newest(service.list_conversations(), lambda c: c.last_timestamp, cap):
        who = conv.peer_name or conv.peer_identifier
        for m in service.get_messages(conv.conversation_id):
            if len(specs) >= cap:
                break
            outbound = str(getattr(m.direction, "value", m.direction)) == "outbound"
            specs.append(("messages", m.message_id, f"Text {'to' if outbound else 'from'} {who}",
                          m.body, m.timestamp))
            fields.append({"status": "sent" if outbound else "received", "ts": aware(m.timestamp),
                           "direction": "outbound" if outbound else "inbound",
                           "payee": f"{who} {conv.peer_identifier}"})
    records = await make_all(specs)
    return [StructuredRow(r, f) for r, f in zip(records, fields, strict=True)]


messages.structured = messages_structured  # type: ignore[attr-defined]  # found by retrieve()


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


async def voicemail_structured(state: Any, intent: Intent, cap: int) -> list[StructuredRow] | None:
    """Every voicemail (up to ``cap``) with typed ``ts`` and caller, for "voicemail today" counts
    and "who called back" questions."""
    service = getattr(state, "voicemail_service", None)
    if service is None:
        return None
    specs: list[tuple[Any, ...]] = []
    fields: list[dict[str, Any]] = []
    for t in newest(service.list_threads(), lambda t: t.last_received_at, cap):
        for v in service.get_voicemails(t.thread_id):
            if len(specs) >= cap:
                break
            who = v.caller_name or v.caller_phone
            specs.append(("voicemail", v.voicemail_id, f"Voicemail from {who}", v.transcript, v.received_at))
            fields.append({"status": "received", "ts": aware(v.received_at),
                           "payee": f"{who} {v.caller_phone or ''}".strip()})
    records = await make_all(specs)
    return [StructuredRow(r, f) for r, f in zip(records, fields, strict=True)]


voicemail.structured = voicemail_structured  # type: ignore[attr-defined]  # found by retrieve()


async def contacts_structured(state: Any, intent: Intent, cap: int) -> list[StructuredRow] | None:
    """Every contact (up to ``cap``) with typed ``emails``, ``phones``, ``is_vip`` and ``payee``
    (display name), so a person's phone or email is a field, not a text match."""
    service = getattr(state, "contacts_service", None)
    if service is None:
        return None
    specs: list[tuple[Any, ...]] = []
    fields: list[dict[str, Any]] = []
    for c in service.list_contacts()[:cap]:
        detail = "; ".join([*c.emails, *c.phones, c.notes or ""]).strip("; ")
        specs.append(("contacts", c.person_id, c.display_name, detail, c.last_updated_at))
        fields.append({"status": "contact", "ts": aware(c.last_updated_at), "payee": c.display_name,
                       "emails": list(c.emails), "phones": list(c.phones), "is_vip": bool(c.is_vip)})
    records = await make_all(specs)
    return [StructuredRow(r, f) for r, f in zip(records, fields, strict=True)]


async def contacts(state: Any, intent: Intent, limit: int) -> list[Record] | None:
    service = getattr(state, "contacts_service", None)
    if service is None:
        return None
    specs: list[tuple[Any, ...]] = []
    for c in service.list_contacts()[:limit]:
        detail = "; ".join([*c.emails, *c.phones, c.notes or ""]).strip("; ")
        specs.append(("contacts", c.person_id, c.display_name, detail, c.last_updated_at))
    return await make_all(specs)


async def waiting_on_structured(state: Any, intent: Intent, cap: int) -> list[StructuredRow] | None:
    """Every waiting-on request (up to ``cap``) with typed status (open/resolved/cancelled),
    ``ts`` (requested), and ``payee`` (the person), so "who am I waiting on" counts open ones."""
    service = getattr(state, "waiting_on_service", None)
    if service is None:
        return None
    specs: list[tuple[Any, ...]] = []
    fields: list[dict[str, Any]] = []
    for w in service.list_all()[:cap]:
        status = str(getattr(w.status, "value", w.status))
        specs.append(("waiting_on", w.request_id, f"Waiting on {w.person_display_name}",
                      f"{w.description} ({status})", w.requested_at))
        fields.append({"status": "open" if status == "open" else status, "ts": aware(w.requested_at),
                       "payee": w.person_display_name})
    records = await make_all(specs)
    return [StructuredRow(r, f) for r, f in zip(records, fields, strict=True)]


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


contacts.structured = contacts_structured  # type: ignore[attr-defined]  # found by retrieve()
waiting_on.structured = waiting_on_structured  # type: ignore[attr-defined]  # found by retrieve()
