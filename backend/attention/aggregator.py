"""Attention v2 — aggregation / escalation plane (Personal-Ops Recovery §12).

Built on the EXISTING durable attention backbone
(:class:`backend.persistence.repositories.attention_repo.AttentionRepository`)
plus the in-memory :class:`backend.attention.service.AttentionService`.

Design (frozen by spec §12):

- :class:`AttentionAggregator` is constructed per request from
  ``request.app.state`` (request-time resolution — the same pattern the
  wellness sleep provider uses, no startup wiring). It is deliberately
  dependency-light: it holds the registry (or ``None``) and optional
  in-memory source services and never imports an app object.
- :meth:`collect_candidates` produces one candidate per *actionable* source
  (mail, messages, voicemail, calendar, bills, commitments, routines,
  incidents, agent approvals). Every source is wrapped in its own try/except
  so a single source failing never blocks the others (honest degradation).
- Candidates are deduplicated by a **canonical key** (a stable hash of
  ``source_kind`` + sorted ``source_refs``) and upserted into the durable
  ``attention_items`` table: if a durable item with a matching ``source_refs``
  (subset match on the ref that carries the source id) already exists it is
  refreshed in place instead of stacked. Each candidate is also mirrored into
  the in-memory queue.
- **Escalation** is pure, deterministic and DB-free: the item's
  ``escalation_policy`` JSONB holds a time ladder
  ``{"levels": {"normal": "7d", "high": "48h", "critical": "4h"}}`` measured
  relative to the item's ``deadline_at`` (or ``created_at`` when there is no
  deadline). :func:`escalation_level_for` returns the *highest* level whose
  threshold is crossed (baseline ``normal``). :meth:`run_escalation` notifies
  only when ``not repo.already_notified(item, level)`` — the
  ``notification_log`` enforces **one notification per (item, level)**, so the
  same level can never double-notify (the anti-nag guarantee); a *new* level
  after a real clock change may notify once.
- :meth:`transition` performs the user actions (``snooze`` / ``resolve`` /
  ``acknowledge`` / ``suppress`` / ``dismiss``) durably (via the repo) and
  mirrors them in memory. Unknown id → ``None``. When the registry is
  ``None`` it degrades to in-memory only.

This module is read-only with respect to every other package (bills,
routines, persistence): it *imports* their public services and repositories
but edits none of them.
"""

from __future__ import annotations

import hashlib
import inspect
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from backend.bills.service import bill_escalation_level
from backend.persistence.repositories.attention_repo import AttentionRepository

if TYPE_CHECKING:  # pragma: no cover — typing only
    from backend.attention.service import AttentionService

#: ``backend.attention.service`` must not be imported at module import time: it
#: re-enters ``backend.api.app`` (via ``backend.api.models``) which, in turn,
#: imports ``backend.attention`` — a circular import this module must not
#: trigger (the established codebase pattern, cf. ``backend.bills.service``
#: which lazy-imports it the same way). The *instance* is always delegated to
#: the canonical singleton getter on every call (no instance caching) so the
#: aggregator always sees the same singleton the endpoints resolve — and test
#: resets (:func:`backend.attention.reset_attention_service`) take effect.


def _get_service() -> Any:
    """Lazily obtain the process-wide in-memory :class:`AttentionService`."""
    from backend.attention.service import get_attention_service

    return get_attention_service()


#: Source kinds (one candidate per actionable source, deduplicated per kind+ref).
SOURCE_MAIL = "mail"
SOURCE_MESSAGES = "messages"
SOURCE_VOICEMAIL = "voicemail"
SOURCE_CALENDAR = "calendar"
SOURCE_BILLS = "bills"
SOURCE_COMMITMENTS = "commitments"
SOURCE_ROUTINES = "routines"
SOURCE_INCIDENTS = "incidents"
SOURCE_APPROVALS = "approvals"

#: Default time ladder (measured from the item's deadline) when an item has no
#: ``escalation_policy`` of its own — the §12 "not repeated nagging" baseline.
DEFAULT_ESCALATION_LEVELS: dict[str, str] = {"normal": "7d", "high": "48h", "critical": "4h"}

#: Bill proximity ladder → (urgency, importance). Frozen by the contract:
#: {0: 'normal', 1: 'high', 2: 'critical', 3: 'critical'}.
_BILL_URGENCY = {0: "normal", 1: "high", 2: "critical", 3: "critical"}
_BILL_IMPORTANCE = {0: 5, 1: 6, 2: 8, 3: 9}

#: Escalation level → in-memory float urgency (0..1) for the mirror.
_URGENCY_FLOAT = {"low": 0.3, "normal": 0.5, "high": 0.75, "critical": 0.95}

#: Escalation level → durable importance int for the mirror.
_LEVEL_IMPORTANCE = {"normal": 6, "high": 8, "critical": 9}

#: Durable attention state → in-memory AttentionState (the existing endpoint
#: vocabulary). ``dismissed`` mirrors to ``SUPPRESSED`` — the in-memory enum
#: (§25) has no DISMISSED member, and both mean "removed from the live queue
#: at the user's explicit request".
_STATE_TO_STATUS = {
    "active": "NOW",
    "snoozed": "TODAY",
    "resolved": "RESOLVED",
    "dismissed": "SUPPRESSED",
}


