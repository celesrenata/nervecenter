"""Daily Operational State composer (prompt2 §52 — AC-R17, R109).

The ``GET /api/briefing/operational`` route returns the §52 rollup — the
user's current operational picture — as a flat, read-only object consumed by
the ``nerve today`` CLI command (AC-R11) and the §64 ``get_daily_brief`` MCP
tool (AC-R10).

The eight §52 sections, each backed by its *owning* service (R109 step 4 —
"do not invent aggregate data"):

    open commitments     ← detection plane (``commitment`` findings, unfulfilled)
    incoming requests    ← detection plane (``request`` findings, unresolved)
    waiting-ons          ← waiting-on plane (``list_open``)
    upcoming deadlines   ← detection plane (``deadline`` findings, due in future)
    pending approvals    ← policy/approvals service (``services["policy"]``)
    today's calendar     ← calendar plane (events spanning ``now``'s day)
    active incidents     ← incidents service (``services["incidents"]``)
    blocked goals        ← goals service (active goals past their deadline)

Design notes
------------
- **No state of its own.** Like :class:`~backend.briefing.service.BriefingService`
  this is a pure projection. The in-memory planes (detection, waiting-on,
  calendar) are process singletons and are *always* available; the
  persistence-backed services (goals, incidents, approvals) live on
  ``app.state.services`` and are present only when ``NERVE_DB_URL`` is wired.
  When one of those is absent its section renders as an *honest empty list* —
  never invented data (the same tolerance contract the other routers use).
- **Deterministic and clock-injected.** ``now`` (a UTC instant or ISO string,
  defaulting to the wall clock) is the single clock every time-relative
  value is computed from: the "today" calendar window, waiting-on day counts,
  deadline recency, and the blocked-goal over-due test. Pinning ``now`` makes
  the rollup exactly reproducible in tests.
- **Flat wire shape.** The response is *not* wrapped in the §46
  ``ListResponse`` envelope — the CLI reads the bare object (``as_of`` plus the
  eight section lists). Section items are plain dicts projecting the owning
  record's fields; the finding sections intentionally carry only structural
  metadata (id, status, due, ``why``, a short ``matched`` quote) because the
  §7 no-plaintext policy forbids storing message bodies on findings.

Definition choices (documented, deterministic):
- *Open commitment* — a ``commitment`` finding not yet resolved
  (status ``OPEN`` or ``OVERDUE``); a resolved one (``PASSING``) is no longer
  open.
- *Incoming request* — a ``request`` finding still ``OPEN`` (unresolved).
- *Upcoming deadline* — a ``deadline`` finding still ``OPEN`` whose ``due`` is
  at or after ``now`` (a passed deadline has been swept to ``PASSING`` and is
  therefore no longer *upcoming*).
- *Pending approval* — an approval whose state is awaiting a decision
  (``WAITING_FOR_APPROVAL`` / a generic pending marker).
- *Active incident* — an incident in status ``active`` or ``in-progress``
  (``resolved`` / ``closed`` are terminal and excluded).
- *Blocked goal* — a goal still ``active`` (not achieved/abandoned) that has
  passed its deadline; an active goal with no deadline is on track, not
  blocked.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from inspect import isawaitable
from typing import Any

from backend.api.models import OperationalState
from backend.calendar import get_calendar_service
from backend.communications.detection import get_detection_service
from backend.waiting_on import get_waiting_on_service

#: Section list-keys on :class:`OperationalState` that must always be present
#: (even when empty). Used by tests to assert the full §52 shape.
SECTION_KEYS = (
    "open_commitments",
    "incoming_requests",
    "waiting_on",
    "upcoming_deadlines",
    "pending_approvals",
    "calendar_today",
    "active_incidents",
    "blocked_goals",
)


def _ensure_utc(value: datetime | str | None) -> datetime:
    """Coerce a datetime (or ISO-8601 string) to an aware UTC instant.

    Mirrors the briefing plane's ``_ensure_utc``; defaults to the wall clock
    when ``None`` because the rollup always needs a concrete instant.
    """
    if value is None:
        return datetime.now(UTC)
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _iso(dt: datetime | str | None) -> str | None:
    if dt is None:
        return None
    if isinstance(dt, str):
        return dt
    try:
        return dt.astimezone(UTC).isoformat()
    except (ValueError, AttributeError, TypeError):  # noqa: S110
        return str(dt)


def _as_dict(item: Any) -> dict[str, Any]:
    """Project an owning-service record onto a JSON dict (best-effort)."""
    if isinstance(item, dict):
        return dict(item)
    dump = getattr(item, "model_dump", None)
    if callable(dump):
        try:
            payload: Any = dump(mode="json")
        except Exception:  # noqa: BLE001
            payload = dump()
        if isinstance(payload, dict):
            return payload
        return {"value": payload}
    return {"value": item}


async def _first_result(service: Any, method_names: tuple[str, ...]) -> list[Any]:
    """Call the first available no-arg method on ``service`` and return rows.

    The method may be sync or a coroutine; the result is normalized to a list.
    Any failure (missing method, raised store error, ``None``) yields an empty
    list — an unavailable store must render an empty section, not an error.
    """
    for name in method_names:
        method = getattr(service, name, None)
        if method is None:
            continue
        try:
            result = method()
            if isawaitable(result):
                result = await result
        except Exception:  # noqa: BLE001 - an unavailable store must yield an empty section
            return []
        if result is None:
            return []
        if isinstance(result, (list, tuple)):
            return list(result)
        return [result]
    return []


def _status_value(value: Any) -> str:
    """Normalize an enum/str status to its string form."""
    if value is None:
        return ""
    if hasattr(value, "value"):
        return str(value.value)
    return str(value)


def _finding_item(finding: Any) -> dict[str, Any]:
    """Project a detection finding onto an operational-section item.

    Only structural metadata is carried — the §7 no-plaintext policy keeps the
    raw message body out of the rollup. ``source`` is the finding's
    ``created_from`` (its source communication id).
    """
    due = getattr(finding, "due", None)
    return {
        "finding_id": getattr(finding, "finding_id", None),
        "status": _status_value(getattr(finding, "status", None)),
        "due": _iso(due) if hasattr(due, "astimezone") else None,
        "why": list(getattr(finding, "why", []) or []),
        "matched": getattr(finding, "matched", "") or "",
        "confidence": getattr(finding, "confidence", None),
        "source": getattr(finding, "created_from", None),
    }


# -- the eight sections --------------------------------------------------------


def _open_commitments(detection: Any) -> list[dict[str, Any]]:
    """Unfulfilled ``commitment`` findings (OPEN or OVERDUE)."""
    out = []
    for f in detection.all_findings(kind="commitment"):
        if _status_value(getattr(f, "status", None)) in ("OPEN", "OVERDUE"):
            out.append(_finding_item(f))
    return out


def _incoming_requests(detection: Any) -> list[dict[str, Any]]:
    """Unresolved ``request`` findings (still OPEN)."""
    out = []
    for f in detection.all_findings(kind="request"):
        if _status_value(getattr(f, "status", None)) == "OPEN":
            out.append(_finding_item(f))
    return out


def _upcoming_deadlines(detection: Any, now: datetime) -> list[dict[str, Any]]:
    """OPEN ``deadline`` findings whose ``due`` is at/after ``now``."""
    out = []
    for f in detection.all_findings(kind="deadline"):
        if _status_value(getattr(f, "status", None)) != "OPEN":
            continue
        due = getattr(f, "due", None)
        if due is None:
            continue
        if due.tzinfo is None:
            due = due.replace(tzinfo=UTC)
        if due < now:
            continue
        item = _finding_item(f)
        item["due"] = due.astimezone(UTC).isoformat()
        out.append(item)
    return out


def _waiting_on_items(waiting_on: Any, now: datetime) -> list[dict[str, Any]]:
    """OPEN waiting-on requests, with deterministically-computed day counts."""
    out = []
    for item in waiting_on.list_open(now=now):
        days = waiting_on.days_elapsed(item.request_id, now=now)
        out.append(
            {
                "request_id": item.request_id,
                "person": item.person_display_name,
                "what": item.description,
                "days": days if days is not None else 0,
            }
        )
    return out


def _calendar_today(calendar: Any, now: datetime) -> list[dict[str, Any]]:
    """Calendar events that span ``now``'s calendar day (UTC)."""
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)
    out = []
    for ev in calendar.list_events(start=day_start, end=day_end, limit=200):
        out.append(
            {
                "event_id": ev.event_id,
                "title": ev.title,
                "start": _iso(ev.start),
                "end": _iso(ev.end),
                "all_day": ev.all_day,
                "location": ev.location,
            }
        )
    return out


