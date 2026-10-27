"""Relationships service — the user-controlled People & Relationships
workspace (Personal-Ops Recovery §11, workstream J).

Layers the durable communication plane (workstream F: ``people`` /
``person_identities`` / ``communications`` / ``conversations`` /
``commitments`` / ``followups``) and workstream E's ``calendar_events``
into the frozen §11 surfaces:

- **Remembering** — birthdays (reserved ``people.notes`` sub-key),
  relationship notes and boundaries (``people.notes`` /
  ``people.boundaries`` JSONB), open commitments, and promises made.
- **Unanswered conversations** — a *factual* surfacing: a (person, channel)
  conversation is "unanswered" when its most recent inbound communication is
  at least N days old (default 7, configurable) and no outbound
  communication followed it. A message-counting fact — never a judgment
  about the person.
- **Promises made** — commitments whose source communication was
  *outgoing*. The detection plane already produced the finding on ingest;
  this service only reads and classifies (no re-detection).
- **Follow-up reminders** — open ``followups`` rows, linked to a person and
  their source refs.
- **Social plans** — upcoming calendar events where the person is an
  explicit attendee (``calendar_events.extra["attendees"]`` — captured by
  the workstream-E sync) or the user has marked the event social
  (``extra["social"] = true``). Deliberately no fuzzy description/location
  matching: NerveCenter surfaces what the user's calendar actually says,
  and nothing it guesses (§11: assistance reflects, it does not decide).
- **Reflective assistance (ON REQUEST ONLY)** — :meth:`reflect` returns a
  deterministic, structured, *factual* summary (commitment counts,
  interaction frequency, unanswered items, note/boundary counts) with the
  question echoed back. No inference call, no diagnosis, no good/bad
  vocabulary: NerveCenter supports the user's judgment, it does not replace
  it. There are no scoring fields anywhere in this service.

Reserved JSONB sub-shapes (additive data only — no schema change):

- ``people.notes`` → ``{"notes": [{"at": iso, "text": str}],
  "birthdays": [{"date": "YYYY-MM-DD", "name": str?}]}``. The
  ``birthdays`` key is reserved; a bare list stored at the top level is
  treated as legacy notes and normalized on read.
- ``people.boundaries`` → ``{"items": [{"scope": str, "rule": str,
  "active": bool}]}``. A bare list is treated as legacy and normalized on
  read.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any, Callable

from sqlalchemy import select

from backend.persistence.models.personal import CalendarEventRow, PersonRow

#: Default: a conversation counts as "unanswered" after this many days.
DEFAULT_UNANSWERED_DAYS = 7

#: Social plans look this many days into the future.
SOCIAL_HORIZON_DAYS = 14

#: Interaction window (days) for the reflective summary.
REFLECTION_WINDOW_DAYS = 30

#: Reserved sub-key inside ``people.notes`` holding birthday entries.
BIRTHDAYS_KEY = "birthdays"

#: User-driven marker inside ``calendar_events.extra``: "I mark this social".
SOCIAL_FLAG_KEY = "social"

#: Direction vocabulary actually written to ``communications.direction``.
#: The relay connector stores raw relay values (``inbound``/``outbound``)
#: while normalized writers use the §7 enum values; both families are
#: understood.
_INCOMING_VALUES = frozenset({"incoming", "inbound", "received"})
_OUTGOING_VALUES = frozenset({"outgoing", "outbound", "sent"})


def _now() -> datetime:
    return datetime.now(UTC)


def _utc(value: Any) -> datetime | None:
    """Coerce a stored value to an aware UTC datetime (None passthrough)."""
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value


def _iso(value: Any) -> str | None:
    dt = _utc(value)
    return dt.isoformat() if dt is not None else None


def _age_days(now: datetime, then: datetime) -> int:
    """Whole days elapsed between ``then`` and ``now`` (>= 0, floor)."""
    delta = now - then
    if delta.total_seconds() < 0:
        return 0
    return int(delta.total_seconds() // 86400)


def _is_inbound(direction: Any) -> bool:
    return str(direction or "").strip().lower() in _INCOMING_VALUES


def _is_outbound(direction: Any) -> bool:
    return str(direction or "").strip().lower() in _OUTGOING_VALUES


def _to_dict(row: Any) -> dict[str, Any]:
    return {c.name: getattr(row, c.name) for c in row.__table__.columns}


# ---------------------------------------------------------------------------
# Reserved JSONB sub-shape normalization (people.notes / people.boundaries)
# ---------------------------------------------------------------------------


def normalize_notes(raw: Any) -> dict[str, Any]:
    """Normalize the reserved ``people.notes`` JSONB bag.

    Returns ``{"notes": [...], "birthdays": [...], **other}`` where note
    entries are coerced to ``{"at": iso|None, "text": str}`` and birthday
    entries to ``{"date": "YYYY-MM-DD", "name": str?}``. Unknown top-level
    keys are preserved verbatim (the bag is user data — never discarded);
    a bare list stored at the top level is treated as legacy notes.
    """
    if isinstance(raw, list):
        coerced = [
            {"at": None, "text": str(item).strip()}
            for item in raw
            if isinstance(item, str) and item.strip()
        ]
        return {"notes": coerced, "birthdays": []}
    if not isinstance(raw, dict):
        return {"notes": [], "birthdays": []}

    result: dict[str, Any] = dict(raw)

    notes = raw.get("notes")
    if isinstance(notes, list):
        coerced_notes: list[dict[str, Any]] = []
        for item in notes:
            if isinstance(item, dict):
                text = str(item.get("text") or "").strip()
                if text:
                    at = item.get("at")
                    at = str(at).strip() if at is not None else None
                    coerced_notes.append({"at": at, "text": text})
            elif isinstance(item, str) and item.strip():
                coerced_notes.append({"at": None, "text": item.strip()})
        result["notes"] = coerced_notes
    else:
        result["notes"] = []

    birthdays = raw.get(BIRTHDAYS_KEY)
    coerced_bdays: list[dict[str, Any]] = []
    if isinstance(birthdays, list):
        for item in birthdays:
            if not isinstance(item, dict):
                continue
            d = str(item.get("date") or "").strip()
            if not d:
                continue
            entry: dict[str, Any] = {"date": d}
            name = str(item.get("name") or "").strip()
            if name:
                entry["name"] = name
            coerced_bdays.append(entry)
    result[BIRTHDAYS_KEY] = coerced_bdays
    return result


def next_birthday_occurrence(
    entry: dict[str, Any], *, today: date | None = None
) -> dict[str, Any] | None:
    """The next occurrence of a ``YYYY-MM-DD`` birthday (this or next year).

    Returns ``None`` for unparseable dates — never a guessed birthday.
    """
    d = str(entry.get("date") or "")
    try:
        parsed = date.fromisoformat(d)
    except ValueError:
        return None
    today = today or date.today()
    candidate = parsed
    while candidate < today:
        try:
            candidate = candidate.replace(year=candidate.year + 1)
        except ValueError:  # Feb 29 on a non-leap next year
            candidate = candidate.replace(year=candidate.year + 1, day=28)
    return {
        "date": d,
        "name": entry.get("name"),
        "occurs_on": candidate.isoformat(),
        "in_days": (candidate - today).days,
    }


def normalize_boundaries(raw: Any) -> list[dict[str, Any]]:
    """Normalize ``people.boundaries`` into ``[{scope, rule, active}]``.

    Accepts the reserved ``{"items": [...]}`` bag or a legacy bare list;
    entries may be strings (treated as ``{"rule": str}``).
    """
    if isinstance(raw, dict):
        items = raw.get("items")
        items = items if isinstance(items, list) else []
    elif isinstance(raw, list):
        items = raw
    else:
        items = []
    out: list[dict[str, Any]] = []
    for item in items:
        if isinstance(item, str):
            item = {"rule": item}
        if not isinstance(item, dict):
            continue
        rule = str(item.get("rule") or item.get("text") or "").strip()
        if not rule:
            continue
        out.append(
            {
                "scope": str(item.get("scope") or "general").strip() or "general",
                "rule": rule,
                "active": bool(item.get("active", True)),
            }
        )
    return out


class RelationshipsService:
    """Durable People & Relationships workspace (spec §11).

    Parameters
    ----------
    communications:
        Workstream-F :class:`CommunicationsRepository` (people, identities,
        communications, conversations).
    commitments:
        Workstream-F :class:`CommitmentsRepository` (commitments + followups).
    calendar:
        Workstream-E :class:`CalendarRepository` (read-only event access for
        social plans); optional.
    session_factory:
        The durable session factory (``RepositoryRegistry.session_factory``)
        — used for the single user-driven write this service owns that the
        E-owned repository does not expose: toggling the
        ``extra["social"]`` marker on a calendar event.
    in_memory:
        The in-memory :class:`CommunicationsService` (optional). Used only
        for exact ``person_id``-keyed display-name / identity fallback when
        a durable person row is absent — never for fuzzy matching.
    unanswered_days:
        The N in "unanswered after N days" (default 7).
    now_fn:
        Injectable clock for test determinism.
    """

    def __init__(
        self,
        communications: Any,
        commitments: Any,
        *,
        calendar: Any = None,
        session_factory: Any = None,
        in_memory: Any = None,
        unanswered_days: int = DEFAULT_UNANSWERED_DAYS,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self._communications = communications
        self._commitments = commitments
        self._calendar = calendar
        self._session_factory = session_factory
        self._in_memory = in_memory
        self.unanswered_days = int(unanswered_days)
        self._now_fn = now_fn or _now

    # -- internals ----------------------------------------------------------

    def _now(self) -> datetime:
        return self._now_fn()

    async def _person_names(self) -> dict[str, str]:
        rows = await self._communications.list_people()
        return {row.id: row.display_name for row in rows}

    def _in_memory_person(self, person_id: str) -> Any | None:
        if self._in_memory is None:
            return None
        try:
            return self._in_memory.get_person(person_id)
        except Exception:  # pragma: no cover - defensive seam
            return None

    async def _get_or_create_person(self, person_id: str) -> Any:
        """The durable person row, creating it (from in-memory data when
        present) when it does not exist yet.

        The durable row reuses the canonical ``person_id`` so durable
        rows and records keyed by it (identities, communications,
        commitments) stay linked to the in-memory record.
        """
        row = await self._communications.get_person(person_id)
        if row is not None:
            return row
        mem = self._in_memory_person(person_id)
        display = None
        if mem is not None:
            display = getattr(mem, "display_name", None)
        return await self._communications.create_person(
            id=person_id,
            display_name=display or "Unknown",
            vip=bool(getattr(mem, "is_vip", False)) if mem is not None else False,
        )

    async def ensure_person(self, person_id: str) -> Any:
        """The durable person row, promoting an in-memory-only person.

        Unlike :meth:`_get_or_create_person`, this raises ``KeyError`` when
        the person is unknown to *both* the durable plane and the in-memory
        registry — the frozen ``PATCH /api/people/{id}`` route must 404 on an
        unknown person rather than silently mint an ``Unknown`` row.
        """
        row = await self._communications.get_person(person_id)
        if row is not None:
            return row
        mem = self._in_memory_person(person_id)
        if mem is None:
            raise KeyError(person_id)
        # Promote on the canonical person_id — never a fresh UUID — so the
        # durable row joins the same identity/communication records.
        return await self._communications.create_person(
            id=person_id,
            display_name=getattr(mem, "display_name", None) or "Unknown",
            vip=bool(getattr(mem, "is_vip", False)),
        )

    # -- durable fields: notes / boundaries / birthdays ---------------------

    async def set_notes(
        self,
        person_id: str,
        notes: list[Any],
        *,
        mode: str = "replace",
    ) -> dict[str, Any]:
        """Replace (default) or append relationship notes on the person.

        Entries may be ``{"text": str, "at": iso?}`` or plain strings. The
        reserved ``birthdays`` sub-key is preserved.

        Raises ``KeyError`` when the person is unknown to both planes; an
        in-memory-only person is promoted to durable first.
        """
        person = await self.ensure_person(person_id)
        current = normalize_notes(person.notes)
        existing = current.get("notes", [])
        now = self._now()
        new_entries: list[dict[str, Any]] = []
        for item in notes:
            if isinstance(item, dict):
                text = str(item.get("text") or "").strip()
                if not text:
                    continue
                entry: dict[str, Any] = {"at": _iso(item.get("at")) or now.isoformat(), "text": text}
                new_entries.append(entry)
            elif isinstance(item, str) and item.strip():
                new_entries.append({"at": now.isoformat(), "text": item.strip()})
        if mode == "append":
            merged = existing + new_entries
        else:
            merged = new_entries
        notes_bag = {**{k: v for k, v in current.items() if k not in ("notes",)}, "notes": merged, BIRTHDAYS_KEY: current.get(BIRTHDAYS_KEY, [])}
        await self._set_person_field(person.id, notes_bag, "notes")
        return normalize_notes(notes_bag)

    async def set_boundaries(self, person_id: str, boundaries: list[Any]) -> list[dict[str, Any]]:
        """Replace the person's standing boundaries (reserved sub-shape)."""
        person = await self.ensure_person(person_id)
        items = normalize_boundaries(boundaries)
        await self._set_person_field(person.id, {"items": items}, "boundaries")
        return items

    async def set_vip(self, person_id: str, value: bool) -> bool:
        """Set the durable VIP flag (frozen ``PATCH /api/people/{id}``).

        Raises ``KeyError`` when the person is unknown to both planes; an
        in-memory-only person is promoted to durable first.
        """
        person = await self.ensure_person(person_id)
        await self._set_person_field(person.id, bool(value), "vip")
        return bool(value)

    async def get_birthdays(self, person_id: str) -> list[dict[str, Any]]:
        person = await self._communications.get_person(person_id)
        if person is None:
            return []
        return normalize_notes(person.notes).get(BIRTHDAYS_KEY, [])

    async def set_birthdays(
        self, person_id: str, birthdays: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Replace the reserved ``birthdays`` sub-key (notes preserved)."""
        person = await self.ensure_person(person_id)
        current = normalize_notes(person.notes)
        coerced: list[dict[str, Any]] = []
        for item in birthdays:
            if not isinstance(item, dict):
                continue
            d = str(item.get("date") or "").strip()
            if not d:
                continue
            try:
                date.fromisoformat(d)
            except ValueError:
                continue
            entry: dict[str, Any] = {"date": d}
            name = str(item.get("name") or "").strip()
            if name:
                entry["name"] = name
            coerced.append(entry)
        notes_bag = {**current, BIRTHDAYS_KEY: coerced}
        await self._set_person_field(person.id, notes_bag, "notes")
        return coerced

    async def _set_person_field(
        self, person_id: str, value: Any, field: str
    ) -> None:
        if self._session_factory is None:
            raise ValueError("no durable session factory configured")
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(PersonRow).where(PersonRow.id == person_id).limit(1)
                )
            ).scalar_one_or_none()
            if row is None:
                raise KeyError(person_id)
            setattr(row, field, value)
            await session.commit()

    # -- aggregates ----------------------------------------------------------

    async def _unanswered_entries(
        self, person_id: str | None, now: datetime
    ) -> list[dict[str, Any]]:
        """(person, channel) conversations whose newest inbound message is
        >= ``unanswered_days`` old with no outbound reply after it."""
        threshold = timedelta(days=self.unanswered_days)
        comms = await self._communications.list_communications(
            person_id=person_id, limit=5000
        )
        by_person_channel: dict[tuple[str, str], list[Any]] = {}
        for row in comms:
            if person_id is not None and row.person_id != person_id:
                continue
            by_person_channel.setdefault((row.person_id, row.channel), []).append(row)

        out: list[dict[str, Any]] = []
        for (pid, channel), rows in by_person_channel.items():
            inbound = [r for r in rows if _is_inbound(r.direction)]
            outbound = [r for r in rows if _is_outbound(r.direction)]
            if not inbound:
                continue
            timed_in: list[tuple[datetime, Any]] = []
            for r in inbound:
                at = _utc(r.received_at)
                if at is not None:
                    timed_in.append((at, r))
            if not timed_in:
                continue
            last_in_at, last_in = max(timed_in, key=lambda pair: pair[0])
            replied_after = any(
                (ot := _utc(r.received_at)) is not None and ot >= last_in_at
                for r in outbound
            )
            if replied_after:
                continue
            age = now - last_in_at
            if age < threshold:
                continue
            out.append(
                {
                    "person_id": pid,
                    "channel": channel,
                    "last_inbound_at": last_in_at.isoformat(),
                    "age_days": _age_days(now, last_in_at),
                    "subject": last_in.subject,
                    "content_ref": last_in.content_ref,
                }
            )
        out.sort(key=lambda e: (-e["age_days"], e["person_id"], e["channel"]))
        return out

    async def unanswered_all(self, limit: int = 200) -> list[dict[str, Any]]:
        """Cross-person unanswered list (factual fields only — the subject
        line / content_ref metadata, never body text)."""
        now = self._now()
        entries = await self._unanswered_entries(None, now)
        names = await self._person_names()
        for entry in entries:
            entry["display_name"] = names.get(entry["person_id"])
        return entries[:limit]

    async def _promises(self, *, open_only: bool) -> list[dict[str, Any]]:
        """Commitments sourced from *outgoing* communications (promises
        made). No re-detection — the finding already exists on ingest."""
        commitments = await self._commitments.list_commitments(
            state="open" if open_only else None
        )
        src_ids = [
            c.source_communication_id
            for c in commitments
            if c.source_communication_id
        ]
        directions: dict[str, str] = {}
        if src_ids:
            rows = await self._communications.get_communications_by_ids(src_ids)
            directions = {r.id: r.direction for r in rows}
        names = await self._person_names()
        out: list[dict[str, Any]] = []
        for c in commitments:
            direction = directions.get(c.source_communication_id)
            if not _is_outbound(direction):
                continue
            out.append(
                {
                    "id": c.id,
                    "person_id": c.person_id,
                    "display_name": names.get(c.person_id),
                    "text": c.text,
                    "due_at": _iso(c.due_at),
                    "state": c.state,
                    "source_communication_id": c.source_communication_id,
                    "created_at": _iso(c.created_at),
                }
            )

        def _due_key(item: dict[str, Any]) -> tuple[int, str]:
            return (1 if item["due_at"] is None else 0, item["due_at"] or "")

        out.sort(key=_due_key)
        return out

    async def promises_all(self, open_only: bool = False) -> list[dict[str, Any]]:
        """All promises made (outgoing-sourced commitments), due first."""
        return await self._promises(open_only=open_only)

    async def follow_up_reminders(
        self, person_id: str | None = None
    ) -> list[dict[str, Any]]:
        """Open follow-ups (due first, undated last) with person + source."""
        rows = await self._commitments.list_followups(state="open")
        if person_id is not None:
            rows = [r for r in rows if r.person_id == person_id]
        now = self._now()
        names = await self._person_names()
        out: list[dict[str, Any]] = []
        for r in rows:
            due = _utc(r.due_at)
            out.append(
                {
                    "id": r.id,
                    "person_id": r.person_id,
                    "display_name": names.get(r.person_id),
                    "text": r.text,
                    "due_at": due.isoformat() if due else None,
                    "is_due": bool(due and due <= now),
                    "state": r.state,
                    "linked_refs": list(r.linked_refs or []),
                    "created_at": _iso(r.created_at),
                }
            )
        out.sort(key=lambda i: (i["due_at"] is None, i["due_at"] or ""))
        return out

    async def social_plans(
        self, person_id: str | None = None, days: int = SOCIAL_HORIZON_DAYS
    ) -> list[dict[str, Any]]:
        """Upcoming calendar events that are social for the person.

        A plan is: (a) user-marked (``extra["social"] = true``), or (b) — when
        scoped to one person — the person's identity handle appears verbatim
        in ``extra["attendees"]``. Without a person filter only user-marked
        events are plans. No description/location fuzzy matching.
        """
        if self._calendar is None:
            return []
        now = self._now()
        events = await self._calendar.list_events(
            start=now, end=now + timedelta(days=max(1, days)), limit=500
        )
        handles: set[str] = set()
        if person_id is not None:
            for ident in await self._communications.list_identities(person_id):
                handles.add(str(ident.handle).strip().lower())
            mem = self._in_memory_person(person_id)
            if mem is not None:
                for identity in getattr(mem, "identities", []) or []:
                    handles.add(str(getattr(identity, "value", "")).strip().lower())

        out: list[dict[str, Any]] = []
        for ev in events:
            if ev.deleted:
                continue
            extra = ev.extra or {}
            attendees = [str(a) for a in (extra.get("attendees") or [])]
            marked = extra.get(SOCIAL_FLAG_KEY) is True
            if person_id is not None:
                if not (
                    marked
                    or any(str(a).strip().lower() in handles for a in attendees)
                ):
                    continue
            elif not marked:
                continue
            out.append(
                {
                    "event_id": f"{ev.account_id}:{ev.ical_uid}",
                    "account_id": ev.account_id,
                    "ical_uid": ev.ical_uid,
                    "title": ev.title,
                    "start": _iso(ev.start),
                    "end": _iso(ev.end),
                    "all_day": bool(ev.all_day),
                    "location": ev.location,
                    "participants": attendees,
                    "marked_social": marked,
                    "person_id": person_id,
                }
            )
        out.sort(key=lambda e: (e["start"] is None, e["start"] or "", e["title"]))
        return out

    async def person_profile(self, person_id: str) -> dict[str, Any]:
        """The full factual relationship record for one person.

        ``person`` is ``None`` only when the person is unknown to both the
        durable plane and the in-memory registry (the endpoint answers 404).
        No scoring fields anywhere in the payload.
        """
        now = self._now()
        person = await self._communications.get_person(person_id)
        mem = self._in_memory_person(person_id)
        if person is None and mem is None:
            return {"person": None, "generated_at": now.isoformat()}

        if person is not None:
            display = person.display_name
            vip = bool(person.vip)
            notes_bag = normalize_notes(person.notes)
            boundaries = normalize_boundaries(person.boundaries)
            durable = True
        else:  # pragma: no cover - only when the in-memory seam is provided
            display = getattr(mem, "display_name", None)
            vip = bool(getattr(mem, "is_vip", False))
            notes_bag = {"notes": [], "birthdays": []}
            boundaries = []
            durable = False

        identities = await self._communications.list_identities(person_id)
        if not identities and mem is not None:
            identities = []
        identities_payload: list[dict[str, Any]] = []
        if identities:
            for i in identities:
                identities_payload.append(
                    {
                        "channel": i.channel,
                        "handle": i.handle,
                        "resolved": bool(i.resolved),
                    }
                )
        elif mem is not None:
            for identity in getattr(mem, "identities", []) or []:
                kind = getattr(identity, "kind", None)
                identities_payload.append(
                    {
                        "channel": str(getattr(kind, "value", kind) or "unknown"),
                        "handle": str(getattr(identity, "value", "")),
                        "resolved": True,
                    }
                )

        birthdays = notes_bag.get(BIRTHDAYS_KEY, [])
        birthdays_out = []
        for entry in birthdays:
            occurrence = next_birthday_occurrence(entry, today=now.date())
            if occurrence is not None:
                birthdays_out.append({**entry, **occurrence})
            else:
                birthdays_out.append(entry)

        # Commitments made TO/with this person (open) + promises made (all
        # outgoing-sourced commitments, any state, so "kept" is visible).
        commitments = await self._commitments.list_commitments(
            person_id=person_id, state="open"
        )
        promises = [
            p
            for p in await self._promises(open_only=False)
            if p["person_id"] == person_id
        ]

        unanswered = [
            {k: v for k, v in entry.items() if k != "person_id"}
            for entry in await self._unanswered_entries(person_id, now)
        ]
        follow_ups = await self.follow_up_reminders(person_id=person_id)
        social = await self.social_plans(person_id=person_id)

        profile = {
            "person": {
                "person_id": person_id,
                "display_name": display,
                "is_vip": vip,
                "durable": durable,
            },
            "identities": [
                {"channel": i.channel, "handle": i.handle, "resolved": bool(i.resolved)}
                if hasattr(i, "channel")
                else i
                for i in identities
            ],
            "notes": notes_bag.get("notes", []),
            "boundaries": boundaries,
            "birthdays": birthdays_out,
            "commitments": [
                {
                    "id": c.id,
                    "text": c.text,
                    "due_at": _iso(c.due_at),
                    "state": c.state,
                    "source_communication_id": c.source_communication_id,
                }
                for c in commitments
            ],
            "promises_made": promises,
            "unanswered": unanswered,
            "follow_ups": follow_ups,
            "social_plans": social,
            "generated_at": now.isoformat(),
        }
        return profile

    # -- reflection (on request only) ---------------------------------------

    async def reflect(self, person_id: str, question: str) -> dict[str, Any] | None:
        """A deterministic, factual reflection on the relationship record.

        No inference call, no diagnosis, no good/bad vocabulary. The output
        is the counted record plus the question echoed back, so the user
        keeps the judgment. Returns ``None`` when the person is unknown.
        """
        profile = await self.person_profile(person_id)
        if profile.get("person") is None:
            return None

        commitments = profile["commitments"]
        promises = profile["promises_made"]
        unanswered = profile["unanswered"]

        now = self._now()
        window = timedelta(days=REFLECTION_WINDOW_DAYS)
        recent = 0
        last_contact: datetime | None = None
        comms = await self._communications.list_communications(
            person_id=person_id, limit=2000
        )
        for row in comms:
            at = _utc(row.received_at)
            if at is None:
                continue
            if last_contact is None or at > last_contact:
                last_contact = at
            if (now - at) <= window:
                recent += 1

        open_commitments = [c for c in commitments if c.get("state") == "open"]
        last_contact_at = last_contact.isoformat() if last_contact else None

        facts = {
            "note_count": len(profile["notes"]),
            "active_boundaries": sum(
                1 for b in profile["boundaries"] if b.get("active", True)
            ),
            "birthday_count": len(profile["birthdays"]),
            "open_commitments": len(open_commitments),
            "commitments_done": len(
                [
                    p
                    for p in await self._commitments.list_commitments(
                        person_id=person_id, state="done"
                    )
                ]
            ),
            "promises_made": len(promises),
            "promises_open": len([p for p in promises if p.get("state") == "open"]),
            "promises_done": len([p for p in promises if p.get("state") == "done"]),
            "unanswered_conversations": len(unanswered),
            "open_follow_ups": len(profile["follow_ups"]),
            "upcoming_social_plans": len(profile["social_plans"]),
            "interactions_last_30_days": recent,
            "last_contact_at": last_contact_at,
        }

        name = profile["person"]["display_name"] or "this person"
        summary = [
            f"You have {facts['note_count']} relationship note(s) and "
            f"{facts['active_boundaries']} active boundary(ies) recorded for {name}."
            f" Open commitments to {name}: {facts['open_commitments']}."
            f" Promises you made to {name}: {facts['promises_made']} "
            f"(open: {facts['promises_open']}, done: {facts['promises_done']})."
            f" Conversations with {name} not yet replied to after "
            f"{self.unanswered_days} days: {facts['unanswered_conversations']}."
            f" Interactions in the last {REFLECTION_WINDOW_DAYS} days: "
            f"{facts['interactions_last_30_days']}."
        ]
        if last_contact_at is not None:
            summary.append(f" Last recorded contact: {last_contact_at}.")

        return {
            "person": profile["person"],
            "question": question,
            "facts": facts,
            "summary": summary,
            "note": (
                "These are the recorded facts for this relationship. "
                "NerveCenter supports your judgment — it does not evaluate "
                "this relationship for you."
            ),
            "generated_at": now.isoformat(),
        }

    # -- user-driven writes ---------------------------------------------------

    async def create_reminder(
        self,
        person_id: str,
        text: str,
        *,
        due_at: datetime | None = None,
        source_ref: str | None = None,
    ) -> dict[str, Any]:
        """Create a follow-up reminder linked to the person (+ source ref).

        Raises ``KeyError`` when the person is unknown to both planes; an
        in-memory-only person is promoted to durable first so the reminder
        carries a durable FK (``followups.person_id`` → ``people.id``).
        """
        await self.ensure_person(person_id)
        row = await self._commitments.create_followup(
            text=text,
            person_id=person_id,
            due_at=due_at,
            linked_refs=[source_ref] if source_ref else [],
        )
        return {
            "id": row.id,
            "person_id": row.person_id,
            "text": row.text,
            "due_at": _iso(row.due_at),
            "state": row.state,
            "linked_refs": list(row.linked_refs or []),
            "created_at": _iso(row.created_at),
        }

    async def set_event_social(
        self, event_id: str, value: bool
    ) -> dict[str, Any] | None:
        """User-mark a calendar event as (not) social:
        ``calendar_events.extra["social"]``.

        ``event_id`` is the stable composite ``"<account_id>:<ical_uid>"``
        used by the calendar endpoints. Returns the updated event, or
        ``None`` when the event does not exist.
        """
        if self._session_factory is None:
            raise ValueError("no durable session factory configured")
        account_id, _, ical_uid = event_id.partition(":")
        if not ical_uid:
            raise ValueError(f"invalid event id: {event_id!r}")

        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(CalendarEventRow)
                    .where(
                        CalendarEventRow.account_id == account_id,
                        CalendarEventRow.ical_uid == ical_uid,
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            extra = dict(row.extra or {})
            extra[SOCIAL_FLAG_KEY] = bool(value)
            row.extra = extra
            await session.commit()
            return {
                "event_id": f"{row.account_id}:{row.ical_uid}",
                "title": row.title,
                "start": _iso(row.start),
                "all_day": bool(row.all_day),
                "marked_social": bool(row.extra.get(SOCIAL_FLAG_KEY) is True),
            }


__all__ = [
    "BIRTHDAYS_KEY",
    "DEFAULT_UNANSWERED_DAYS",
    "REFLECTION_WINDOW_DAYS",
    "RelationshipsService",
    "SOCIAL_FLAG_KEY",
    "SOCIAL_HORIZON_DAYS",
    "next_birthday_occurrence",
    "normalize_boundaries",
    "normalize_notes",
]
