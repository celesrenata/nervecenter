"""Ask actions: "remove all my future appointments with Laura".

Flow (kept out of the grounded citation flow in :mod:`backend.context.ask`;
this module must not import it):

1. :func:`looks_like_calendar_delete` is a cheap regex gate.
2. :func:`extract_delete_intent` makes one ``kind="extract"`` call for the
   person. The person must occur in the request text (checked in code), and
   the scope (``all_future`` / ``next`` / ``date`` / ``ambiguous``) comes from
   the wording, never from the model.
3. :func:`propose_calendar_delete` searches **live Google** on every writable
   account (series masters and single events), classifies each hit as a
   strong or possible match, and stores a :class:`DeletionProposal`. Nothing
   is written. The card lists every row; only Confirm deletes.
4. :func:`confirm_deletion` acts on stored rows only. A series is ended with
   one request (organiser: ``RRULE`` ``UNTIL`` now, past kept; attendee: the
   whole series is removed from the owner's calendar). Only if that fails does
   it delete occurrences one by one, and then at most ``MAX_INSTANCES`` inside
   ``HORIZON_DAYS``; it reports plainly that an open-ended series is still
   open beyond that and what the owner can do.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta
from typing import Any

import structlog

from backend.calendar.delete_proposals import (
    DeletionProposal,
    DeletionStore,
    get_delete_store,
)
from backend.calendar.providers import ProviderEvent, TransportError
from backend.calendar.series import (
    HORIZON_DAYS,
    MAX_INSTANCES,
    is_open_ended,
    repeats_label,
    series_starts_in_future,
)
from backend.connectors.redact import redact_message
from backend.context.ask_actions import (
    ActionError,
    CalendarContext,
    _parse_model_json,
    public_account,
    runner_for,
)

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Gate and wording
# ---------------------------------------------------------------------------
_VERBS = r"(?:remove|delete|cancel|clear|drop|take\s+off|get\s+rid\s+of|erase)"
_NOUNS = r"(?:appointment|meeting|event|session|therapy|calendar|reminder)"
_DELETE_RE = re.compile(
    rf"\b{_VERBS}\b[^?!.]{{0,120}}?\b{_NOUNS}s?\b|\b{_NOUNS}s?\b[^?!.]{{0,60}}?\b{_VERBS}\b"
    rf"|\btake\b[^?!.]{{0,80}}?\b{_NOUNS}s?\b[^?!.]{{0,40}}?\boff\b",
    re.IGNORECASE,
)


def looks_like_calendar_delete(question: str | None) -> bool:
    """Cheap gate: does the request ask to take something off the calendar?"""
    return bool(question and _DELETE_RE.search(question))


_ALL_RE = re.compile(
    r"\b(all|every|any|future|upcoming|remaining|rest\s+of|going\s+forward|from\s+now\s+on|anymore|any\s+more)\b",
    re.IGNORECASE,
)
_NEXT_RE = re.compile(r"\b(next|first|this|tomorrow|today|tonight|coming)\b", re.IGNORECASE)
_DATE_RE = re.compile(
    r"\b(?:on\s+)?(?:mon|tues?|wednes|thur?s?|fri|satur|sun)day\b|\b\d{1,2}[/-]\d{1,2}\b|"
    r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{1,2}\b",
    re.IGNORECASE,
)


def scope_from_wording(question: str) -> str:
    """``all_future`` | ``next`` | ``date`` | ``ambiguous``, decided in code."""
    if _ALL_RE.search(question):
        return "all_future"
    if _NEXT_RE.search(question):
        return "next"
    if _DATE_RE.search(question):
        return "date"
    return "ambiguous"


SCOPE_CHOICES = [
    {"id": "next", "label": "Only the next one"},
    {"id": "all_future", "label": "All future ones"},
]

# ---------------------------------------------------------------------------
# Intent
# ---------------------------------------------------------------------------
_NAME_RE = re.compile(r"(?<![.!?]\s)(?<!^)\b([A-Z][a-z]{2,})\b")
_NOT_NAMES = {"the", "and", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday",
              "sunday", "january", "february", "march", "april", "june", "july", "august",
              "september", "october", "november", "december", "google", "calendar"}


def _prompt(question: str) -> str:
    return "\n".join([
        "You read a request to remove calendar entries and name the person it is about.",
        "Reply with one JSON object and nothing else.",
        'If the request is not asking to remove calendar entries reply {"is_delete": false}.',
        'Otherwise reply {"is_delete": true, "person": the person\'s name exactly as written in '
        'the request, "topic": a short word like therapy or null}.',
        'If the person is only "her" or "him" and no name appears, use null for person.',
        "Do not invent names that are not in the request.",
        f"Request: {question}",
    ])


def extract_delete_intent(transport: Any, model: str, question: str) -> dict[str, Any] | None:
    """One extraction call. ``None`` means "not a delete request" (fall through).
    Otherwise ``{"person": str | None, "topic": str | None, "scope": str}``."""
    try:
        if hasattr(transport, "check"):
            result = transport.generate(model, _prompt(question), kind="extract",
                                        sensitive=True, max_tokens=200)
        else:
            result = transport.generate(model, _prompt(question))
    except Exception as exc:  # noqa: BLE001 - extraction is best effort
        log.warning("ask.calendar.delete_extract_failed", error=type(exc).__name__)
        return None
    data = _parse_model_json(str((result or {}).get("content", "")))
    if not data or data.get("is_delete") is False:
        return None
    low = question.lower()
    person = str(data.get("person") or "").strip()
    if not person or person.lower() not in low:  # the name must be in the request text
        person = next(
            (m.group(1) for m in _NAME_RE.finditer(question) if m.group(1).lower() not in _NOT_NAMES),
            "",
        )
    topic = str(data.get("topic") or "").strip()
    return {
        "person": person or None,
        "topic": topic[:40] if topic and topic.lower() in low else None,
        "scope": scope_from_wording(question),
    }


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------
def _word_in(needle: str, text: str | None) -> bool:
    if not needle or not text:
        return False
    return bool(re.search(rf"(?<![A-Za-z0-9]){re.escape(needle)}(?![A-Za-z0-9])", text, re.IGNORECASE))


def contacts_named(contacts: list[Any], person: str) -> list[Any]:
    tokens = [t for t in re.split(r"\s+", person.lower()) if t]
    return [c for c in contacts if tokens and all(t in c.display_name.lower().split() or
                                                   t in c.display_name.lower() for t in tokens)]


def match_events(
    events: list[ProviderEvent], person: str, contacts: list[Any]
) -> list[tuple[ProviderEvent, str]]:
    """``(event, "strong" | "possible")`` for every event that mentions the person.

    Strong: the name as a whole word in the title or an attendee's display
    name, or an attendee email equal to the one contact with that name.
    Possible: the name only in the description or location. When more than
    one contact has that name nothing is strong, because the event does not
    tell them apart.
    """
    known = contacts_named(contacts, person)
    ambiguous = len(known) > 1
    emails = {e.lower() for c in known for e in (c.emails or [])} if len(known) == 1 else set()
    out: list[tuple[ProviderEvent, str]] = []
    for ev in events:
        extra = ev.extra or {}
        title_hit = _word_in(person, ev.title)
        attendee_hit = any(_word_in(person, n) for n in extra.get("attendee_names") or [])
        email_hit = bool(emails & {str(a).lower() for a in extra.get("attendees") or [] if a})
        text_hit = _word_in(person, ev.description) or _word_in(person, ev.location)
        if title_hit or attendee_hit or email_hit:
            out.append((ev, "possible" if ambiguous else "strong"))
        elif text_hit:
            out.append((ev, "possible"))
    return out


# ---------------------------------------------------------------------------
# Proposal
# ---------------------------------------------------------------------------
def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _row_id(account_id: str, uid: str) -> str:
    return "r" + hashlib.sha1(f"{account_id}:{uid}".encode()).hexdigest()[:10]  # noqa: S324 - id only


def plan_path(kind: str, starts_in_future: bool, organizer_self: bool) -> str:
    if kind == "single":
        return "delete"
    if starts_in_future:
        return "series_deleted"
    return "series_end" if organizer_self else "series_removed"


def _path_label(path: str, fallback: dict[str, Any] | None) -> str:
    if path == "delete":
        return "Deletes this event."
    if path == "series_deleted":
        return "Deletes the whole series. It has not started yet, so no past occurrences are lost."
    if path == "series_end":
        return "Ends the series as of today. Past occurrences stay."
    text = "Removes the whole series from your calendar in one step."
    if fallback:
        text += (
            f" If Google refuses, it deletes the next {fallback['count']} occurrence(s) "
            f"through {fallback['until'][:10]} (at most {fallback['max_instances']}), and an "
            "open-ended series stays open after that."
        )
    return text


async def _upcoming_rows(
    ctx: CalendarContext, person: str, contacts: list[Any], now: datetime, scope: str
) -> tuple[list[dict[str, Any]], dict[str, Any], list[str]]:
    runner = runner_for(ctx.state)
    horizon_end = now + timedelta(days=HORIZON_DAYS)
    rows: list[dict[str, Any]] = []
    events: dict[str, Any] = {}
    failures: list[str] = []
    for account in ctx.accounts:
        try:
            found = await runner.search_upcoming(account["id"], person, time_min=now)
        except TransportError as exc:
            failures.append(f"{account['label']} ({exc.code})")
            continue
        except Exception as exc:  # noqa: BLE001 - one account failing must not hide the rest
            log.warning("ask.calendar.delete_search_failed", error=redact_message(exc))
            failures.append(str(account["label"]))
            continue
        master_ids = {e.ical_uid for e in found if (e.extra or {}).get("recurrence")}
        for ev, confidence in match_events(found, person, contacts):
            extra = ev.extra or {}
            if extra.get("recurring_event_id") in master_ids:
                continue  # covered by its series row
            is_series = bool(extra.get("recurrence"))
            rid = _row_id(account["id"], ev.ical_uid)
            base: dict[str, Any] = {
                "id": rid,
                "title": ev.title,
                "start": _iso(ev.start),
                "end": _iso(ev.end),
                "all_day": ev.all_day,
                "account": public_account(account) | {"connection_id": account["connection_id"]},
                "confidence": confidence,
            }
            if not is_series:
                start = ev.start
                if start is None or start <= now and not ev.all_day:
                    continue
                if ev.all_day and start.date() < now.date():
                    continue
                rows.append(base | {"kind": "single", "repeats": None, "next_dates": [_iso(start)],
                                    "upcoming_count": 1, "upcoming_capped": False,
                                    "path": "delete", "fallback": None})
                events[rid] = ev
                continue
            try:
                instances = await runner.list_instances(
                    account["id"], ev, time_min=now, time_max=horizon_end, limit=MAX_INSTANCES + 1
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("ask.calendar.delete_instances_failed", error=redact_message(exc))
                failures.append(f"{account['label']} (occurrences)")
                continue
            if not instances:
                continue  # the series is already over
            shown = instances[:MAX_INSTANCES]
            future_start = series_starts_in_future(ev.start, now)
            organizer = bool(extra.get("organizer_self", True))
            path = plan_path("series", future_start, organizer)
            until = _iso(shown[-1].end or shown[-1].start) or ""
            fallback = (
                {"count": len(shown), "until": until, "max_instances": MAX_INSTANCES}
                if path == "series_removed" else None
            )
            rows.append(base | {
                "kind": "series",
                "repeats": repeats_label(extra.get("recurrence")),
                "open_ended": is_open_ended(extra.get("recurrence")),
                "next_dates": [_iso(i.start) for i in instances[:3]],
                "upcoming_count": len(shown),
                "upcoming_capped": len(instances) > MAX_INSTANCES or is_open_ended(extra.get("recurrence")),
                "organizer_self": organizer,
                "path": path,
                "fallback": fallback,
            })
            events[rid] = ev
            events[rid + "#next"] = instances[0]
    rows.sort(key=lambda r: (r["next_dates"][0] or "", r["id"]))
    for row in rows:
        row["path_label"] = _path_label(row["path"], row["fallback"])
        row["selected"] = row["confidence"] == "strong"
    if scope == "next":
        strong = [r for r in rows if r["confidence"] == "strong"]
        for row in rows:
            row["selected"] = bool(strong) and row is strong[0]
    return rows, events, failures


def _view(proposal: DeletionProposal, answer_rows: int) -> dict[str, Any]:
    return {
        "id": proposal.id,
        "person": proposal.person,
        "scope": proposal.scope,
        "scope_choices": SCOPE_CHOICES if proposal.scope == "ambiguous" else [],
        "rows": proposal.rows,
        "expires_at": _iso(proposal.expires_at),
        "requires_confirmation": True,
        "limits": {"max_instances": MAX_INSTANCES, "horizon_days": HORIZON_DAYS},
    }


def _contacts(state: Any) -> list[Any]:
    service = getattr(state, "contacts_service", None)
    try:
        return list(service.list_contacts()) if service is not None else []
    except Exception:  # noqa: BLE001 - contacts only sharpen matching
        return []


def propose_calendar_delete(
    transport: Any, model: str, question: str, ctx: CalendarContext
) -> dict[str, Any] | None:
    """The deletion action for one Ask request. ``None`` falls through to the
    normal answer; otherwise ``{"status", "answer", ["delete_proposal"]}``.
    Nothing is written here."""
    intent = extract_delete_intent(transport, model, question)
    if intent is None:
        return None
    person = intent["person"]
    if not person:
        return {"status": "answered",
                "answer": "Tell me whose appointments to remove, using their name, and I will "
                          "list them for you to confirm."}
    if not ctx.accounts:
        return {"status": "answered",
                "answer": "No Google calendar is connected for writing, so there is nothing I "
                          "can remove. Connect one under Connections and ask again."}
    now = ctx.now()
    scope = intent["scope"]
    rows, events, failures = ctx.run(
        _upcoming_rows(ctx, person, _contacts(ctx.state), now, scope), timeout=120.0
    )
    if not rows:
        note = f" I could not search {', '.join(failures)}." if failures else ""
        return {"status": "answered",
                "answer": f"I found no upcoming appointments with {person} on "
                          f"{', '.join(a['label'] for a in ctx.accounts)}, so nothing needs removing.{note}"}
    store = get_delete_store(ctx.state)
    proposal = DeletionProposal(
        id=store.new_id(), sub=ctx.sub, person=person, scope=scope, rows=rows, events=events,
        cutoff=now,
    )
    store.put(proposal)
    selected = sum(1 for r in rows if r["selected"])
    unsure = sum(1 for r in rows if r["confidence"] == "possible")
    answer = f"I found {len(rows)} calendar entr{'y' if len(rows) == 1 else 'ies'} that may involve {person}"
    answer += f"; {selected} selected" + (f", {unsure} not sure" if unsure else "") + "."
    if scope == "ambiguous":
        answer += " Do you want only the next one or all future ones? Choose below."
    answer += " Nothing is deleted until you press Confirm."
    if failures:
        answer += f" I could not search {', '.join(failures)}."
    return {"status": "delete_proposal", "answer": answer, "delete_proposal": _view(proposal, len(rows))}


# ---------------------------------------------------------------------------
# Confirm
# ---------------------------------------------------------------------------
def _consent(exc: TransportError) -> bool:
    return exc.code in ("consent_required", "no_credential", "auth_failed")


async def _delete_series_row(
    runner: Any, account_id: str, master: ProviderEvent, row: dict[str, Any], cutoff: datetime
) -> dict[str, Any]:
    horizon = timedelta(days=HORIZON_DAYS)
    try:
        await runner.delete_series(account_id, master, cutoff)
        return {"status": "deleted", "path": row["path"],
                "detail": "Removed the whole series." if row["path"] == "series_removed"
                else "Deleted the whole series."}
    except TransportError as exc:
        if _consent(exc):
            raise
        first_error = exc.code
    # Bounded fallback: never walks an open-ended series.
    out = await runner.delete_instances(
        account_id, master, since=cutoff, max_instances=MAX_INSTANCES, horizon=horizon
    )
    until = _iso(out["until"]) or ""
    detail = (f"Google would not remove the whole series ({first_error}). Deleted {out['deleted']} "
              f"upcoming occurrence(s) through {until[:10]}.")
    if out["still_open"]:
        detail += (" The series is still open after that date: ask the organiser to end it, or "
                   "decline the whole series in Google Calendar.")
    return {"status": "instances_deleted", "path": "instances_deleted", "detail": detail,
            "deleted": out["deleted"], "until": until, "still_open": out["still_open"]}


async def confirm_deletion(
    state: Any,
    store: DeletionStore,
    proposal: DeletionProposal,
    *,
    item_ids: list[str] | None,
    scope: str | None,
) -> dict[str, Any]:
    """Act on the stored rows the person selected. Idempotent: a repeat confirm
    returns the first result. Browser ids that are not in the proposal are refused."""
    if proposal.result is not None:
        return proposal.result
    effective = proposal.scope
    if effective == "ambiguous":
        if scope not in ("next", "all_future"):
            raise ActionError(422, "scope_required",
                              "Choose whether to remove only the next one or all future ones.")
        effective = scope
    known = {r["id"]: r for r in proposal.rows}
    if item_ids is None:
        chosen = [r["id"] for r in proposal.rows if r["selected"]]
    else:
        bad = [i for i in item_ids if i not in known]
        if bad:
            raise ActionError(422, "unknown_item", "That item is not part of this proposal.")
        chosen = list(dict.fromkeys(item_ids))
    if not chosen:
        raise ActionError(422, "nothing_selected", "Select at least one item to delete.")

    runner = runner_for(state)
    results: list[dict[str, Any]] = []
    consent_connection: str | None = None
    for rid in chosen:
        row = known[rid]
        ev = proposal.events[rid]
        account_id = row["account"]["id"]
        try:
            if row["kind"] == "single":
                removed = await runner.delete_event(account_id, ev)
                out = {"status": "deleted" if removed else "not_found", "path": "delete",
                       "detail": "Deleted." if removed else "It was already gone."}
            elif effective in ("next", "date"):
                inst = proposal.events[rid + "#next"]
                removed = await runner.delete_event(account_id, inst)
                out = {"status": "deleted" if removed else "not_found", "path": "occurrence",
                       "detail": "Deleted the next occurrence only; the series continues."}
            elif row["path"] == "series_end":
                await runner.end_series(account_id, ev, proposal.cutoff)
                out = {"status": "series_ended", "path": "series_end",
                       "detail": "Ended the series as of today. Past occurrences stay."}
            else:
                out = await _delete_series_row(runner, account_id, ev, row, proposal.cutoff)
        except TransportError as exc:
            if _consent(exc):
                consent_connection = consent_connection or row["account"].get("connection_id")
                out = {"status": "failed", "path": row["path"], "detail": "consent_required"}
            else:
                out = {"status": "failed", "path": row["path"], "detail": redact_message(exc.message)}
        except Exception as exc:  # noqa: BLE001
            log.warning("ask.calendar.delete_failed", error=redact_message(exc))
            out = {"status": "failed", "path": row["path"], "detail": "Google did not accept the change."}
        results.append({"id": rid, "title": row["title"], **out})

    if consent_connection is not None and all(r["status"] == "failed" for r in results):
        raise ActionError(409, "consent_required", "Grant calendar write access to remove events.",
                          connection_id=consent_connection)
    done = sum(1 for r in results if r["status"] != "failed")
    result = {
        "status": "done",
        "proposal_id": proposal.id,
        "results": results,
        "summary": f"{done} of {len(results)} item(s) removed.",
        "consent_required": ({"code": "consent_required", "connection_id": consent_connection}
                             if consent_connection else None),
    }
    if all(r["status"] != "failed" for r in results):
        proposal.result = result  # a repeat confirm replays it; a partial failure may be retried
    return result