_PENDING_STATES = frozenset(
    {"WAITING_FOR_APPROVAL", "PENDING", "pending", "wait", "waiting"}
)


async def _pending_approvals(services: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Approvals still awaiting a decision (via the ``policy`` service)."""
    svc = services.get("policy") if isinstance(services, Mapping) else None
    if svc is None:
        return []
    result = await _first_result(svc, ("list_approvals", "list"))
    out = []
    for item in result:
        row = _as_dict(item)
        state = row.get("state") or row.get("status") or row.get("decision")
        # An approval store that exposes no state field is authoritative about
        # what it returns; when it *does* expose one, keep only the pending.
        if state is None or _status_value(state) in _PENDING_STATES:
            out.append(row)
    return out


_ACTIVE_INCIDENTS = frozenset({"active", "in-progress"})


async def _active_incidents(services: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Incidents in a non-terminal state (``active`` / ``in-progress``)."""
    svc = services.get("incidents") if isinstance(services, Mapping) else None
    if svc is None:
        return []
    result = await _first_result(svc, ("list_incidents",))
    out = []
    for item in result:
        row = _as_dict(item)
        if _status_value(row.get("status")) in _ACTIVE_INCIDENTS:
            out.append(row)
    return out


async def _blocked_goals(services: Mapping[str, Any], now: datetime) -> list[dict[str, Any]]:
    """Active goals that have passed their deadline."""
    svc = services.get("goals") if isinstance(services, Mapping) else None
    if svc is None:
        return []
    result = await _first_result(svc, ("list_goals", "list"))
    out = []
    for item in result:
        row = _as_dict(item)
        if _status_value(row.get("status")) != "active":
            continue
        deadline = row.get("deadline")
        if deadline is None:
            continue
        dt = _ensure_utc(deadline)
        if dt < now:
            out.append(row)
    return out


# -- the composition ----------------------------------------------------------


async def compose_operational(
    services: Mapping[str, Any] | None = None,
    *,
    now: datetime | str | None = None,
    detection: Any | None = None,
    waiting_on: Any | None = None,
    calendar: Any | None = None,
) -> OperationalState:
    """Compose the §52 Daily Operational State from live plane state.

    ``services`` is the ``app.state.services`` mapping (goals / incidents /
    policy). ``now`` pins the clock for every time-relative section. The
    in-memory planes default to their process singletons and can be injected
    for tests. Returns a fully-materialized :class:`OperationalState`;
    ``empty`` is True when no section carries content.
    """
    clock = _ensure_utc(now)
    services = services or {}
    det = detection if detection is not None else get_detection_service()
    wo = waiting_on if waiting_on is not None else get_waiting_on_service()
    cal = calendar if calendar is not None else get_calendar_service()

    sections = [
        _open_commitments(det),
        _incoming_requests(det),
        _waiting_on_items(wo, clock),
        _upcoming_deadlines(det, clock),
        await _pending_approvals(services),
        _calendar_today(cal, clock),
        await _active_incidents(services),
        await _blocked_goals(services, clock),
    ]
    return OperationalState(
        as_of=clock,
        open_commitments=sections[0],
        incoming_requests=sections[1],
        waiting_on=sections[2],
        upcoming_deadlines=sections[3],
        pending_approvals=sections[4],
        calendar_today=sections[5],
        active_incidents=sections[6],
        blocked_goals=sections[7],
        empty=all(not s for s in sections),
    )
