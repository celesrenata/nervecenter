"""Routine Engine — orchestration over the durable routines plane (workstream H).

Composition (spec §9):

- :class:`RoutinesService` — list / schedule-evaluation / check-in /
  complete / sleep-records over the repository, plus the engine semantics
  the frozen column set encodes:

  - **Schedule evaluation** — per-routine wall-clock slots from
    ``schedule.times`` on the scheduled ``schedule.weekdays`` (ISO
    Monday=0 … Sunday=6; an empty list means every day), in the routine's
    own IANA ``schedule.timezone``. Slots are evaluated against the request
    clock: a slot is *due now* inside the trailing
    :data:`DUE_WINDOW_MINUTES` window, and a slot is *passed unmet* when
    its wall-clock time has lapsed on a scheduled day with no completion.
  - **Missed-window handling** — a passed-unmet slot yields ONE gentle
    note, and the window key ``{routine_id}:{YYYY-MM-DD}:{HH:MM}`` is
    merged into ``routines.missed_flags`` (the repo keeps the first
    timestamp per key), so re-evaluating the same window never re-flags
    it. No streak counters, no guilt copy, anywhere.
  - **Wake fallback alarm** — for ``wake`` routines the engine computes a
    DETERMINISTIC clock-based fallback alarm from the schedule (the next
    local slot in the routine's timezone) and persists it to
    ``deterministic_fallback_alarm`` as ``{"at": iso, "timezone": IANA,
    "kind": "clock_fallback", "trigger_level": N}``. It fires via the
    durable attention path only — no HealthKit / device / push dependency.
  - **Attention mirroring** — the escalation (a due-now or passed-unmet
    routine, and the wake fallback alarm) is mirrored into the attention
    plane exactly like :func:`backend.bills.service.BillService.refresh_
    attention`: a durable ``attention_items`` row via ``registry.attention``
    (one item per routine, matched by ``routine:{id}`` source ref so
    re-evaluation updates in place) PLUS an in-memory mirror to
    :mod:`backend.attention` when the durable path is unavailable.
  - **Sleep records + regularity** — explicit records only (``manual`` /
    ``apple_health``); a window with fewer than :data:`SPARSE_SLEEP_RECORDS`
    records reports ``regularity: "unknown"`` — missing data NEVER proves
    asleep or awake.
- :func:`compute_wake_fallback_alarm` — the pure, deterministic wake-alarm
  computation: the next local slot in the routine's timezone at/after
  ``now`` (rolls to later scheduled days/times when all of today's slots
  have passed).
- :class:`RoutinesTodayProvider` + :func:`register_routines_today_provider`
  — the Today ``coming_up`` contributor (upcoming local slots, 48h) and
  ``handled`` contributor (today's completed check-ins). ``needs_you``
  returns ``[]`` by design — the attention mirroring above is the sole
  source for that section (no double-counting). Registration is
  idempotent and follows the exact bills contract.
- :func:`make_wellness_sleep_provider` — factory for the wellness sleep
  seam (frozen by the §10 sleep-seam contract): an async callable
  ``(days) -> list[{bed_at, wake_at, duration_minutes, source}]`` (ISO
  strings; ``wake_at`` / ``duration_minutes`` may be ``None``) built from
  the durable sleep records, or ``[]`` when degraded (no registry /
  no records). The integrator binds it to ``app.state.wellness_sleep_
  provider`` — the wellness plane resolves the seam at request time and
  never imports this package at module level.
"""

from __future__ import annotations

import math
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from typing import Any
from zoneinfo import ZoneInfo

import structlog

from backend.persistence.models.routines import (
    SLEEP_SOURCES,
    RoutineCheckInRow,
    RoutineRow,
    SleepRecordRow,
)
from backend.persistence.repositories.routines_repo import RoutinesRepository

log = structlog.get_logger(__name__)

#: Trailing window in which a schedule slot counts as "due now" (minutes
#: back from the request clock). A slot older than this is passed-unmet.
DUE_WINDOW_MINUTES = 45

#: Trailing window for the Today ``coming_up`` contribution (48h, matching
#: the Today page's frozen window).
COMING_UP_WINDOW = timedelta(hours=48)

#: A sleep query window with fewer than this many records is "unknown"
#: (spec §9: missing data never proves asleep or awake).
SPARSE_SLEEP_RECORDS = 3

#: Sample-stdev gate (minutes) for a "stable" sleep regularity verdict
#: (same gate the wellness trends engine uses).
STABLE_STDEV_MINUTES = 45.0

#: The deterministic wake fallback alarm's trigger level (durable
#: attention path — no device channel).
WAKE_FALLBACK_TRIGGER_LEVEL = 2

