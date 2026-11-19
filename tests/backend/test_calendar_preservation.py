"""Preservation properties for Google calendar sync (spec
calendar-sync-all-calendars, Property 2).

Observed on the UNFIXED code and expected to keep holding after the fix:
``calendar_ids`` override, primary-only accounts, id/ref shape, write target,
deletion markers, request parameters/window, and OAuth scopes.
"""
from __future__ import annotations

import asyncio
import json
import pathlib
import urllib.parse
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

import backend.api.app  # noqa: F401  (import first: avoids a circular import)
from backend.calendar.providers import (
    DEFAULT_BACK_WINDOW,
    DEFAULT_FORE_WINDOW,
    GoogleCalendarProvider,
    HttpResponse,
)
from backend.calendar.service import CalendarService
from backend.calendar.sync import CalendarSyncRunner, event_ical_id
from backend.connectors.registry import EXTRA_SCOPES
from tests.backend.test_calendar_integration import (
    _build_registry,
    _make_engine,
    _token_response,
)

EVENTS = "https://www.googleapis.com/calendar/v3/calendars/"
LIST_URL = "https://www.googleapis.com/calendar/v3/users/me/calendarList"
PRIMARY = "me@example.com"
NOW = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
WIN_START = NOW - DEFAULT_BACK_WINDOW
WIN_END = NOW + DEFAULT_FORE_WINDOW


def _json(body: dict[str, Any]) -> HttpResponse:
    return HttpResponse(200, {}, json.dumps(body).encode("utf-8"))


def _ev(uid: str, status: str = "confirmed", day: int = 20) -> dict[str, Any]:
    return {
        "id": uid, "summary": f"ev {uid}", "status": status,
        "start": {"dateTime": f"2026-11-{day:02d}T10:00:00Z"},
        "end": {"dateTime": f"2026-11-{day:02d}T11:00:00Z"},
    }


class Scripted:
    """Scripted transport: one calendarList page plus per-calendar events."""

    def __init__(self, entries: list[dict[str, Any]], events: dict[str, list[dict[str, Any]]]):
        self.entries = entries
        self.events = events
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    async def request(self, method, url, *, headers=None, params=None, body=None):
        params = dict(params or {})
        self.calls.append((method, url, params))
        if url == "https://oauth2.googleapis.com/token":
            return _token_response()
        if url == LIST_URL:
            return _json({"items": self.entries})
        if url.startswith(EVENTS) and url.endswith("/events"):
            cal = urllib.parse.unquote(url[len(EVENTS):-len("/events")])
            return _json({"items": self.events.get(cal, [])})
        return HttpResponse(404, {}, b"not found")

    def list_calls(self) -> list[str]:
        return [u for _, u, _ in self.calls if u == LIST_URL]

    def event_calls(self) -> list[tuple[str, dict[str, Any]]]:
        return [
            (urllib.parse.unquote(u[len(EVENTS):-len("/events")]), p)
            for _, u, p in self.calls
            if u.startswith(EVENTS) and u.endswith("/events")
        ]


def _provider(t: Scripted, config: dict[str, Any] | None = None) -> GoogleCalendarProvider:
    cfg = {"client_id": "c", **(config or {})}
    return GoogleCalendarProvider(t, secret="rt", config=cfg)


def _run_sync(t: Scripted, config: dict[str, Any] | None = None):
    return asyncio.run(
        _provider(t, config).sync(window_start=WIN_START, window_end=WIN_END)
    )


def _assert_request_shape(t: Scripted) -> None:
    calls = t.event_calls()
    assert calls
    for _, p in calls:
        assert p["singleEvents"] == "true"
        assert p["showDeleted"] == "true"
        assert p["maxResults"] == "250"
        assert "syncToken" not in p
        assert p["timeMin"] == "2026-11-06T12:00:00Z"  # now - 14 days
        assert p["timeMax"] == "2027-01-19T12:00:00Z"  # now + 60 days


# -- strategies --------------------------------------------------------------