# ---------------------------------------------------------------------------
# pure helpers
# ---------------------------------------------------------------------------


def _ensure_utc(value: Any) -> datetime | None:
    """Coerce a datetime / ISO string to a tz-aware UTC datetime (or ``None``)."""
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _attr(obj: Any, name: str, default: Any = None) -> Any:
    """Read ``name`` from a dict, model/dataclass, or ORM row (else default)."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _canonical_key(source_kind: str, source_refs: list[str]) -> str:
    """A stable hash of ``source_kind`` + sorted ``source_refs`` strings.

    Two candidates pointing at the same logical source share a key, so a
    second sync refreshes the existing item instead of stacking a duplicate.
    """
    payload = "\x1f".join([source_kind, *sorted(str(r) for r in source_refs)])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _parse_duration(value: Any) -> timedelta | None:
    """Parse ``"7d"`` / ``"48h"`` / ``"4h"`` / ``"90m"`` into a timedelta.

    Only the single-unit forms the §12 policy JSONB uses are accepted; anything
    unparseable yields ``None`` (the level is simply skipped).
    """
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return timedelta(seconds=float(value))
    if not isinstance(value, str):
        return None
    s = value.strip().lower()
    if not s:
        return None
    unit = s[-1]
    try:
        num = float(s[:-1]) if len(s) > 1 else float(s)
    except ValueError:
        return None
    if unit == "d":
        return timedelta(days=num)
    if unit == "h":
        return timedelta(hours=num)
    if unit == "m":
        return timedelta(minutes=num)
    if unit in ("s", ""):
        return timedelta(seconds=num)
    return None


def _default_levels() -> dict[str, timedelta]:
    parsed: dict[str, timedelta] = {}
    for level, dur in DEFAULT_ESCALATION_LEVELS.items():
        d = _parse_duration(dur)
        if d is not None:
            parsed[level] = d
    return parsed


def _policy_levels(escalation_policy: Any) -> dict[str, timedelta]:
    """Extract ``{level: timedelta}`` from an ``escalation_policy`` JSONB dict.

    Falls back to :data:`DEFAULT_ESCALATION_LEVELS` when the item carries no
    usable ladder, so every item always has a deterministic baseline.
    """
    levels: dict[str, timedelta] = {}
    if isinstance(escalation_policy, dict):
        raw = escalation_policy.get("levels")
        if isinstance(raw, dict):
            for level, dur in raw.items():
                parsed = _parse_duration(dur)
                if parsed is not None:
                    levels[str(level)] = parsed
    if not levels:
        return _default_levels()
    return levels


def escalation_level_for(item: Any, now: datetime, policy: Any = None) -> str:
    """Deterministic escalation level for ``item`` at clock ``now``.

    ``item`` needs ``deadline_at`` (nullable), ``created_at`` and
    ``escalation_policy`` (a dict, ORM row, or anything exposing those attrs).
    ``policy`` (optional) may override ``item.escalation_policy``; when ``None``
    the item's own policy is used, else :data:`DEFAULT_ESCALATION_LEVELS`.

    The level is the **highest** whose threshold is crossed — a threshold is
    the *countdown* to the deadline (or created time) — else the ``normal``
    baseline. This is a pure function: the same clock never yields a different
    answer, which is what makes the anti-nag guarantee testable without a DB.
    """
    now_utc = _ensure_utc(now) or datetime.now(UTC)
    if policy is None:
        policy = _attr(item, "escalation_policy")
    levels = _policy_levels(policy)

    base = _attr(item, "deadline_at") or _attr(item, "created_at")
    base_utc = _ensure_utc(base)
    if base_utc is None:
        # No time anchor at all: nothing to escalate against — stay baseline.
        return "normal"

    # A level is crossed once the time-to-deadline drops to/below its
    # threshold; the highest crossed level wins. Iterating in descending
    # threshold order (normal → high → critical) lets each crossed level
    # overwrite the last, so the most important crossed level remains.
    level = "normal"
    for name, threshold in sorted(levels.items(), key=lambda kv: kv[1], reverse=True):
        if name not in _LEVEL_IMPORTANCE:
            continue
        if (base_utc - now_utc) <= threshold:
            level = name
    return level


def _state_to_status(state: str) -> str:
    return _STATE_TO_STATUS.get(state, "NOW")


def row_to_wire(row: Any) -> dict[str, Any]:
    """Map a durable :class:`AttentionItemRow` onto the in-memory wire shape.

    The durable row stores an int ``importance`` (1..10), a string
    ``urgency`` and a ``state`` string; the wire contract
    (``backend.api.models.AttentionItem``) uses floats in [0, 1], ``due`` and
    the ``AttentionState`` enum vocabulary. This mapper is the single seam so
    durable-only responses (transition/snooze/dismiss) return the *same* wire
    shape as the in-memory queue instead of a raw ORM column dump.

    ``priority`` is derived with the same explainable formula as
    :meth:`backend.attention.service.AttentionService._score` (base +
    deadline proximity + direct-request bonus, clamped to [0, 1]).
    """
    importance_f = min(_attr(row, "importance", 5) / 10.0, 1.0)
    urgency_f = _URGENCY_FLOAT.get(_attr(row, "urgency", "normal"), 0.5)
    # Wire ``due`` mirrors the in-memory contract: a snoozed item's due is its
    # ``snoozed_until`` (when it returns to the queue); otherwise the deadline.
    snoozed_until = _ensure_utc(_attr(row, "snoozed_until"))
    due = snoozed_until or _ensure_utc(_attr(row, "deadline_at"))
    suggested_action = _attr(row, "recommended_action")

    # -- priority, mirroring AttentionService._score -------------------------
    priority = 0.5 * importance_f + 0.5 * urgency_f
    if due is not None:
        hours = (due - datetime.now(UTC)).total_seconds() / 3600.0
        if hours <= 0:
            priority += 0.25
        elif hours <= 12:
            priority += 0.20
        elif hours <= 48:
            priority += 0.12
        else:
            priority += 0.04
    if suggested_action:
        priority += 0.10
    priority = max(0.0, min(1.0, priority))

    refs = list(_attr(row, "source_refs", []) or [])
    return {
        "attention_id": str(_attr(row, "id", "")),
        "reason": _attr(row, "title", ""),
        "priority": round(priority, 4),
        "urgency": urgency_f,
        "importance": importance_f,
        "due": due,
        "entities": [_source_id(r) for r in refs],
        "sources": refs,
        "suggested_action": suggested_action,
        "status": _state_to_status(_attr(row, "state", "active")),
        "why": [],
        "created_at": _attr(row, "created_at"),
        "updated_at": _attr(row, "updated_at"),
    }


def _source_id(ref: str) -> str:
    """The source id carried by a ``kind:id`` reference (the part after ``:``)."""
    return str(ref).split(":", 1)[-1]


def _candidate_payload(candidate: dict[str, Any]) -> dict[str, Any]:
    """Shape an aggregator candidate into the in-memory ``create/refresh`` dict.

    The in-memory model scores a float ``urgency``/``importance`` in [0, 1];
    the durable row stores a string urgency + int importance. Both are kept
    consistent from the candidate's canonical values.
    """
    refs = list(candidate.get("source_refs", []))
    return {
        "reason": candidate["title"],
        "urgency": _URGENCY_FLOAT.get(candidate.get("urgency", "normal"), 0.5),
        "importance": min(candidate.get("importance", 5) / 10.0, 1.0),
        "due": _ensure_utc(candidate.get("deadline_at")),
        "sources": [candidate["source_kind"], *refs],
        "entities": [_source_id(r) for r in refs],
        "suggested_action": candidate.get("recommended_action"),
        "why": list(candidate.get("evidence", [])),
    }


def _latest_message(conv: Any) -> Any:
    """Best-effort latest message of a conversation (message/vmail seam)."""
    if isinstance(conv, dict):
        return conv.get("last_message") or conv.get("message")
    return getattr(conv, "last_message", None) or getattr(conv, "message", None)


async def _call(obj: Any, name: str) -> Any:
    """Call ``obj.<name>()`` (async or sync) when it exists, else ``None``."""
    method = getattr(obj, name, None)
    if method is None or not callable(method):
        return None
    result = method()
    if inspect.isawaitable(result):
        result = await result
    return result


# ---------------------------------------------------------------------------
# aggregator
# ---------------------------------------------------------------------------


class AttentionAggregator:
    """Aggregate the durable + in-memory sources into durable attention items.

    ``app_state`` exposes ``.repository_registry`` (may be ``None``) and may
    also carry the optional in-memory source services. Build it per request
    (``AttentionAggregator(request.app.state)``) — no startup wiring.
    """

    def __init__(
        self,
        app_state: Any,
        *,
        messages_service: Any = None,
        voicemail_service: Any = None,
        incident_service: Any = None,
        approvals_service: Any = None,
    ) -> None:
        self._state = app_state
        registry = getattr(app_state, "repository_registry", None) if app_state else None
        self._registry = registry if registry is not None else None
        self._repo: AttentionRepository | None = (
            getattr(self._registry, "attention", None) if self._registry else None
        )

        # In-memory sources: prefer explicit injection, else app.state binding.
        def _from_state(name: str, default: Any = None) -> Any:
            if default is not None:
                return default
            return getattr(app_state, name, None) if app_state else None

        self._messages = _from_state("messages_service", messages_service)
        self._voicemail = _from_state("voicemail_service", voicemail_service)
        self._incidents = _from_state("incident_service", incident_service)
        self._approvals = _from_state("approvals_service", approvals_service)

    # -- source accessors -----------------------------------------------------

    @property
    def _session_factory(self) -> Any:
        if self._registry is None:
            return None
        return getattr(self._registry, "session_factory", None)

    # -- candidate collection -------------------------------------------------

    async def collect_candidates(self, now: Any = None) -> list[dict[str, Any]]:
        """One candidate per actionable source; a single failure never blocks
        the others (each source is independently guarded)."""
        now_utc = _ensure_utc(now) or datetime.now(UTC)
        collectors = (
            self._candidates_mail,
            self._candidates_messages,
            self._candidates_voicemail,
            self._candidates_calendar,
            self._candidates_bills,
            self._candidates_commitments,
            self._candidates_routines,
            self._candidates_incidents,
            self._candidates_approvals,
        )
        out: list[dict[str, Any]] = []
        for collect in collectors:
            try:
                out.extend(await collect(now_utc))
            except Exception:  # noqa: BLE001 — degrade: one source never blocks others
                continue
        return out

    # -- mail -----------------------------------------------------------------

    async def _candidates_mail(self, now_utc: datetime) -> list[dict[str, Any]]:
        if self._registry is None:
            return []
        comms = await self._registry.communications.list_communications(limit=200)
        out: list[dict[str, Any]] = []
        for comm in comms:
            flags = _attr(comm, "flags")
            if isinstance(flags, (list, tuple, set)):
                has_deadline = "has_deadline" in flags
                has_commitment = "has_commitment" in flags
            elif isinstance(flags, dict):
                has_deadline = bool(flags.get("has_deadline"))
                has_commitment = bool(flags.get("has_commitment"))
            else:
                has_deadline = has_commitment = False
            if not (has_deadline or has_commitment):
                continue  # only "actionable" mail surfaces as a candidate
            received = _ensure_utc(_attr(comm, "received_at")) or now_utc
            deadline = received + timedelta(days=3)
            importance = 7 if has_commitment else 6
            urgency = "high" if (has_deadline and has_commitment) else "normal"
            out.append(
                {
                    "source_kind": SOURCE_MAIL,
                    "source_refs": [f"{SOURCE_MAIL}:{_attr(comm, 'id')}"],
                    "title": f"Mail needs a reply: {_attr(comm, 'subject') or 'new message'}",
                    "body": (
                        f"Received {received.isoformat()} ({_attr(comm, 'channel') or 'mail'})."
                    ),
                    "importance": importance,
                    "urgency": urgency,
                    "deadline_at": deadline,
                    "evidence": [
                        f"communication ref {_attr(comm, 'content_ref') or _attr(comm, 'id')}"
                    ],
                    "recommended_action": "Review the message and reply.",
                    "escalation_policy": dict(DEFAULT_ESCALATION_LEVELS)
                    | {"levels": dict(DEFAULT_ESCALATION_LEVELS)},
                }
            )
        return out

    # -- messages -------------------------------------------------------------

    async def _candidates_messages(self, now_utc: datetime) -> list[dict[str, Any]]:
        svc = self._messages
        if svc is None:
            return []
        conversations = await _call(svc, "list_conversations")
        if conversations is None:
            conversations = await _call(svc, "list")
        if not isinstance(conversations, list):
            return []
        out: list[dict[str, Any]] = []
        for conv in conversations:
            last_message = _latest_message(conv)
            if last_message is None:
                continue
            unread = _attr(conv, "unread_count")
            needs_reply = _attr(conv, "needs_reply")
            if not (unread or needs_reply):
                continue
            conv_id = _attr(conv, "id") or _attr(last_message, "id")
            updated = _ensure_utc(_attr(conv, "last_message_at")) or now_utc
            out.append(
                {
                    "source_kind": SOURCE_MESSAGES,
                    "source_refs": [f"{SOURCE_MESSAGES}:{conv_id}"],
                    "title": f"Message from {_attr(last_message, 'sender') or 'a contact'}",
                    "body": f"{_attr(last_message, 'content') or 'New message in your inbox.'}",
                    "importance": 6,
                    "urgency": "high" if needs_reply else "normal",
                    "deadline_at": updated + timedelta(hours=24),
                    "evidence": [f"conversation {conv_id} unread={unread}"],
                    "recommended_action": "Read and reply to the message.",
                    "escalation_policy": dict(DEFAULT_ESCALATION_LEVELS)
                    | {"levels": dict(DEFAULT_ESCALATION_LEVELS)},
                }
            )
        return out

    # -- voicemail ------------------------------------------------------------

    async def _candidates_voicemail(self, now_utc: datetime) -> list[dict[str, Any]]:
        svc = self._voicemail
        if svc is None:
            return []
        threads = await _call(svc, "list_threads")
        if not isinstance(threads, list):
            return []
        out: list[dict[str, Any]] = []
        for thread in threads:
            callback_reason = _attr(thread, "callback_reason")
            needs_callback = _attr(thread, "needs_callback")
            if not (callback_reason or needs_callback):
                continue
            thread_id = _attr(thread, "id")
            last_at = _ensure_utc(_attr(thread, "last_message_at")) or now_utc
            out.append(
                {
                    "source_kind": SOURCE_VOICEMAIL,
                    "source_refs": [f"{SOURCE_VOICEMAIL}:{thread_id}"],
                    "title": (
                        f"Voicemail from {_attr(thread, 'caller_name') or 'caller'} "
                        "needs a call back"
                    ),
                    "body": (
                        f"Caller requested a callback ({callback_reason or 'callback requested'})."
                    ),
                    "importance": 7,
                    "urgency": "high",
                    "deadline_at": last_at + timedelta(hours=48),
                    "evidence": [f"voicemail thread {thread_id} callback={callback_reason}"],
                    "recommended_action": "Call the caller back.",
                    "escalation_policy": dict(DEFAULT_ESCALATION_LEVELS)
                    | {"levels": dict(DEFAULT_ESCALATION_LEVELS)},
                }
            )
        return out

    # -- calendar -------------------------------------------------------------

    async def _candidates_calendar(self, now_utc: datetime) -> list[dict[str, Any]]:
        if self._registry is None:
            return []
        events = await self._registry.calendar.list_events(
            start=now_utc, end=now_utc + timedelta(hours=72)
        )
        out: list[dict[str, Any]] = []
        for event in events:
            if _attr(event, "deleted") or _attr(event, "status") == "cancelled":
                continue
            start = _ensure_utc(_attr(event, "start"))
            if start is None or start < now_utc:
                continue
            within_6h = (start - now_utc) <= timedelta(hours=6)
            location = _attr(event, "location")
            out.append(
                {
                    "source_kind": SOURCE_CALENDAR,
                    "source_refs": [f"{SOURCE_CALENDAR}:{_attr(event, 'id')}"],
                    "title": f"Upcoming: {_attr(event, 'title') or 'appointment'}",
                    "body": f"Starts {start.isoformat()}" + (f" ({location})" if location else ""),
                    "importance": 6 if _attr(event, "appt") else 5,
                    "urgency": "high" if within_6h else "normal",
                    "deadline_at": start,
                    "evidence": [
                        f"calendar event {_attr(event, 'ical_uid') or _attr(event, 'id')}"
                    ],
                    "recommended_action": "Prepare for the appointment.",
                    "escalation_policy": dict(DEFAULT_ESCALATION_LEVELS)
                    | {"levels": dict(DEFAULT_ESCALATION_LEVELS)},
                }
            )
        return out

    # -- bills ----------------------------------------------------------------

    async def _candidates_bills(self, now_utc: datetime) -> list[dict[str, Any]]:
        if self._registry is None:
            return []
        from backend.persistence.repositories.bills_repo import BillsRepository

        repo = BillsRepository(self._session_factory)
        bills = await repo.upcoming(days=14, now=now_utc)
        out: list[dict[str, Any]] = []
        for bill in bills:
            level = bill_escalation_level(bill, now_utc)
            if level is None:
                continue
            due = _ensure_utc(_attr(bill, "due_at")) or now_utc
            amount = _attr(bill, "amount_cents", "0")
            payee = _attr(bill, "payee") or "unknown biller"
            currency = _attr(bill, "currency") or "USD"
            out.append(
                {
                    "source_kind": SOURCE_BILLS,
                    "source_refs": [f"bill:{_attr(bill, 'id')}"],
                    "title": f"Bill due {due.strftime('%Y-%m-%d')}: {payee}",
                    "body": f"{payee} — {amount} ({currency}), due {due.isoformat()}.",
                    "importance": _BILL_IMPORTANCE.get(level, 5),
                    "urgency": _BILL_URGENCY.get(level, "normal"),
                    "deadline_at": due,
                    "evidence": [f"bill {_attr(bill, 'id')} escalation level {level}"],
                    "recommended_action": "Confirm payment if paid, or dispute the charge.",
                    "escalation_policy": {
                        "bills.escalation_level": level,
                        "levels": dict(DEFAULT_ESCALATION_LEVELS),
                    },
                }
            )
        return out

    # -- commitments ----------------------------------------------------------

    async def _candidates_commitments(self, now_utc: datetime) -> list[dict[str, Any]]:
        if self._registry is None:
            return []
        commitments = await self._registry.commitments.list_commitments(state="open")
        out: list[dict[str, Any]] = []
        for commitment in commitments:
            due = _ensure_utc(_attr(commitment, "due_at"))
            if due is None:
                urgency, deadline = "normal", now_utc + timedelta(days=7)
            elif due < now_utc:
                urgency, deadline = "critical", due
            elif (due - now_utc) <= timedelta(hours=24):
                urgency, deadline = "high", due
            else:
                urgency, deadline = "normal", due
            out.append(
                {
                    "source_kind": SOURCE_COMMITMENTS,
                    "source_refs": [f"{SOURCE_COMMITMENTS}:{_attr(commitment, 'id')}"],
                    "title": f"Commitment: {_attr(commitment, 'text') or 'follow up'}",
                    "body": f"Open commitment due {due.isoformat() if due else 'soon'}.",
                    "importance": 7,
                    "urgency": urgency,
                    "deadline_at": deadline,
                    "evidence": [
                        f"commitment {_attr(commitment, 'id')} "
                        f"due={due.isoformat() if due else None}"
                    ],
                    "recommended_action": "Close out the commitment.",
                    "escalation_policy": dict(DEFAULT_ESCALATION_LEVELS)
                    | {"levels": dict(DEFAULT_ESCALATION_LEVELS)},
                }
            )
        return out

    # -- routines -------------------------------------------------------------

    async def _candidates_routines(self, now_utc: datetime) -> list[dict[str, Any]]:
        if self._registry is None:
            return []
        from backend.persistence.repositories.routines_repo import RoutinesRepository
        from backend.routines.service import RoutinesService

        service = RoutinesService(RoutinesRepository(self._session_factory))
        view = await service.today_view(now=now_utc)
        out: list[dict[str, Any]] = []
        missed = set(view.get("missed_notes", []))
        for entry in list(view.get("due_now", [])) + list(view.get("missed_notes", [])):
            routine_id = _attr(entry, "routine_id") or _attr(entry, "id") or _attr(entry, "window")
            if not routine_id:
                continue
            window = _attr(entry, "window") or _attr(entry, "time")
            is_missed = entry in missed
            at = _ensure_utc(_attr(entry, "at") or _attr(entry, "deadline_at")) or now_utc
            out.append(
                {
                    "source_kind": SOURCE_ROUTINES,
                    "source_refs": [f"{SOURCE_ROUTINES}:{routine_id}:{window or 'now'}"],
                    "title": (
                        f"Routine {'missed' if is_missed else 'due now'}: "
                        f"{_attr(entry, 'title') or 'routine'}"
                    ),
                    "body": (
                        f"Routine window {window or 'now'} ({_attr(entry, 'timezone') or 'UTC'})."
                    ),
                    "importance": 5,
                    "urgency": "normal" if is_missed else "high",
                    "deadline_at": at,
                    "evidence": [f"routine {_attr(entry, 'routine_id')} window {window}"],
                    "recommended_action": "Complete the routine or reschedule it.",
                    "escalation_policy": dict(DEFAULT_ESCALATION_LEVELS)
                    | {"levels": dict(DEFAULT_ESCALATION_LEVELS)},
                }
            )
        return out

    # -- incidents ------------------------------------------------------------

    async def _candidates_incidents(self, now_utc: datetime) -> list[dict[str, Any]]:
        svc = self._incidents
        if svc is None:
            return []
        rows = await _call(svc, "list_incidents")
        if not isinstance(rows, list):
            return []
        out: list[dict[str, Any]] = []
        for row in rows:
            state = _attr(row, "state", "active")
            if state in ("resolved", "dismissed", "suppressed"):
                continue
            row_id = _attr(row, "id") or _attr(row, "incident_id")
            created = _ensure_utc(_attr(row, "created_at")) or now_utc
            out.append(
                {
                    "source_kind": SOURCE_INCIDENTS,
                    "source_refs": [f"{SOURCE_INCIDENTS}:{row_id}"],
                    "title": (
                        f"Incident: "
                        f"{_attr(row, 'title') or _attr(row, 'summary') or 'active incident'}"
                    ),
                    "body": (
                        f"State {state} — {_attr(row, 'summary') or 'an incident needs attention.'}"
                    ),
                    "importance": 7,
                    "urgency": "high",
                    "deadline_at": created + timedelta(hours=4),
                    "evidence": [f"incident {row_id} state={state}"],
                    "recommended_action": "Review the incident and acknowledge it.",
                    "escalation_policy": dict(DEFAULT_ESCALATION_LEVELS)
                    | {"levels": dict(DEFAULT_ESCALATION_LEVELS)},
                }
            )
        return out

    # -- approvals ------------------------------------------------------------

    async def _candidates_approvals(self, now_utc: datetime) -> list[dict[str, Any]]:
        svc = self._approvals
        if svc is None:
            return []
        rows = await _call(svc, "list_approvals")
        if rows is None:
            rows = await _call(svc, "list_pending")
        if not isinstance(rows, list):
            return []
        out: list[dict[str, Any]] = []
        for row in rows:
            state = _attr(row, "state") or _attr(row, "status")
            if state in ("approved", "rejected", "denied", "expired", "dismissed"):
                continue
            row_id = _attr(row, "id") or _attr(row, "approval_id")
            created = _ensure_utc(_attr(row, "created_at")) or now_utc
            out.append(
                {
                    "source_kind": SOURCE_APPROVALS,
                    "source_refs": [f"{SOURCE_APPROVALS}:{row_id}"],
                    "title": (
                        f"Agent approval needed: "
                        f"{_attr(row, 'summary') or _attr(row, 'title') or 'pending action'}"
                    ),
                    "body": f"An agent is waiting on your approval ({state or 'pending'}).",
                    "importance": 7,
                    "urgency": "high",
                    "deadline_at": created + timedelta(hours=24),
                    "evidence": [f"approval {row_id} state={state}"],
                    "recommended_action": "Approve or reject the pending agent action.",
                    "escalation_policy": dict(DEFAULT_ESCALATION_LEVELS)
                    | {"levels": dict(DEFAULT_ESCALATION_LEVELS)},
                }
            )
        return out

    # -- sync -----------------------------------------------------------------

    async def sync(self, now: Any = None) -> dict[str, int]:
        """Upsert candidates into durable items (+ in-memory mirror); return counts.

        Returns ``{"created": n, "updated": n, "skipped": n}``. With no registry
        the durable side is skipped (honest degradation) but the in-memory
        mirror still runs for every source that yields items.
        """
        now_utc = _ensure_utc(now) or datetime.now(UTC)
        candidates = await self.collect_candidates(now_utc)
        created = updated = skipped = 0

        existing: list[Any] = []
        if self._repo is not None:
            try:
                existing = await self._repo.list_attention_items(state="active")
            except Exception:  # noqa: BLE001 — read failure degrades to all-create
                existing = []

        for candidate in candidates:
            self._mirror_in_memory(candidate)  # always available (in-process)
            if self._repo is None:
                skipped += 1
                continue
            match = self._match_existing(existing, candidate)
            try:
                if match is None:
                    row = await self._repo.create_attention_item(
                        title=candidate["title"],
                        importance=candidate["importance"],
                        urgency=candidate["urgency"],
                        body=candidate.get("body"),
                        deadline_at=_ensure_utc(candidate.get("deadline_at")),
                        source_refs=list(candidate.get("source_refs", [])),
                        recommended_action=candidate.get("recommended_action"),
                        escalation_policy=candidate.get("escalation_policy"),
                    )
                    existing.append(row)
                    created += 1
                else:
                    updated += 1
                    await self._update_row(match, candidate)
            except Exception:  # noqa: BLE001 — durable failure degrades to mirror-only
                if created == 0 and updated == 0:
                    pass  # keep the counters honest per-candidate
                skipped += 1
        return {"created": created, "updated": updated, "skipped": skipped}

    @staticmethod
    def _match_existing(existing: list[Any], candidate: dict[str, Any]) -> Any:
        """Find a durable item whose ``source_refs`` carry this candidate's ref.

        The ref that carries the source id (e.g. ``bill:<id>``) must appear in
        the item's ``source_refs`` — the same subset match the bills endpoint
        uses to resolve attention by ref."""
        refs = {str(r) for r in candidate.get("source_refs", [])}
        if not refs:
            return None
        for row in existing:
            row_refs = {str(r) for r in (getattr(row, "source_refs", None) or [])}
            if refs & row_refs:
                return row
        return None

    async def _update_row(self, row: Any, candidate: dict[str, Any]) -> None:
        """Refresh a durable item in place (title/urgency/deadline/policy)."""
        assert self._repo is not None
        async with self._repo._sessions() as session:  # noqa: SLF001 — same factory
            fresh = await session.get(row.__class__, row.id)
            if fresh is None:
                return
            fresh.title = candidate["title"]
            fresh.urgency = candidate["urgency"]
            fresh.importance = candidate["importance"]
            fresh.deadline_at = _ensure_utc(candidate.get("deadline_at")) or fresh.deadline_at
            fresh.escalation_policy = candidate.get("escalation_policy") or fresh.escalation_policy
            if candidate.get("recommended_action"):
                fresh.recommended_action = candidate["recommended_action"]
            fresh.state = "active"
            await session.commit()

    # -- in-memory mirror -----------------------------------------------------

    def _mirror_in_memory(self, candidate: dict[str, Any]) -> Any:
        """Mirror a candidate into the in-memory queue (create or refresh)."""
        service = _get_service()
        payload = _candidate_payload(candidate)
        ref = payload["sources"][-1] if payload["sources"] else None
        existing = self._find_in_memory(service, ref)
        if existing is not None:
            refreshed = service.refresh(existing.attention_id, payload)
            return refreshed if refreshed is not None else existing
        try:
            return service.create(payload)
        except Exception:  # noqa: BLE001 — create requires a reason; degrade
            return None

    @staticmethod
    def _find_in_memory(service: AttentionService, ref: str | None) -> Any:
        if ref is None:
            return None
        for item in service.list():
            if ref in list(item.sources):
                return item
        return None

    # -- escalation -----------------------------------------------------------

    async def run_escalation(self, now: Any = None) -> list[dict[str, Any]]:
        """Notify durable ACTIVE items that crossed a policy threshold.

        Only when ``not repo.already_notified(item, level)`` →
        ``record_notification`` + bump urgency on the row + mirror in memory.
        A second run at the same clock returns every entry ``notified=False``
        (the anti-nag guarantee — the same (item, level) can never
        double-notify; a NEW level after a real clock change may notify once).
        """
        now_utc = _ensure_utc(now) or datetime.now(UTC)
        results: list[dict[str, Any]] = []
        if self._repo is None:
            return results
        try:
            rows = await self._repo.list_attention_items(state="active")
        except Exception:  # noqa: BLE001
            return results
        for row in rows:
            level = escalation_level_for(row, now_utc, row.escalation_policy)
            notified = False
            try:
                if not await self._repo.already_notified(row.id, level):
                    await self._repo.record_notification(
                        row.id, level=level, sent_at=now_utc, channel="attention"
                    )
                    await self._bump_urgency(row, level)
                    self._mirror_level(row, level)
                    notified = True
            except Exception:  # noqa: BLE001 — never let one item stop the run
                notified = False
            results.append({"item_id": row.id, "level": level, "notified": notified})
        return results

    async def _bump_urgency(self, row: Any, level: str) -> None:
        assert self._repo is not None
        async with self._repo._sessions() as session:  # noqa: SLF001
            fresh = await session.get(row.__class__, row.id)
            if fresh is None:
                return
            fresh.urgency = level
            fresh.importance = max(int(fresh.importance), _LEVEL_IMPORTANCE.get(level, 6))
            await session.commit()

    def _mirror_level(self, row: Any, level: str) -> None:
        service = _get_service()
        refs = [str(r) for r in (getattr(row, "source_refs", None) or [])]
        ref = refs[-1] if refs else None
        existing = self._find_in_memory(service, ref) if ref else None
        if existing is None:
            return
        importance = _LEVEL_IMPORTANCE.get(level, int(getattr(row, "importance", 6)))
        service.refresh(
            existing.attention_id,
            {
                "urgency": _URGENCY_FLOAT.get(level, 0.5),
                "importance": min(importance / 10.0, 1.0),
                "due": _ensure_utc(getattr(row, "deadline_at", None)),
            },
        )

    # -- transitions ----------------------------------------------------------

    async def transition(
        self,
        item_id: str,
        action: str,
        *,
        until: Any = None,
    ) -> Any:
        """Perform a user action durably + in memory.

        Actions: ``snooze`` / ``resolve`` / ``acknowledge`` (resolve alias) /
        ``suppress`` / ``dismiss``. Unknown id → ``None``.

        - No registry → in-memory only (matching the historical endpoint
          behavior exactly; the e2e degraded-mode wire contract).
        - Registry present → write through the durable repository and mirror
          the state into the in-memory queue. When the id has no durable row
          but exists in memory (an endpoint-created item), it degrades to an
          in-memory-only transition for that id — no phantom durable row is
          fabricated.
        """
        service = _get_service()
        now_utc = _ensure_utc(datetime.now(UTC)) or datetime.now(UTC)

        def _in_memory_only() -> Any:
            if action == "snooze":
                return service.snooze(item_id, _ensure_utc(until))
            if action == "resolve":
                return service.resolve(item_id)
            if action == "acknowledge":
                return service.acknowledge(item_id)
            if action == "suppress":
                return service.suppress(item_id)
            if action == "dismiss":
                target = service.get(item_id)
                if target is None:
                    return None
                return service.suppress(target.attention_id)
            return None

        if self._repo is None:
            return _in_memory_only()

        until_utc = _ensure_utc(until)
        state: str | None = None
        try:
            if action == "snooze":
                snooze_row = await self._repo.snooze(
                    item_id, until=until_utc or (now_utc + timedelta(days=1))
                )
                if snooze_row is None:
                    return _in_memory_only()
                # ``snooze`` returns the SnoozeRow event; the durable state is
                # on the item row, so re-fetch it for the wire response.
                row = await self._repo.get_attention_item(item_id)
                state = "snoozed"
            elif action in ("resolve", "acknowledge"):
                row = await self._repo.set_state(item_id, "resolved")
                state = "resolved"
            elif action in ("suppress", "dismiss"):
                row = await self._repo.set_state(item_id, "dismissed")
                state = "dismissed"
            else:
                row = None
                state = None
        except Exception:  # noqa: BLE001 — a repo failure degrades, never 500s
            row = None
            state = None

        if row is None:
            # No durable row for this id (e.g. an endpoint-created item):
            # fall back to the in-memory transition for that id. Unknown ids
            # return ``None`` (→ 404) without an exception.
            return _in_memory_only()

        if state is not None:
            await self._mirror_state(item_id, state, due=until_utc)
        return row

    async def _mirror_state(self, item_id: str, state: str, *, due: Any = None) -> None:
        """Mirror a durable state change into the in-memory queue.

        The durable row id is not the in-memory id; match by the item's
        ``source_refs`` so the in-memory mirror stays consistent with durable.
        ``due`` (snooze only) sets the mirrored item's ``due`` to the snooze
        window so the in-memory and durable states agree.
        """
        service = _get_service()
        ref: str | None = None
        try:
            row = await self._repo.get_attention_item(item_id) if self._repo else None
            if row is not None:
                for r in getattr(row, "source_refs", None) or []:
                    ref = str(r)
        except Exception:  # noqa: BLE001
            ref = None
        target = self._find_in_memory(service, ref) if ref else service.get(item_id)
        if target is None:
            return
        # Imported at call time (request handling): ``backend.api.models``
        # re-enters the app import chain and must not load at module import.
        from backend.api.models import AttentionState

        if state == "snoozed" and due is not None:
            until = _ensure_utc(due)
            if until is not None:
                target.due = until
        service.update_status(
            target.attention_id, AttentionState(_state_to_status(state)), action="state"
        )