#: Trigger level carried by a due-now / passed-unmet routine's attention
#: mirror (routine items are gentle by design).
ROUTINE_TRIGGER_LEVEL = 1

WEEKDAY_LABELS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def _ensure_utc(dt: Any) -> datetime | None:
    """Normalize a datetime/string to UTC (``None`` stays ``None``)."""
    if dt is None:
        return None
    if isinstance(dt, str):
        dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _iso(dt: Any) -> str | None:
    """ISO-8601 for a datetime (UTC-normalized), ``None`` when absent."""
    normalized = _ensure_utc(dt)
    return normalized.isoformat() if normalized is not None else None


def _local_zone(timezone: str | None) -> tzinfo:
    """Resolve an IANA zone name to a :class:`ZoneInfo` (UTC when absent).

    A schedule without a usable timezone still evaluates deterministically
    (in UTC) — the engine never crashes on a missing zone.
    """
    if not timezone:
        return UTC
    try:
        return ZoneInfo(str(timezone))
    except Exception:  # noqa: BLE001 — invalid zone degrades to UTC
        return UTC


def _slot_label(t: time) -> str:
    return f"{t.hour:02d}:{t.minute:02d}"


def _parse_schedule(schedule: dict[str, Any] | None) -> dict[str, Any]:
    """Parse a ``routines.schedule`` JSONB into a normalized structure.

    ``{"times": ["HH:MM"], "weekdays": [0..6], "timezone": "IANA"}``;
    missing ``times`` means no wall-clock slots (nothing to evaluate),
    an empty/absent ``weekdays`` means every day, and a missing
    ``timezone`` degrades to UTC.
    """
    schedule = schedule or {}
    times: list[time] = []
    for raw in schedule.get("times") or []:
        try:
            hh, mm = str(raw).split(":", 1)
            times.append(time(int(hh), int(mm)))
        except (ValueError, TypeError):
            continue
    weekdays: list[int] = []
    for w in schedule.get("weekdays") or []:
        try:
            weekdays.append(int(w) % 7)
        except (ValueError, TypeError):
            continue
    weekdays = sorted(set(weekdays))
    return {
        "times": times,
        "weekdays": weekdays,
        "timezone": schedule.get("timezone"),
    }


def compute_wake_fallback_alarm(
    row: Any, *, now: datetime | None = None
) -> dict[str, Any] | None:
    """Compute the deterministic clock-based wake fallback alarm for a
    ``wake`` routine (spec §9).

    The alarm is the next local wall-clock slot at/after ``now`` in the
    routine's own IANA timezone — the first ``schedule.times`` entry on the
    next scheduled day (ISO weekday, Monday=0; empty ``weekdays`` = every
    day). When every slot today has already passed, the computation rolls
    forward to the next scheduled day. The result is a plain dict shaped
    exactly for the ``routines.deterministic_fallback_alarm`` JSONB::

        {"at": iso8601, "timezone": IANA, "kind": "clock_fallback",
         "trigger_level": N}

    ``None`` when the routine is not a ``wake`` routine, is disabled, or
    carries no usable schedule.
    """
    if getattr(row, "kind", None) != "wake":
        return None
    if not getattr(row, "enabled", True):
        return None
    schedule = _parse_schedule(getattr(row, "schedule", None))
    if not schedule["times"]:
        return None
    now_utc = _ensure_utc(now) or datetime.now(UTC)
    zone = _local_zone(schedule["timezone"])
    weekdays = schedule["weekdays"]
    # Next local slot at/after now: walk up to 7 days from today (local),
    # and within a scheduled day walk the slots in wall-clock order.
    for offset in range(0, 8):
        local_day = (now_utc.astimezone(zone) + timedelta(days=offset)).date()
        if weekdays and (local_day.isoweekday() - 1) not in weekdays:
            continue
        for t in sorted(schedule["times"]):
            slot_local = datetime.combine(local_day, t, tzinfo=zone)
            slot_utc = slot_local.astimezone(UTC)
            if slot_utc >= now_utc:
                return {
                    "at": slot_utc.isoformat(),
                    "timezone": schedule.get("timezone") or "UTC",
                    "kind": "clock_fallback",
                    "trigger_level": WAKE_FALLBACK_TRIGGER_LEVEL,
                }
    return None