_uid = st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789", min_size=3, max_size=12)
_status = st.sampled_from(["confirmed", "tentative", "cancelled"])
_events = st.lists(
    st.tuples(_uid, _status, st.integers(min_value=7, max_value=28)),
    min_size=0, max_size=6, unique_by=lambda e: e[0],
)
_cal_ids = st.lists(
    st.from_regex(r"[a-z]{3,8}@group\.calendar\.google\.com", fullmatch=True),
    min_size=1, max_size=4, unique=True,
)
_extras = st.lists(
    st.sampled_from([
        {"id": "unsel@group.calendar.google.com", "selected": False, "accessRole": "reader"},
        {"id": "hid@group.calendar.google.com", "selected": True, "hidden": True,
         "accessRole": "reader"},
        {"id": "del@group.calendar.google.com", "selected": True, "deleted": True,
         "accessRole": "reader"},
        {"id": "fb@group.calendar.google.com", "selected": True,
         "accessRole": "freeBusyReader"},
    ]),
    max_size=4, unique_by=lambda e: e["id"],
)
_SETTINGS = settings(max_examples=40, deadline=None,
                     suppress_health_check=[HealthCheck.function_scoped_fixture])


def _items(evs: list[tuple[str, str, int]]) -> list[dict[str, Any]]:
    return [_ev(uid, status, day) for uid, status, day in evs]


# -- 3.1 / 3.5 / 3.6: calendar_ids override ---------------------------------

@_SETTINGS
@given(cals=_cal_ids, evs=_events)
def test_calendar_ids_override_queries_exactly_those_in_order(cals, evs) -> None:
    t = Scripted([], {c: _items(evs) for c in cals})
    result = _run_sync(t, {"calendar_ids": cals})
    assert t.list_calls() == []
    assert [c for c, _ in t.event_calls()] == cals
    assert result.calendars == cals
    _assert_request_shape(t)
    assert [e.ical_uid for e in result.events] == [u for u, _, _ in evs] * len(cals)


def test_calendar_ids_override_skips_filters() -> None:
    # An override id is queried even though nothing marks it selected/readable.
    cal = "odd@group.calendar.google.com"
    t = Scripted([{"id": cal, "selected": False, "accessRole": "freeBusyReader"}],
                 {cal: [_ev("x1")]})
    result = _run_sync(t, {"calendar_ids": [cal]})
    assert t.list_calls() == []
    assert [e.ical_uid for e in result.events] == ["x1"]


# -- 3.2 / 3.4 / 3.5 / 3.6: primary-only accounts ---------------------------

@_SETTINGS
@given(evs=_events, extras=_extras, has_selected=st.booleans())
def test_primary_only_account_rows_and_requests(evs, extras, has_selected) -> None:
    primary = {"id": PRIMARY, "primary": True, "accessRole": "owner"}
    if has_selected:
        primary["selected"] = True
    t = Scripted([*extras, primary], {PRIMARY: _items(evs)})
    result = _run_sync(t)
    assert t.list_calls() == [LIST_URL]
    assert result.calendars == [PRIMARY]
    assert [c for c, _ in t.event_calls()] == [PRIMARY]
    _assert_request_shape(t)
    assert [e.ical_uid for e in result.events] == [u for u, _, _ in evs]
    assert [e.deleted for e in result.events] == [s == "cancelled" for _, s, _ in evs]
    assert [e.status for e in result.events] == [s for _, s, _ in evs]
    for e in result.events:
        assert event_ical_id("acct", e.ical_uid) == f"acct:{e.ical_uid}"


def test_window_constants_unchanged() -> None:
    assert DEFAULT_BACK_WINDOW == timedelta(days=14)
    assert DEFAULT_FORE_WINDOW == timedelta(days=60)


# -- runner level: ids, Ask ref, deletion marker ----------------------------

async def _harness(tmp_path: pathlib.Path, transport: Scripted, config=None):
    engine = await _make_engine(tmp_path / "pres.db")
    registry = _build_registry(engine)
    service = CalendarService()
    acct = await registry.calendar.create_account(
        provider="google", timezone="UTC", secret_ref="vault://cal",
        config={"client_id": "cid", "client_secret": "cs", **(config or {})},
    )
    await registry.connections.upsert_secret(
        kind="calendar_credential", owner_kind="connection", owner_id=acct.id,
        plaintext=json.dumps({"refresh_token": "rt"}),
    )
    await registry.calendar.update_account(acct.id, secret_ref="vault://cal")
    runner = CalendarSyncRunner(registry, calendar_service=service, transport=transport)
    return engine, registry, service, acct, runner


