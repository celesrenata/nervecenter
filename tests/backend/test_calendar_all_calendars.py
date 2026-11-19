"""Bug condition exploration: Google sync must cover every selected readable
calendar, not only the primary one (spec calendar-sync-all-calendars).

Property 1: these tests encode the EXPECTED behavior and FAIL on unfixed code.
"""
from __future__ import annotations

import json
import pathlib
import urllib.parse
from datetime import UTC, datetime
from typing import Any

import backend.api.app  # noqa: F401  (import first: avoids a circular import)
from backend.calendar.providers import GoogleCalendarProvider, HttpResponse
from backend.calendar.service import CalendarService
from backend.calendar.sync import CalendarSyncRunner

from tests.backend.test_calendar_integration import (
    _build_registry,
    _make_engine,
    _token_response,
)

PRIMARY = "me@example.com"
SHARED = "pam@group.calendar.google.com"
EVENTS = "https://www.googleapis.com/calendar/v3/calendars/"
LIST_URL = "https://www.googleapis.com/calendar/v3/users/me/calendarList"
WIN_START = datetime(2026, 11, 14, tzinfo=UTC)
WIN_END = datetime(2027, 1, 14, tzinfo=UTC)


def _ev(uid: str, summary: str, start: str, status: str = "confirmed") -> dict[str, Any]:
    return {
        "id": uid, "summary": summary, "status": status,
        "start": {"dateTime": start},
        "end": {"dateTime": start.replace("T10", "T11")},
    }


def _json(body: dict[str, Any]) -> HttpResponse:
    return HttpResponse(200, {}, json.dumps(body).encode("utf-8"))


class Scripted:
    """Transport: calendarList pages plus per-calendar event items."""

    def __init__(
        self,
        list_pages: list[list[dict[str, Any]]],
        events: dict[str, list[dict[str, Any]]],
    ) -> None:
        self.list_pages = list_pages
        self.events = events
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    async def request(self, method, url, *, headers=None, params=None, body=None):
        params = dict(params or {})
        self.calls.append((method, url, params))
        if url == "https://oauth2.googleapis.com/token":
            return _token_response()
        if url == LIST_URL:
            idx = int(params.get("pageToken", "0"))
            body_: dict[str, Any] = {"items": self.list_pages[idx]}
            if idx + 1 < len(self.list_pages):
                body_["nextPageToken"] = str(idx + 1)
            return _json(body_)
        if url.startswith(EVENTS) and url.endswith("/events"):
            cal = urllib.parse.unquote(url[len(EVENTS):-len("/events")])
            return _json({"items": self.events.get(cal, [])})
        return HttpResponse(404, {}, b"not found")

    def event_calendars(self) -> list[str]:
        return [
            urllib.parse.unquote(u[len(EVENTS):-len("/events")])
            for _, u, _ in self.calls
            if u.startswith(EVENTS) and u.endswith("/events")
        ]


def _primary() -> dict[str, Any]:
    return {"id": PRIMARY, "primary": True, "selected": True,
            "accessRole": "owner", "summary": "Me"}


def _shared() -> dict[str, Any]:
    return {"id": SHARED, "selected": True, "accessRole": "reader", "summary": "Pam"}


PAM = _ev("pam-1", "see Pam", "2026-12-11T10:00:00Z")
MINE = _ev("mine-1", "Standup", "2026-11-20T10:00:00Z")


async def _sync(transport: Scripted):
    provider = GoogleCalendarProvider(transport, secret="rt", config={"client_id": "c"})
    return await provider.sync(window_start=WIN_START, window_end=WIN_END)


async def test_shared_calendar_event_is_synced() -> None:
    t = Scripted([[_primary(), _shared()]], {PRIMARY: [MINE], SHARED: [PAM]})
    result = await _sync(t)
    assert SHARED in t.event_calendars()
    pam = [e for e in result.events if e.title == "see Pam"]
    assert len(pam) == 1
    assert pam[0].extra.get("calendar_id") == SHARED


async def test_calendar_list_second_page_is_followed() -> None:
    t = Scripted([[_primary()], [_shared()]], {PRIMARY: [MINE], SHARED: [PAM]})
    result = await _sync(t)
    assert SHARED in t.event_calendars()
    assert any(e.title == "see Pam" for e in result.events)


async def test_runner_stores_one_row_and_lists_both_calendars(
    tmp_path: pathlib.Path,
) -> None:
    engine = await _make_engine(tmp_path / "all.db")
    registry = _build_registry(engine)
    service = CalendarService()
    t = Scripted([[_primary(), _shared()]], {PRIMARY: [MINE], SHARED: [PAM]})
    acct = await registry.calendar.create_account(
        provider="google", timezone="UTC", secret_ref="vault://cal",
        config={"client_id": "cid", "client_secret": "cs"},
    )
    await registry.connections.upsert_secret(
        kind="calendar_credential", owner_kind="connection", owner_id=acct.id,
        plaintext=json.dumps({"refresh_token": "rt"}),
    )
    await registry.calendar.update_account(acct.id, secret_ref="vault://cal")
    runner = CalendarSyncRunner(registry, calendar_service=service, transport=t)
    await runner.sync_account(acct.id)
    rows = await registry.calendar.list_events(
        start=datetime(2026, 12, 11, tzinfo=UTC), end=datetime(2026, 12, 12, tzinfo=UTC)
    )
    pam_rows = [r for r in rows if "pam" in (r.title or "").lower()]
    assert len(pam_rows) == 1
    assert (pam_rows[0].extra or {}).get("calendar_id") == SHARED
    acct2 = await registry.calendar.get_account(acct.id)
    assert acct2.sync_state["last_calendars"] == [PRIMARY, SHARED]
    await engine.dispose()


async def test_same_event_on_two_calendars_yields_one_row() -> None:
    dup = _ev("dup-1", "Team lunch", "2026-12-02T10:00:00Z")
    t = Scripted([[_primary(), _shared()]], {PRIMARY: [dup], SHARED: [dup]})
    result = await _sync(t)
    assert SHARED in t.event_calendars()
    rows = [e for e in result.events if e.ical_uid == "dup-1"]
    assert len(rows) == 1
    assert set(rows[0].extra.get("calendar_ids", [])) == {PRIMARY, SHARED}