def _slot_dict(
    row: Any, slot_local: datetime, slot_utc: datetime, *, state: str
) -> dict[str, Any]:
    """One evaluated schedule slot, shaped for the Today/Today-view consumers.

    ``slot_local`` is the wall-clock slot in the routine's timezone (drives
    the ``time`` label); ``slot_utc`` is its UTC instant (drives ``at``).
    """
    sched = _parse_schedule(getattr(row, "schedule", None))
    label = _slot_label(slot_local.time())
    return {
        "id": f"{row.id}:{label}",
        "kind": "routine",
        "routine_id": row.id,
        "title": row.title,
        "time": label,
        "at": slot_utc.isoformat(),
        # The Today aggregator orders ``coming_up`` by ``deadline_at`` (and
        # Low Energy Mode keeps deadline items only) — same field the bills
        # provider emits.
        "deadline_at": slot_utc.isoformat(),
        "timezone": sched.get("timezone") or "UTC",
        "state": state,
    }


def _slot_window_key(row: Any, slot_utc: datetime, slot_local: datetime) -> str:
    """The one-time missed-window key: ``{routine_id}:{YYYY-MM-DD}:{HH:MM}``
    (the local date + wall-clock slot, in the routine's timezone)."""
    return (
        f"{row.id}:{slot_local.strftime('%Y-%m-%d')}:"
        f"{slot_local.strftime('%H:%M')}"
    )


