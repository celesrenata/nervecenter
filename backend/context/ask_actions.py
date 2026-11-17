"""Ask actions: turn "add X to my calendar" into a calendar event.

Flow (kept out of the grounded citation flow in :mod:`backend.context.ask`):

1. :func:`looks_like_calendar_create` is a cheap regex gate, so an ordinary
   question never pays for a second inference call.
2. :func:`extract_event_draft` asks the secure reader route for a JSON draft
   (title, local start, duration, assumptions) and :func:`finalize_draft`
   applies the checks the model cannot be trusted with: weekday agreement, past
   starts, and the assumed-PM flag.
3. :func:`resolve_account` picks the Google calendar: the account chosen in the
   UI, else one named in the request, else ``NERVE_ASK_CALENDAR_DEFAULT_ACCOUNT``.
4. With ``NERVE_ASK_CALENDAR_CONFIRM_FIRST`` true (default) Ask returns a
   proposal and the event is written only by ``confirm_proposal``. With it
   false the event is written at once and an undo token is returned.

This module must not import :mod:`backend.context.ask` (it imports us).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import structlog
from pydantic import BaseModel, Field, ValidationError, field_validator

from backend.calendar.proposals import (
    Proposal,
    ProposalStore,
    get_proposal_store,
)
from backend.calendar.providers import TransportError, resolve_zone
from backend.calendar.service import CalendarService, get_calendar_service
from backend.calendar.sync import (
    CalendarSyncRunner,
    EventWrite,
    HttpxTransport,
    event_ical_id,
)
from backend.connectors.redact import redact_message

log = structlog.get_logger(__name__)

ENV_CONFIRM_FIRST = "NERVE_ASK_CALENDAR_CONFIRM_FIRST"
ENV_DEFAULT_ACCOUNT = "NERVE_ASK_CALENDAR_DEFAULT_ACCOUNT"
ENV_USER_TIMEZONE = "NERVE_USER_TIMEZONE"
DEFAULT_ACCOUNT = "celes@frameshift.net"
WRITE_SCOPE_EXTRA = "calendar_write"

_FALSE = {"0", "false", "no", "off"}


def confirm_first(env: dict[str, str] | None = None) -> bool:
    """The one switch: ``true`` (default) proposes and waits for Confirm,
    ``false`` creates at once and returns an undo token."""
    raw = (env if env is not None else os.environ).get(ENV_CONFIRM_FIRST, "true")
    return raw.strip().lower() not in _FALSE


# ---------------------------------------------------------------------------
# Gate
# ---------------------------------------------------------------------------
_VERBS = r"(?:add|put|schedule|book|create|set\s+up|make|enter|log|save|block)"
_NOUNS = r"(?:calendar|appointment|meeting|event|reminder)"
_CREATE_RE = re.compile(
    rf"\b{_VERBS}\b[^?!.]{{0,120}}?\b{_NOUNS}s?\b|\b{_NOUNS}s?\b[^?!.]{{0,60}}?\b(?:add|put)\b",
    re.IGNORECASE,
)


def looks_like_calendar_create(question: str | None) -> bool:
    """Cheap gate: does the request ask to put something on the calendar?"""
    return bool(question and _CREATE_RE.search(question))


# ---------------------------------------------------------------------------
# Draft
# ---------------------------------------------------------------------------
class DraftError(Exception):
    """The request was understood but cannot be scheduled (e.g. in the past).
    The message is safe to show the user."""


class EventDraft(BaseModel):
    """The event the model understood. Times are naive local wall-clock until
    :func:`finalize_draft` attaches the zone."""

    model_config = {"extra": "ignore"}

    title: str
    start: datetime
    end: datetime | None = None
    duration_minutes: int = Field(default=60, ge=5, le=24 * 60)
    all_day: bool = False
    location: str | None = None
    calendar_hint: str | None = None
    assumptions: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    @field_validator("title")
    @classmethod
    def _title(cls, value: str) -> str:
        value = " ".join(str(value).split())
        if not value:
            raise ValueError("empty title")
        return value[:200]


_WEEKDAYS = (
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
)
_WEEKDAY_RE = re.compile(r"\b(" + "|".join(_WEEKDAYS) + r")\b", re.IGNORECASE)
_AT_TIME_RE = re.compile(r"\bat\s+(\d{1,2})(?::(\d{2}))?\b(?!\s*(?:[ap]\.?m\b|o'?clock))",
                         re.IGNORECASE)
_MERIDIEM_RE = re.compile(r"\b\d{1,2}(?::\d{2})?\s*[ap]\.?m\b|\bnoon\b|\bmidnight\b|\bo'?clock\b",
                          re.IGNORECASE)


def user_timezone(default_account_tz: str | None) -> str:
    """Default calendar account tz, else ``NERVE_USER_TIMEZONE``, else UTC.
    An account left on the creation default ("UTC") counts as unset."""
    account_tz = (default_account_tz or "").strip()
    if account_tz and account_tz.upper() != "UTC":
        return account_tz
    return os.environ.get(ENV_USER_TIMEZONE, "").strip() or account_tz or "UTC"


def _extraction_prompt(question: str, now_local: datetime, tz: str) -> str:
    return "\n".join(
        [
            "You turn a request to add something to a calendar into JSON.",
            "Reply with one JSON object and nothing else.",
            f"Current local date: {now_local:%Y-%m-%d} ({now_local:%A}). "
            f"Current local time: {now_local:%H:%M}. Timezone: {tz}.",
            'If the request is not asking to create a calendar entry reply {"is_event": false}.',
            'Otherwise reply {"is_event": true, "title": str, "start": "YYYY-MM-DDTHH:MM" local, '
            '"end": "YYYY-MM-DDTHH:MM" local or null, "duration_minutes": int (default 60), '
            '"all_day": bool, "location": str or null, "calendar_hint": the account or calendar '
            "the user named, else null, \"assumptions\": [str]}.",
            "Resolve relative dates (today, tomorrow, next Friday) from the current local date.",
            "A clock time without am/pm for an appointment is the nearest plausible daytime "
            "reading (4:30 -> 16:30) and must be flagged as assumed in assumptions.",
            "Do not invent details that are not in the request.",
            f"Request: {question}",
        ]
    )


def _parse_model_json(content: str) -> dict[str, Any] | None:
    text = (content or "").strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.MULTILINE).strip()
    lo, hi = text.find("{"), text.rfind("}")
    if lo < 0 or hi <= lo:
        return None
    try:
        data = json.loads(text[lo : hi + 1])
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def finalize_draft(
    draft: EventDraft, question: str, now_local: datetime, tz: ZoneInfo
) -> EventDraft:
    """Checks done in code, not by the model. Returns a draft whose start/end
    are tz-aware in ``tz``. Raises :class:`DraftError` for a past start."""

    def local(value: datetime) -> datetime:
        if value.tzinfo is not None:
            value = value.astimezone(tz).replace(tzinfo=None)
        return value

    start = local(draft.start)
    warnings = list(draft.warnings)
    assumptions = list(draft.assumptions)

    named = {_WEEKDAYS.index(m.lower()) for m in _WEEKDAY_RE.findall(question or "")}
    if named and start.weekday() not in named:
        wanted = min(named, key=lambda d: (d - now_local.weekday()) % 7)
        shift = (wanted - now_local.weekday()) % 7
        fixed_day = (now_local + timedelta(days=shift)).date()
        warnings.append(
            f"You said {_WEEKDAYS[wanted].title()} but {start:%a %b} {start.day} is a "
            f"{start:%A}; I used {fixed_day:%a %b} {fixed_day.day}."
        )
        start = datetime.combine(fixed_day, start.time())

    # The assumed-PM flag does not depend on the model remembering to say it.
    if not draft.all_day and not _MERIDIEM_RE.search(question or ""):
        match = _AT_TIME_RE.search(question or "")
        if match:
            hour = int(match.group(1))
            if 1 <= hour <= 11 and start.hour == hour + 12 and not any(
                "pm" in a.lower() or "assum" in a.lower() or str(start.hour) in a
                for a in assumptions
            ):
                assumptions.append(
                    f"No am/pm given: read {match.group(0)[3:]} as {start:%H:%M} (PM)."
                )

    if draft.all_day:
        start = datetime.combine(start.date(), datetime.min.time())
        end = start + timedelta(days=1)
        if draft.end is not None and local(draft.end).date() > start.date():
            end = datetime.combine(local(draft.end).date(), datetime.min.time())
    else:
        end = local(draft.end) if draft.end is not None else None
        if end is None or end <= start:
            end = start + timedelta(minutes=draft.duration_minutes)

    start_aware = start.replace(tzinfo=tz)
    if draft.all_day:
        if start_aware.date() < now_local.date():
            raise DraftError(
                f"{start:%A %b} {start.day} is already past, so I did not add it."
            )
    elif start_aware < now_local:
        raise DraftError(
            f"{start:%A %b} {start.day} at {start:%-I:%M %p} is already past, "
            "so I did not add it. Tell me the date again."
        )
    return draft.model_copy(
        update={
            "start": start_aware,
            "end": end.replace(tzinfo=tz),
            "assumptions": assumptions,
            "warnings": warnings,
        }
    )


def extract_event_draft(
    transport: Any,
    model: str,
    question: str,
    now_local: datetime,
    tz: str,
) -> EventDraft | None:
    """One inference call on the secure reader route. Returns ``None`` when the
    model says this is not an event or its answer is unusable (the caller then
    falls through to the normal answer). Raises :class:`DraftError` for a start
    in the past."""
    prompt = _extraction_prompt(question, now_local, tz)
    try:
        if hasattr(transport, "check"):  # SecureTransport: reader route, secure headers
            result = transport.generate(
                model, prompt, kind="extract", sensitive=True, max_tokens=300
            )
        else:
            result = transport.generate(model, prompt)
    except Exception as exc:  # noqa: BLE001 - extraction is best effort
        log.warning("ask.calendar.extract_failed", error=type(exc).__name__)
        return None
    data = _parse_model_json(str((result or {}).get("content", "")))
    if not data or data.get("is_event") is False:
        return None
    data = {k: v for k, v in data.items() if v is not None}
    try:
        draft = EventDraft.model_validate(data)
    except ValidationError:
        return None
    return finalize_draft(draft, question, now_local, resolve_zone(tz, "UTC"))


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------
def account_write_info(row: Any, connection: Any | None) -> dict[str, Any]:
    """Write capability of one calendar account row (no secrets)."""
    cfg = getattr(row, "config", None) or {}
    connection_id = cfg.get("connection_id")
    conn_cfg = (getattr(connection, "config", None) or {}) if connection is not None else {}
    granted = conn_cfg.get("granted_extra") or []
    return {
        "connection_id": str(connection_id) if connection_id else None,
        "can_write": WRITE_SCOPE_EXTRA in granted,
        "writable": row.provider == "google" and bool(connection_id),
    }


def _identities(row: Any, connection: Any | None) -> list[str]:
    conn_cfg = (getattr(connection, "config", None) or {}) if connection is not None else {}
    values = [
        row.account_ref,
        row.label,
        conn_cfg.get("account"),
        getattr(connection, "label", None) if connection is not None else None,
    ]
    return [str(v).strip().lower() for v in values if v and str(v).strip()]


async def list_writable_accounts(registry: Any) -> list[dict[str, Any]]:
    """Google accounts linked through ``config.connection_id`` (the only
    writable ones), as plain dicts."""
    rows = await registry.calendar.list_accounts(enabled_only=True)
    connections = {str(c.id): c for c in await registry.connections.list_connections()}
    out: list[dict[str, Any]] = []
    for row in rows:
        conn = connections.get(str((row.config or {}).get("connection_id") or ""))
        info = account_write_info(row, conn)
        if not info["writable"]:
            continue
        idents = _identities(row, conn)
        out.append(
            {
                "id": row.id,
                "label": row.label or row.account_ref or (idents[0] if idents else row.id),
                "timezone": row.timezone,
                "identities": idents,
                "connection_id": info["connection_id"],
                "can_write": info["can_write"],
            }
        )
    return out


def public_account(account: dict[str, Any]) -> dict[str, Any]:
    return {k: account[k] for k in ("id", "label", "can_write")}


def _match(accounts: list[dict[str, Any]], needle: str | None) -> dict[str, Any] | None:
    needle = (needle or "").strip().lower()
    if not needle:
        return None
    for account in accounts:
        if any(needle == i or needle in i or (len(i) > 3 and i in needle)
               for i in account["identities"]):
            return account
    return None


def default_account_name() -> str:
    return os.environ.get(ENV_DEFAULT_ACCOUNT, "").strip() or DEFAULT_ACCOUNT


def resolve_account(
    accounts: list[dict[str, Any]],
    *,
    account_id: str | None,
    hint: str | None,
    question: str,
) -> dict[str, Any] | None:
    """UI-chosen account > account named in the request > the default account.
    With one writable account and no match, that account."""
    if account_id:
        chosen = next((a for a in accounts if a["id"] == account_id), None)
        if chosen is not None:
            return chosen
    named = _match(accounts, hint)
    if named is None:
        lowered = (question or "").lower()
        named = next(
            (a for a in accounts if any(len(i) > 3 and i in lowered for i in a["identities"])),
            None,
        )
    if named is not None:
        return named
    default = _match(accounts, default_account_name())
    if default is not None:
        return default
    return accounts[0] if len(accounts) == 1 else None


# ---------------------------------------------------------------------------
# Context handed to the (sync) Ask pipeline
# ---------------------------------------------------------------------------
@dataclass
class CalendarContext:
    """Everything the sync Ask pipeline needs for calendar actions, prepared
    on the event loop. :meth:`run` brings async work back onto that loop."""

    state: Any
    loop: asyncio.AbstractEventLoop
    sub: str
    accounts: list[dict[str, Any]]
    store: ProposalStore
    account_id: str | None = None
    now_fn: Callable[[], datetime] | None = None

    def run(self, coro: Coroutine[Any, Any, Any], timeout: float = 60.0) -> Any:
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def now(self) -> datetime:
        return self.now_fn() if self.now_fn else datetime.now(UTC)


async def build_calendar_context(
    state: Any, *, sub: str, account_id: str | None = None,
    now_fn: Callable[[], datetime] | None = None,
) -> CalendarContext | None:
    """``None`` when durable storage is not configured (no calendar to write)."""
    registry = getattr(state, "repository_registry", None)
    if registry is None or not hasattr(registry, "calendar"):
        return None
    try:
        accounts = await list_writable_accounts(registry)
    except Exception as exc:  # noqa: BLE001 - never break Ask over a calendar read
        log.warning("ask.calendar.accounts_failed", error=redact_message(exc))
        return None
    if now_fn is None:
        now_fn = getattr(state, "ask_calendar_now", None)
    return CalendarContext(
        state=state,
        loop=asyncio.get_running_loop(),
        sub=sub,
        accounts=accounts,
        store=get_proposal_store(state),
        account_id=account_id,
        now_fn=now_fn,
    )


# ---------------------------------------------------------------------------
# Propose / confirm / cancel / undo
# ---------------------------------------------------------------------------
class ActionError(Exception):
    """A calendar action failed in a way the API reports with a stable code."""

    def __init__(self, status: int, code: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.extra = extra

    def body(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, **self.extra}


def calendar_service_for(state: Any) -> CalendarService:
    service = getattr(state, "calendar_service", None)
    if service is None:
        service = get_calendar_service()
        state.calendar_service = service
    return service


def runner_for(state: Any) -> CalendarSyncRunner:
    runner = getattr(state, "calendar_sync_runner", None)
    if isinstance(runner, CalendarSyncRunner):
        return runner
    return CalendarSyncRunner(
        state.repository_registry,
        calendar_service=calendar_service_for(state),
        transport=getattr(state, "calendar_transport", None) or HttpxTransport(),
    )


def provider_event_id(proposal_id: str) -> str:
    """Deterministic Google event id (base32hex, lowercase) for a proposal, so
    confirming twice cannot create two events."""
    digest = hashlib.sha256(proposal_id.encode("utf-8")).digest()[:15]
    return base64.b32hexencode(digest).decode("ascii").lower().rstrip("=")


async def _conflicts(state: Any, start: datetime, end: datetime) -> list[dict[str, Any]]:
    """Events overlapping the window. Durable rows are replayed into the
    in-memory service first (it is empty after a restart)."""
    from backend.api.endpoints.calendar import _row_payload  # same bridge as /conflicts

    service = calendar_service_for(state)
    registry = getattr(state, "repository_registry", None)
    if registry is not None:
        rows = await registry.calendar.list_events(
            start=start, end=end, include_deleted=False, limit=500
        )
        for row in rows:
            service.ingest(_row_payload(row))
    return [
        {"event_id": e.event_id, "title": e.title,
         "start": e.start.isoformat() if e.start else None,
         "end": e.end.isoformat() if e.end else None}
        for e in service.conflicts(start, end)
    ]


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def proposal_view(proposal: Proposal) -> dict[str, Any]:
    account = next((a for a in proposal.accounts if a["id"] == proposal.account_id), None)
    return {
        "id": proposal.id,
        "title": proposal.title,
        "start": _iso(proposal.start),
        "end": _iso(proposal.end),
        "all_day": proposal.all_day,
        "location": proposal.location,
        "tz": proposal.tz,
        "account": public_account(account) if account else {
            "id": proposal.account_id, "label": proposal.account_id, "can_write": False},
        "accounts": proposal.accounts,
        "warnings": proposal.warnings,
        "assumptions": proposal.assumptions,
        "conflicts": proposal.conflicts,
        "expires_at": _iso(proposal.expires_at),
        "requires_confirmation": True,
    }


def _summary(proposal: Proposal, verb: str) -> str:
    when = (
        f"on {proposal.start:%a %b} {proposal.start.day}"
        if proposal.all_day
        else f"on {proposal.start:%a %b} {proposal.start.day} at {proposal.start:%-I:%M %p}"
    )
    account = next((a["label"] for a in proposal.accounts if a["id"] == proposal.account_id), "")
    return f"{verb} {proposal.title} {when} {('to ' + account) if account else ''}".strip()


def created_view(proposal: Proposal, ev: Any, store: ProposalStore) -> dict[str, Any]:
    created = store.get_created(event_id_for(proposal.account_id, ev.ical_uid), proposal.sub)
    event_id = event_id_for(proposal.account_id, ev.ical_uid)
    return {
        "event": {
            "event_id": event_id,
            "title": ev.title,
            "start": _iso(ev.start),
            "end": _iso(ev.end),
            "all_day": ev.all_day,
            "location": ev.location,
            "account_id": proposal.account_id,
        },
        "undo": {
            "event_id": event_id,
            "expires_at": _iso(created.expires_at) if created else None,
        },
    }


def event_id_for(account_id: str, ical_uid: str) -> str:
    return event_ical_id(account_id, ical_uid)


def _parse_override(value: str | None, tz: str, field: str) -> datetime | None:
    if value is None or value == "":
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ActionError(422, "invalid_time", f"{field} is not a valid ISO-8601 time") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=resolve_zone(tz, "UTC"))


async def confirm_proposal(
    state: Any,
    store: ProposalStore,
    proposal: Proposal,
    *,
    account_id: str | None = None,
    title: str | None = None,
    start: str | None = None,
    end: str | None = None,
) -> dict[str, Any]:
    """Write the proposed event. Idempotent: a repeat confirm returns the first
    result. Raises :class:`ActionError` (409 ``consent_required`` when Google
    refuses the write scope)."""
    if proposal.result is not None:
        return proposal.result
    if account_id and account_id != proposal.account_id:
        if not any(a["id"] == account_id for a in proposal.accounts):
            raise ActionError(422, "account_not_writable", "that account cannot be written to")
        proposal.account_id = account_id
    if title is not None and title.strip():
        proposal.title = " ".join(title.split())[:200]
    new_start = _parse_override(start, proposal.tz, "start")
    new_end = _parse_override(end, proposal.tz, "end")
    if new_start is not None:
        shift = (proposal.end - proposal.start) if new_end is None else None
        proposal.start = new_start
        if shift is not None:
            proposal.end = new_start + shift
    if new_end is not None:
        proposal.end = new_end
    if proposal.end <= proposal.start:
        raise ActionError(422, "invalid_time", "end must be after start")

    account = next((a for a in proposal.accounts if a["id"] == proposal.account_id), None)
    runner = runner_for(state)
    try:
        ev = await runner.write_event(
            proposal.account_id,
            EventWrite(
                summary=proposal.title,
                start=proposal.start,
                end=proposal.end,
                tz=proposal.tz,
                location=proposal.location,
                all_day=proposal.all_day,
                event_id=provider_event_id(proposal.id),
            ),
        )
    except TransportError as exc:
        if exc.code in ("consent_required", "no_credential", "auth_failed"):
            raise ActionError(
                409,
                "consent_required",
                "Grant calendar write access to add events.",
                connection_id=account["connection_id"] if account else None,
            ) from exc
        raise ActionError(502, exc.code, redact_message(exc.message)) from exc
    event_id = event_id_for(proposal.account_id, ev.ical_uid)
    store.record_created(event_id, proposal.sub, proposal.account_id, ev)
    proposal.result = created_view(proposal, ev, store)
    return proposal.result


async def undo_created(state: Any, store: ProposalStore, event_id: str, sub: str) -> dict[str, Any]:
    created = store.get_created(event_id, sub)
    if created is None:
        raise ActionError(404, "undo_not_found", "nothing to undo for this event")
    try:
        await runner_for(state).delete_event(created.account_id, created.event)
    except TransportError as exc:
        if exc.code == "consent_required":
            raise ActionError(409, "consent_required",
                              "Grant calendar write access to remove events.") from exc
        raise ActionError(502, exc.code, redact_message(exc.message)) from exc
    store.forget_created(event_id)
    return {"undone": True, "event_id": event_id}


# ---------------------------------------------------------------------------
# Entry point used by AskService
# ---------------------------------------------------------------------------
def propose_calendar_event(
    transport: Any,
    model: str,
    question: str,
    ctx: CalendarContext,
) -> dict[str, Any] | None:
    """Run the calendar action for one Ask request.

    Returns ``None`` to fall through to the normal grounded answer. Otherwise a
    dict ``{"status", "answer", ...}`` the caller merges into the Ask body:
    ``proposal`` (confirm-first), ``created`` (setting off) or ``answered``
    with an explanation (e.g. a past date, no writable account).
    """
    default = _match(ctx.accounts, default_account_name())
    tz_name = user_timezone((default or {}).get("timezone") if default else None)
    zone = resolve_zone(tz_name, "UTC")
    now_local = ctx.now().astimezone(zone)
    try:
        draft = extract_event_draft(transport, model, question, now_local, tz_name)
    except DraftError as exc:
        return {"status": "answered", "answer": str(exc)}
    if draft is None:
        return None
    account = resolve_account(
        ctx.accounts, account_id=ctx.account_id, hint=draft.calendar_hint, question=question
    )
    if account is None:
        return {
            "status": "answered",
            "answer": "I understood the event but no Google calendar is connected for "
                      "writing. Connect one under Connections and ask again.",
        }
    account_tz = user_timezone(account.get("timezone"))
    if account_tz != tz_name:  # keep the wall-clock the user said, in the account's zone
        zone = resolve_zone(account_tz, "UTC")
        draft = draft.model_copy(update={
            "start": draft.start.replace(tzinfo=zone),
            "end": (draft.end or draft.start).replace(tzinfo=zone),
        })
    proposal = Proposal(
        id=ctx.store.new_id(),
        sub=ctx.sub,
        title=draft.title,
        start=draft.start,
        end=draft.end,
        tz=account_tz,
        account_id=account["id"],
        all_day=draft.all_day,
        location=draft.location,
        accounts=[public_account(a) | {"connection_id": a["connection_id"]}
                  for a in ctx.accounts],
        warnings=list(draft.warnings),
        assumptions=list(draft.assumptions),
    )
    try:
        proposal.conflicts = ctx.run(_conflicts(ctx.state, proposal.start, proposal.end))
    except Exception as exc:  # noqa: BLE001 - conflicts are advisory
        log.warning("ask.calendar.conflicts_failed", error=redact_message(exc))
    ctx.store.put(proposal)
    view = proposal_view(proposal)

    if not confirm_first():
        try:
            result = ctx.run(confirm_proposal(ctx.state, ctx.store, proposal))
        except ActionError as exc:
            proposal.warnings.append(exc.message)
            view = proposal_view(proposal)
            if exc.code == "consent_required":
                view["consent_required"] = exc.body()
        except Exception as exc:  # noqa: BLE001
            log.warning("ask.calendar.create_failed", error=redact_message(exc))
            proposal.warnings.append("I could not add the event; confirm to retry.")
            view = proposal_view(proposal)
        else:
            return {
                "status": "created",
                "answer": _summary(proposal, "Added") + ".",
                "proposal_id": proposal.id,
                "conflicts": proposal.conflicts,
                "warnings": proposal.warnings,
                "assumptions": proposal.assumptions,
                **result,
            }
    return {
        "status": "proposal",
        "answer": _summary(proposal, "I can add") + ". Confirm?",
        "proposal": view,
    }