async def test_primary_only_runner_ids_and_ask_ref(tmp_path: pathlib.Path) -> None:
    now = datetime.now(UTC)
    day = (now + timedelta(days=3)).strftime("%Y-%m-%d")
    ev = {"id": "evt-1", "summary": "Dentist", "status": "confirmed",
          "start": {"dateTime": f"{day}T10:00:00Z"}, "end": {"dateTime": f"{day}T11:00:00Z"}}
    t = Scripted([{"id": PRIMARY, "primary": True, "accessRole": "owner"}], {PRIMARY: [ev]})
    engine, registry, service, acct, runner = await _harness(tmp_path, t)
    summary = await runner.sync_account(acct.id)
    assert summary["synced"] is True
    rows = await registry.calendar.list_events(
        start=now, end=now + timedelta(days=10)
    )
    row = next(r for r in rows if r.title == "Dentist")
    assert row.ical_uid == "evt-1"
    assert service.get_event(f"{acct.id}:evt-1") is not None
    # Ask retrieval builds its citation ref as account_id:ical_uid.
    assert f"{row.account_id}:{row.ical_uid}" == f"{acct.id}:evt-1"
    state = (await registry.calendar.get_account(acct.id)).sync_state
    assert state["last_calendars"] == [PRIMARY]
    await engine.dispose()


async def test_cancelled_event_marker_audit_row_and_memory(tmp_path: pathlib.Path) -> None:
    now = datetime.now(UTC)
    day = (now + timedelta(days=2)).strftime("%Y-%m-%d")
    gone = {"id": "gone", "summary": "Will vanish", "status": "cancelled",
            "start": {"dateTime": f"{day}T10:00:00Z"}, "end": {"dateTime": f"{day}T11:00:00Z"}}
    t = Scripted([{"id": PRIMARY, "primary": True, "accessRole": "owner"}], {PRIMARY: [gone]})
    engine, registry, service, acct, runner = await _harness(tmp_path, t)
    await registry.calendar.upsert_event(
        account_id=acct.id, ical_uid="gone", title="Will vanish",
        start=now + timedelta(days=2), end=now + timedelta(days=2, hours=1),
    )
    service.ingest({
        "event_id": f"{acct.id}:gone", "title": "Will vanish",
        "start": (now + timedelta(days=2)).isoformat(),
        "end": (now + timedelta(days=2, hours=1)).isoformat(),
    })
    assert service.get_event(f"{acct.id}:gone") is not None
    summary = await runner.sync_account(acct.id)
    assert summary["deleted"] == 1
    assert service.get_event(f"{acct.id}:gone") is None
    rows = await registry.calendar.list_events(
        start=now, end=now + timedelta(days=10), include_deleted=True
    )
    assert any(r.ical_uid == "gone" and r.deleted for r in rows)
    live = await registry.calendar.list_events(start=now, end=now + timedelta(days=10))
    assert not any(r.ical_uid == "gone" for r in live)
    await engine.dispose()


# -- 3.3: write target -------------------------------------------------------

@pytest.mark.parametrize(
    ("config", "expected"),
    [
        ({}, "primary"),
        ({"calendar_ids": []}, "primary"),
        ({"calendar_ids": ["a@group.calendar.google.com"]}, "a@group.calendar.google.com"),
        ({"calendar_ids": ["a@group.calendar.google.com", "b@group.calendar.google.com"]},
         "a@group.calendar.google.com"),
    ],
)
async def test_writable_provider_target(
    tmp_path: pathlib.Path, config: dict[str, Any], expected: str
) -> None:
    # A rich calendarList must not influence the write target.
    t = Scripted(
        [{"id": PRIMARY, "primary": True, "accessRole": "owner"},
         {"id": "pam@group.calendar.google.com", "selected": True, "accessRole": "reader"}],
        {},
    )
    engine, _registry, _service, acct, runner = await _harness(tmp_path, t, config)
    _account, provider, calendar_id = await runner._writable_provider(acct.id)
    assert isinstance(provider, GoogleCalendarProvider)
    assert calendar_id == expected
    assert t.list_calls() == []
    await engine.dispose()


# -- 3.7: OAuth scopes -------------------------------------------------------

def test_oauth_extra_scopes_unchanged() -> None:
    assert EXTRA_SCOPES == {
        ("google", "calendar"): "https://www.googleapis.com/auth/calendar.readonly",
        ("google", "calendar_write"): "https://www.googleapis.com/auth/calendar.events",
    }