class RoutinesService:
    """Orchestration over :class:`RoutinesRepository`.

    **No streaks, no guilt** — this class (and the whole routines
    workstream) never counts missed streaks, never scores the user, and
    never asserts asleep/awake from the absence of sleep data. A missed
    window is a single gentle note, and a wake routine always has a
    deterministic clock-based fallback alarm it can rely on.
    """

    def __init__(self, repo: RoutinesRepository) -> None:
        self._repo = repo

    # -- reads ---------------------------------------------------------------

    async def list_routines(self, *, enabled_only: bool = False) -> list[dict[str, Any]]:
        rows = await self._repo.list_routines(enabled_only=enabled_only)
        return [self._routine_dict(r) for r in rows]

    async def get_routine(self, routine_id: str) -> dict[str, Any] | None:
        row = await self._repo.get_routine(routine_id)
        return self._routine_dict(row) if row is not None else None

    def _routine_dict(self, row: RoutineRow) -> dict[str, Any]:
        return {
            "id": row.id,
            "kind": row.kind,
            "title": row.title,
            "schedule": row.schedule or {},
            "enabled": row.enabled,
            "last_completed_at": _iso(row.last_completed_at),
            "deterministic_fallback_alarm": row.deterministic_fallback_alarm or {},
            "missed_flags": row.missed_flags or {},
            "created_at": _iso(row.created_at),
            "updated_at": _iso(row.updated_at),
        }

    # -- schedule evaluation ----------------------------------------------------

    def _slots_for_day(
        self, row: RoutineRow, local_day: date
    ) -> list[tuple[datetime, datetime]]:
        """The routine's wall-clock slots for one local day, as
        ``(slot_local, slot_utc)`` pairs in wall-clock order (``[]`` when
        the day is not scheduled)."""
        sched = _parse_schedule(row.schedule)
        if not sched["times"]:
            return []
        weekdays = sched["weekdays"]
        if weekdays and (local_day.isoweekday() - 1) not in weekdays:
            return []
        zone = _local_zone(sched["timezone"])
        out: list[tuple[datetime, datetime]] = []
        for t in sorted(sched["times"]):
            local = datetime.combine(local_day, t, tzinfo=zone)
            out.append((local, local.astimezone(UTC)))
        return out

    def _windows_for(
        self, row: RoutineRow, *, now_utc: datetime
    ) -> list[dict[str, Any]]:
        """All schedule windows for the routine within reach of ``now_utc``:
        today's slots (due-now or passed-unmet) plus any past slot on a
        scheduled day within :data:`DUE_WINDOW_MINUTES` of the clock. Each
        entry: ``{"routine": row, "slot_local", "slot_utc", "key", "state"}``
        where state is ``"due"`` or ``"missed"``."""
        sched = _parse_schedule(row.schedule)
        if not sched["times"]:
            return []
        zone = _local_zone(sched["timezone"])
        window = timedelta(minutes=DUE_WINDOW_MINUTES)
        out: list[dict[str, Any]] = []
        for offset in range(0, 3):
            local_day = (now_utc.astimezone(zone) + timedelta(days=-offset)).date()
            for slot_local, slot_utc in self._slots_for_day(row, local_day):
                if slot_utc > now_utc:
                    continue
                elapsed = now_utc - slot_utc
                state = "due" if elapsed <= window else "missed"
                if state == "missed" and offset > 0:
                    # A past-day slot outside the trailing due window is
                    # already settled (its one-time note fired — or the
                    # window rolled past — when it lapsed); never re-flag it
                    # from a later evaluation.
                    continue
                out.append(
                    {
                        "routine": row,
                        "slot_local": slot_local,
                        "slot_utc": slot_utc,
                        "key": _slot_window_key(row, slot_utc, slot_local),
                        "state": state,
                    }
                )
        return out

    async def _completed_keys(self, row: RoutineRow, *, now_utc: datetime) -> set[str]:
        """The window keys already settled by check-ins — a completed
        window is never re-flagged.

        A check-in settles the nearest *prior* wall-clock slot of its local
        day (or the first slot of that day when it precedes every slot), in
        the routine's own IANA timezone — the same
        ``{routine}:{local-date}:{HH:MM}`` key the window carries.
        """
        sched = _parse_schedule(row.schedule)
        if not sched["times"]:
            return set()
        zone = _local_zone(sched["timezone"])
        since = now_utc - timedelta(days=3)
        rows = await self._repo.list_check_ins(row.id, since=since)
        keys: set[str] = set()
        for ci in rows:
            at_local = _ensure_utc(ci.at)
            if at_local is None:
                continue
            local = at_local.astimezone(zone)
            day_slots = self._slots_for_day(row, local.date())
            if not day_slots:
                continue
            prior = [sl for sl, _su in day_slots if sl.time() <= local.time()]
            slot_local = prior[-1] if prior else day_slots[0][0]
            keys.add(_slot_window_key(row, slot_local.astimezone(UTC), slot_local))
        return keys

    def _due_now(self, row: RoutineRow, *, now_utc: datetime) -> list[dict[str, Any]]:
        """Slots whose wall-clock time is inside the trailing due window."""
        return [w for w in self._windows_for(row, now_utc=now_utc) if w["state"] == "due"]

    def _passed_unmet(
        self,
        row: RoutineRow,
        *,
        now_utc: datetime,
        completed_keys: set[str],
    ) -> list[dict[str, Any]]:
        """Slots that lapsed on a scheduled day without completion and are
        not already flagged in ``missed_flags`` (the one-time note gate)."""
        flags = row.missed_flags or {}
        out: list[dict[str, Any]] = []
        for w in self._windows_for(row, now_utc=now_utc):
            if w["state"] != "missed":
                continue
            if w["key"] in flags:
                continue  # already flagged once — never re-flag
            if w["key"] in completed_keys:
                continue
            out.append(w)
        return out

    # -- missed-window handling (one-time gentle note) ---------------------------

    async def evaluate(
        self,
        *,
        now: datetime | None = None,
        registry: Any | None = None,
    ) -> dict[str, Any]:
        """Full engine evaluation for the request clock.

        For every enabled routine: (1) persist the deterministic wake
        fallback alarm when the computed next slot moved (``wake`` kind
        only); (2) merge one gentle note per newly-passed-unmet window into
        ``missed_flags``; (3) mirror due-now / passed-unmet / wake-alarm
        state into the durable attention path (in-memory fallback when
        ``registry`` is ``None``). Returns the in-request projection::

            {"due_now": [...], "missed_notes": [...], "upcoming": [...]}

        — ``missed_notes`` holds the notes added by THIS evaluation (never
        repeats a previously-flagged window).
        """
        now_utc = _ensure_utc(now) or datetime.now(UTC)
        rows = await self._repo.list_routines(enabled_only=True)

        due_now: list[dict[str, Any]] = []
        missed_notes: list[dict[str, Any]] = []
        upcoming: list[dict[str, Any]] = []
        attention_targets: list[tuple[RoutineRow, str, str]] = []

        for row in rows:
            # (1) deterministic wake fallback alarm — persist on movement.
            if row.kind == "wake":
                alarm = compute_wake_fallback_alarm(row, now=now_utc)
                current = row.deterministic_fallback_alarm or {}
                if alarm and current.get("at") != alarm["at"]:
                    await self._repo.update_routine(
                        row.id, deterministic_fallback_alarm=alarm
                    )
                    row.deterministic_fallback_alarm = alarm
                    attention_targets.append(
                        (row, "wake_alarm", f"Wake fallback alarm set for {alarm['at']}")
                    )

            # (2) missed windows — one-time gentle note per new window.
            completed_keys = await self._completed_keys(row, now_utc=now_utc)
            for w in self._passed_unmet(
                row, now_utc=now_utc, completed_keys=completed_keys
            ):
                await self._repo.merge_missed_flags(
                    row.id, {w["key"]: now_utc.isoformat()}
                )
                note = self._missed_note(row, w)
                missed_notes.append(note)
                attention_targets.append((row, "missed", note["note"]))

            # (3) due-now + upcoming (48h) slots.
            for w in self._due_now(row, now_utc=now_utc):
                due_now.append(_slot_dict(row, w["slot_local"], w["slot_utc"], state="due"))
                attention_targets.append((row, "due", w["key"]))
            for slot_local, slot_utc in self._upcoming_slots(row, now_utc=now_utc):
                upcoming.append(_slot_dict(row, slot_local, slot_utc, state="upcoming"))

        # (4) attention mirroring — one durable item per routine, in place.
        for row, state, detail in attention_targets:
            await self._refresh_routine_attention(
                row, state=state, detail=detail, now_utc=now_utc, registry=registry
            )

        due_now.sort(key=lambda s: s["at"])
        upcoming.sort(key=lambda s: s["at"])
        return {"due_now": due_now, "missed_notes": missed_notes, "upcoming": upcoming}

    def _upcoming_slots(
        self, row: RoutineRow, *, now_utc: datetime
    ) -> list[tuple[datetime, datetime]]:
        """Local slots strictly after ``now_utc`` within :data:`COMING_UP_WINDOW`
        (the Today ``coming_up`` contribution)."""
        zone = _local_zone(_parse_schedule(row.schedule).get("timezone"))
        out: list[tuple[datetime, datetime]] = []
        for offset in range(0, 3):
            local_day = (now_utc.astimezone(zone) + timedelta(days=offset)).date()
            for slot_local, slot_utc in self._slots_for_day(row, local_day):
                if now_utc < slot_utc <= now_utc + COMING_UP_WINDOW:
                    out.append((slot_local, slot_utc))
        return out

    def _missed_note(self, row: RoutineRow, w: dict[str, Any]) -> dict[str, Any]:
        """The ONE gentle note for a missed window — non-guilt copy only."""
        return {
            "id": w["key"],
            "kind": "routine_missed",
            "routine_id": row.id,
            "title": row.title,
            "window_key": w["key"],
            "slot": _slot_label(w["slot_local"].time()),
            "note": f"You didn't do {row.title} today — no worries.",
            "at": w["slot_utc"].isoformat(),
        }

    # -- attention mirroring (durable first, in-memory fallback) ---------------

    async def _refresh_routine_attention(
        self,
        row: RoutineRow,
        *,
        state: str,
        detail: str,
        now_utc: datetime,
        registry: Any | None,
    ) -> str | None:
        """Create/refresh the routine's attention item (one per routine).

        Durable row first (via ``registry.attention`` — the wave-1 source of
        truth the Today page consumes), matched by the ``routine:{id}``
        source ref so re-evaluation updates the same item in place instead
        of stacking. Falls back to the in-memory
        :mod:`backend.attention` service when the durable path is
        unavailable (the bills C mail-bridge pattern).
        """
        title = f"{row.title}"
        body = f"{detail} (routine: {row.kind}, trigger level {ROUTINE_TRIGGER_LEVEL})"
        source_refs = [f"routine:{row.id}"]
        escalation_policy = {"routines.trigger_level": ROUTINE_TRIGGER_LEVEL}

        item_id: str | None = None
        if registry is not None:
            try:
                rows = await registry.attention.list_attention_items(state="active")
                existing = next(
                    (
                        r
                        for r in rows
                        if f"routine:{row.id}" in list(r.source_refs or [])
                    ),
                    None,
                )
                if existing is None:
                    item_row = await registry.attention.create_attention_item(
                        title=title,
                        body=body,
                        urgency="normal",
                        importance=5,
                        source_refs=source_refs,
                        recommended_action="Complete or skip the routine",
                        escalation_policy=escalation_policy,
                    )
                    item_id = item_row.id
                else:
                    existing.title = title
                    existing.body = body
                    await registry.attention.set_state(existing.id, "active")
                    item_id = existing.id
            except Exception:  # noqa: BLE001 — degrade to in-memory only
                item_id = None
        if item_id is None:
            # The in-memory mirror is a soft side effect (the durable queue
            # is the sole Today source when a registry exists). Import-order
            # hazards (``backend.attention`` re-enters ``backend.api.app``
            # under some import orders) or an absent attention module must
            # never fail the evaluation itself — log and continue with the
            # durable projection only.
            try:
                item_id = self._mirror_in_memory(title, body, detail, source_refs)
            except Exception as exc:  # noqa: BLE001 — mirroring never fails evaluation
                log.warning("routines.attention_mirror_failed", error=str(exc))
        return item_id

    @staticmethod
    def _mirror_in_memory(
        title: str, body: str, detail: str, source_refs: list[str]
    ) -> str:
        """Mirror into the in-memory attention queue (degraded / no registry)."""
        from backend.attention.service import get_attention_service

        service = get_attention_service()
        item = service.create(
            {
                "reason": title,
                "urgency": 0.4,
                "importance": 0.5,
                "due": None,
                "entities": [r.split(":", 1)[-1] for r in source_refs],
                "sources": ["routines"],
                "suggested_action": "Complete or skip the routine",
                "why": [detail],
            }
        )
        return item.attention_id

    # -- check-in / complete -----------------------------------------------------

    async def check_in(
        self,
        routine_id: str,
        *,
        at: datetime | None = None,
        completed: bool = True,
        note: str | None = None,
    ) -> dict[str, Any] | None:
        """Record (or update) the routine's check-in for today — idempotent
        per (routine, UTC day). ``completed`` may be ``False`` for an
        explicit skip (the routine is "handled" either way — no guilt in
        either direction)."""
        row = await self._repo.get_routine(routine_id)
        if row is None:
            return None
        at_utc = _ensure_utc(at) or datetime.now(UTC)
        ci = await self._repo.upsert_check_in(
            routine_id=routine_id,
            at=at_utc,
            completed=completed,
            note=note,
        )
        return self._check_in_dict(ci)

    async def complete(
        self,
        routine_id: str,
        *,
        at: datetime | None = None,
        note: str | None = None,
    ) -> dict[str, Any] | None:
        """Complete the routine: a completed check-in + the completion
        stamp, and — if a missed-window marker exists for the window this
        completion falls in — clear it (a completed window stops being
        "missed"). Returns the routine row dict."""
        row = await self._repo.get_routine(routine_id)
        if row is None:
            return None
        at_utc = _ensure_utc(at) or datetime.now(UTC)
        await self._repo.upsert_check_in(
            routine_id=routine_id, at=at_utc, completed=True, note=note
        )
        await self._repo.touch_completed(routine_id, at_utc)
        # Clear the matching window key(s) from missed_flags (if present).
        # Completing the routine resolves every missed window for the
        # completion's local day (the day the routine was done) — a completed
        # window stops being "missed". The key shape is
        # ``{routine_id}:{local-date}:{slot-HH:MM}``, so match by the
        # ``{routine_id}:{local-date}:`` prefix.
        zone = _local_zone(_parse_schedule(row.schedule).get("timezone"))
        local = at_utc.astimezone(zone)
        day_prefix = f"{row.id}:{local.strftime('%Y-%m-%d')}:"
        flags = dict(row.missed_flags or {})
        cleared = {k for k in flags if k.startswith(day_prefix)}
        if cleared:
            for k in cleared:
                del flags[k]
            await self._repo.update_routine(row.id, missed_flags=flags)
        updated = await self._repo.get_routine(routine_id)
        return self._routine_dict(updated) if updated is not None else None

    def _check_in_dict(self, row: RoutineCheckInRow) -> dict[str, Any]:
        return {
            "id": row.id,
            "routine_id": row.routine_id,
            "at": _iso(row.at),
            "completed": row.completed,
            "note": row.note,
            "created_at": _iso(row.created_at),
        }

    # -- sleep records + regularity ----------------------------------------------

    async def sleep_records(
        self, *, days: int = 14, now: datetime | None = None
    ) -> list[dict[str, Any]]:
        """The windowed sleep records (explicit records only — ``manual`` /
        ``apple_health``)."""
        rows = await self._repo.list_sleep_records(days=days, now=now)
        return [self._sleep_dict(r) for r in rows]

    async def add_sleep_record(
        self,
        *,
        bed_at: datetime,
        wake_at: datetime | None = None,
        source: str = "manual",
        consent_granted: bool = True,
    ) -> dict[str, Any] | None:
        """Store an explicit sleep record (duration computed on write).

        ``source`` must be one of :data:`SLEEP_SOURCES`; ``consent_granted``
        is stored as a durable fact (the endpoint rejects the write when
        consent is absent).
        """
        if source not in SLEEP_SOURCES:
            raise ValueError(f"invalid sleep source: {source!r}")
        row = await self._repo.create_sleep_record(
            bed_at=bed_at,
            wake_at=wake_at,
            source=source,
            consent_granted=consent_granted,
        )
        return self._sleep_dict(row)

    async def sleep_summary(
        self, *, days: int = 14, now: datetime | None = None
    ) -> dict[str, Any]:
        """Sleep regularity over the window: ``{"regularity", "avg_minutes",
        "n"}``. A window with fewer than :data:`SPARSE_SLEEP_RECORDS`
        records reports ``regularity: "unknown"`` — missing data never
        proves asleep or awake (no stdev, no "stable" claim, no sleep
        assertion of any kind)."""
        rows = await self._repo.list_sleep_records(days=days, now=now)
        n = len(rows)
        durations = [
            self._repo.compute_duration_minutes(r.bed_at, r.wake_at) for r in rows
        ]
        durations = [d for d in durations if d is not None]
        if n < SPARSE_SLEEP_RECORDS:
            return {
                "regularity": "unknown",
                "avg_minutes": None,
                "n": n,
                "description": (
                    "Not enough sleep records to say anything — this stays "
                    "unknown until there are at least 3."
                ),
            }
        if not durations:
            return {
                "regularity": "unknown",
                "avg_minutes": None,
                "n": n,
                "description": (
                    "Sleep records exist but no completed sleep durations "
                    "yet — regularity stays unknown."
                ),
            }
        avg = sum(durations) / len(durations)
        stdev = self._sample_stdev(durations)
        regularity = "stable" if stdev <= STABLE_STDEV_MINUTES else "variable"
        return {
            "regularity": regularity,
            "avg_minutes": round(avg),
            "n": n,
            "description": (
                f"{n} records, {regularity} pattern, averaging "
                f"{round(avg)} min of recorded sleep."
            ),
        }

    @staticmethod
    def _sample_stdev(values: list[float]) -> float:
        """Sample standard deviation (0.0 for a single observation)."""
        n = len(values)
        if n < 2:
            return 0.0
        m = sum(values) / n
        return math.sqrt(sum((v - m) ** 2 for v in values) / (n - 1))

    def _sleep_dict(self, row: SleepRecordRow) -> dict[str, Any]:
        return {
            "id": row.id,
            "bed_at": _iso(row.bed_at),
            "wake_at": _iso(row.wake_at),
            "source": row.source,
            "duration_minutes": row.duration_minutes,
            "consent_granted": row.consent_granted,
            "created_at": _iso(row.created_at),
        }

    # -- Today projection ------------------------------------------------------------

    async def today_view(
        self, *, now: datetime | None = None, registry: Any | None = None
    ) -> dict[str, Any]:
        """The routines contribution to the Today page::

            {"due_now": [...], "upcoming": [...], "missed_notes": [...],
             "completed_today": [...], "sleep": {"regularity", "avg_minutes",
             "n"}}

        ``missed_notes`` is the one-time gentle notes (never streaks), and
        ``sleep`` reports ``regularity: "unknown"`` whenever the record
        window is sparse — it never asserts asleep/awake.
        """
        now_utc = _ensure_utc(now) or datetime.now(UTC)
        evaluation = await self.evaluate(now=now_utc, registry=registry)
        rows = await self._repo.list_routines(enabled_only=True)
        completed_today: list[dict[str, Any]] = []
        start_of_day = now_utc.astimezone(UTC).date()
        for row in rows:
            cis = await self._repo.list_check_ins(
                row.id,
                since=datetime.combine(start_of_day, time.min, tzinfo=UTC),
            )
            for ci in cis:
                at_utc = _ensure_utc(ci.at)
                if at_utc is not None and at_utc.astimezone(UTC).date() == start_of_day:
                    completed_today.append(self._check_in_dict(ci))
        completed_today.sort(key=lambda c: c["at"])
        sleep = await self.sleep_summary(now=now_utc)
        return {
            "due_now": evaluation["due_now"],
            "upcoming": evaluation["upcoming"],
            "missed_notes": evaluation["missed_notes"],
            "completed_today": completed_today,
            "sleep": sleep,
        }


# ---------------------------------------------------------------------------
# Today provider (frozen provider protocol — name / needs_you / coming_up /
# handled; ctx exposes .registry and .now)
# ---------------------------------------------------------------------------


def _repo_from_ctx(registry: Any) -> RoutinesRepository:
    """Build a :class:`RoutinesRepository` from a Today ``ctx.registry``.

    ``registry`` is the durable :class:`RepositoryRegistry` (or ``None``,
    handled by the caller). We construct the repo directly from
    ``registry.session_factory`` (the attribute documented on
    ``RepositoryRegistry``) — the routines workstream does not edit
    ``registry.py``.
    """
    return RoutinesRepository(registry.session_factory)


class RoutinesTodayProvider:
    """Today ``coming_up`` / ``handled`` contributor.

    - ``coming_up`` — the routine's NEXT local slot within the 48h window
      (one item per routine — the landing page stays scannable; the full
      schedule remains available via ``GET /api/routines/today``; the shape
      is ``id`` / ``kind`` / ``title`` / ``time`` / ``deadline_at``).
    - ``handled`` — today's completed check-ins (one item per completion).
    - ``needs_you`` — ``[]`` by design: the durable attention mirroring in
      :meth:`RoutinesService._refresh_routine_attention` is the sole source
      for that section (no double-counting — same rule as bills).

    The provider tolerates ``ctx.registry is None`` (returns ``[]``) per the
    frozen :class:`TodaySectionProvider` contract.
    """

    async def name(self) -> str:
        return "routines"

    async def needs_you(self, ctx: Any) -> list[dict[str, Any]]:
        return []

    async def coming_up(self, ctx: Any) -> list[dict[str, Any]]:
        if ctx is None or getattr(ctx, "registry", None) is None:
            return []
        try:
            svc = RoutinesService(_repo_from_ctx(ctx.registry))
            now_utc = _ensure_utc(ctx.now) or datetime.now(UTC)
            rows = await svc._repo.list_routines(enabled_only=True)
            out: list[dict[str, Any]] = []
            for row in rows:
                # One item per routine: the *next* occurrence within the
                # window (slots come back in day-then-time order, so the
                # first is the earliest). A recurring routine has several
                # slots inside 48h, but the Today page shows only the next
                # actionable one.
                slots = svc._upcoming_slots(row, now_utc=now_utc)
                if slots:
                    slot_local, slot_utc = slots[0]
                    out.append(_slot_dict(row, slot_local, slot_utc, state="upcoming"))
            out.sort(key=lambda s: s["at"])
            return out
        except Exception:  # noqa: BLE001 — degrade to no routines (house contract)
            return []

    async def handled(self, ctx: Any) -> list[dict[str, Any]]:
        if ctx is None or getattr(ctx, "registry", None) is None:
            return []
        try:
            svc = RoutinesService(_repo_from_ctx(ctx.registry))
            now_utc = _ensure_utc(ctx.now) or datetime.now(UTC)
            start_of_day = datetime.combine(
                now_utc.astimezone(UTC).date(), time.min, tzinfo=UTC
            )
            rows = await svc._repo.list_routines(enabled_only=True)
            out: list[dict[str, Any]] = []
            for row in rows:
                cis = await svc._repo.list_check_ins(row.id, since=start_of_day)
                for ci in cis:
                    at_utc = _ensure_utc(ci.at)
                    if at_utc is None:
                        continue
                    if at_utc.astimezone(UTC).date() != now_utc.astimezone(UTC).date():
                        continue
                    out.append(
                        {
                            "id": ci.id,
                            "kind": "routine",
                            "routine_id": row.id,
                            "title": row.title,
                            "at": at_utc.isoformat(),
                        }
                    )
            out.sort(key=lambda c: c["at"])
            return out
        except Exception:  # noqa: BLE001 — degrade to no routines (house contract)
            return []


def register_routines_today_provider() -> RoutinesTodayProvider:
    """Register :class:`RoutinesTodayProvider` with the Today aggregator.

    The integrator calls this at startup (``wiring``); the routines
    package never edits ``backend/api/endpoints/today.py``. Idempotent —
    the aggregator dedupes by provider identity.
    """
    from backend.api.endpoints import today

    provider = RoutinesTodayProvider()
    today.register_today_provider(provider)
    return provider


# ---------------------------------------------------------------------------
# Wellness sleep seam (frozen by the §10 sleep-seam contract)
# ---------------------------------------------------------------------------


def make_wellness_sleep_provider(
    registry: Any | None,
) -> Any:
    """Factory for the wellness sleep seam (the §10 sleep-seam contract).

    Returns an async callable ``(days: int) -> list[record]`` where each
    record is ``{"bed_at": iso, "wake_at": iso | None, "duration_minutes":
    int | None, "source": "manual" | "apple_health"}`` — the exact shape the
    wellness trends engine consumes. When ``registry`` is ``None`` (no
    durable store), the callable returns ``[]`` for any window (the
    "degraded" state — the wellness plane renders "no sleep data" and never
    asserts anything).

    The integrator binds the returned callable to
    ``app.state.wellness_sleep_provider``; the wellness endpoint resolves
    the seam at request time (no module-import coupling to this package).
    """
    repo = RoutinesRepository(registry.session_factory) if registry is not None else None

    async def _provider(days: int) -> list[dict[str, Any]]:
        if repo is None:
            return []
        try:
            rows = await repo.list_sleep_records(days=max(1, int(days)))
        except Exception:  # noqa: BLE001 — degrade to "no sleep data"
            return []
        out: list[dict[str, Any]] = []
        for r in rows:
            out.append(
                {
                    "bed_at": _iso(r.bed_at),
                    "wake_at": _iso(r.wake_at),
                    "duration_minutes": r.duration_minutes,
                    "source": r.source,
                }
            )
        return out

    return _provider


__all__ = [
    "RoutinesRepository",
    "RoutinesService",
    "RoutinesTodayProvider",
    "compute_wake_fallback_alarm",
    "make_wellness_sleep_provider",
    "register_routines_today_provider",
]
