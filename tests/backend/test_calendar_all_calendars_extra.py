"""Orphan tombstoning, error isolation, truncation and Ask visibility for the
all-calendars Google sync (spec calendar-sync-all-calendars, task 3.5)."""
from __future__ import annotations

import json
import pathlib
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import backend.api.app  # noqa: F401  (import first: avoids a circular import)
from backend.calendar.providers import GoogleCalendarProvider, HttpResponse, MAX_EVENT_PAGES
from backend.calendar.service import CalendarService
from backend.calendar.sync import CalendarSyncRunner
from backend.context.retrieval.intent import parse_intent
from backend.context.retrieval.sources.communications import calendar as ask_calendar
from backend.context.retrieval.policy import record_id

from tests.backend.test_calendar_all_calendars import (
    EVENTS,
    MINE,
    PAM,
    PRIMARY,
    SHARED,
    WIN_END,
    WIN_START,
    Scripted,
    _json,
    _primary,
    _shared,
)
from tests.backend.test_calendar_integration import _build_registry, _make_engine


class Failing(Scripted):
    """Scripted transport where events for ``fail`` answer HTTP 500."""

    def __init__(self, *args: Any, fail: set[str] = frozenset(), **kw: Any) -> None:  # type: ignore[assignment]
        super().__init__(*args, **kw)
        self.fail = set(fail)

    async def request(self, method, url, *, headers=None, params=None, body=None):
        if url.startswith(EVENTS) and url.endswith("/events"):
            cal = url[len(EVENTS):-len("/events")].replace("%40", "@")
            if cal in self.fail:
                self.calls.append((method, url, dict(params or {})))
                return HttpResponse(500, {}, b"boom")
        return await super().request(method, url, headers=headers, params=params, body=body)


async def _make_runner(tmp_path: pathlib.Path, transport: Scripted):
    engine = await _make_engine(tmp_path / "x.db")
    registry = _build_registry(engine)
    acct = await registry.calendar.create_account(
        provider="google", timezone="UTC", secret_ref="vault://cal",
        config={"client_id": "cid", "client_secret": "cs"},
    )
    await registry.connections.upsert_secret(
        kind="calendar_credential", owner_kind="connection", owner_id=acct.id,
        plaintext=json.dumps({"refresh_token": "rt"}),
    )
    await registry.calendar.update_account(acct.id, secret_ref="vault://cal")
    runner = CalendarSyncRunner(registry, calendar_service=CalendarService(), transport=transport)
    return engine, registry, acct, runner


async def _live_titles(registry) -> set[str]:
    rows = await registry.calendar.list_events(
        start=datetime(2026, 1, 1, tzinfo=UTC), end=datetime(2027, 12, 31, tzinfo=UTC)
    )
    return {r.title for r in rows if not r.deleted}


async def test_dropped_calendar_is_tombstoned_but_primary_kept(tmp_path) -> None:
    t = Scripted([[_primary(), _shared()]], {PRIMARY: [MINE], SHARED: [PAM]})
    engine, registry, acct, runner = await _make_runner(tmp_path, t)
    await runner.sync_account(acct.id)
    assert await _live_titles(registry) == {"Standup", "see Pam"}
    t.list_pages = [[_primary()]]  # shared calendar unselected / removed
    out = await runner.sync_account(acct.id)
    assert out["synced"] and out["deleted"] >= 1
    assert await _live_titles(registry) == {"Standup"}
    await engine.dispose()


async def test_failed_fetch_is_not_tombstoned(tmp_path) -> None:
    t = Failing([[_primary(), _shared()]], {PRIMARY: [MINE], SHARED: [PAM]})
    engine, registry, acct, runner = await _make_runner(tmp_path, t)
    await runner.sync_account(acct.id)
    t.fail = {SHARED}
    out = await runner.sync_account(acct.id)
    assert out["synced"]
    assert (await registry.calendar.get_account(acct.id)).sync_state["last_calendar_errors"] == {
        SHARED: "upstream_error"
    }
    assert "see Pam" in await _live_titles(registry)
    await engine.dispose()


async def test_orphan_guards_primary_and_empty_set(tmp_path) -> None:
    t = Scripted([[_primary(), _shared()]], {PRIMARY: [MINE], SHARED: [PAM]})
    engine, registry, acct, runner = await _make_runner(tmp_path, t)
    await runner.sync_account(acct.id)
    account = await registry.calendar.get_account(acct.id)
    prev_meta = {PRIMARY: {"primary": True}, SHARED: {"primary": False}}

    # Empty new set: skipped entirely.
    empty = SimpleNamespace(calendars=[], calendar_meta={}, errors={})
    assert await runner._tombstone_dropped_calendars(
        account, [PRIMARY, SHARED], prev_meta, empty) == 0
    assert await _live_titles(registry) == {"Standup", "see Pam"}

    # Previous primary dropped (new primary id): never tombstoned, shared is.
    other = SimpleNamespace(
        calendars=["new@example.com"],
        calendar_meta={"new@example.com": {"primary": True}}, errors={},
    )
    assert await runner._tombstone_dropped_calendars(
        account, [PRIMARY, SHARED], prev_meta, other) == 1
    assert await _live_titles(registry) == {"Standup"}
    await engine.dispose()


async def test_secondary_failure_recorded_in_errors_and_primary_failure_raises() -> None:
    t = Failing([[_primary(), _shared()]], {PRIMARY: [MINE], SHARED: [PAM]}, fail={SHARED})
    provider = GoogleCalendarProvider(t, secret="rt", config={"client_id": "c"})
    result = await provider.sync(window_start=WIN_START, window_end=WIN_END)
    assert result.errors == {SHARED: "upstream_error"}
    assert [e.title for e in result.events] == ["Standup"]

    t2 = Failing([[_primary(), _shared()]], {}, fail={PRIMARY})
    provider2 = GoogleCalendarProvider(t2, secret="rt", config={"client_id": "c"})
    try:
        await provider2.sync(window_start=WIN_START, window_end=WIN_END)
    except Exception as exc:  # noqa: BLE001
        assert getattr(exc, "code", None) == "upstream_error"
    else:
        raise AssertionError("primary failure must raise")


class Endless(Scripted):
    """Every events page for SHARED carries a nextPageToken."""

    async def request(self, method, url, *, headers=None, params=None, body=None):
        resp = await super().request(method, url, headers=headers, params=params, body=body)
        if url.startswith(EVENTS) and url.endswith("/events") and "pam%40" in url.replace("@", "%40"):
            data = json.loads(resp.body)
            data["nextPageToken"] = "more"
            return _json(data)
        return resp


async def test_truncation_flag_set_when_page_cap_hit() -> None:
    t = Endless([[_primary(), _shared()]], {PRIMARY: [MINE], SHARED: [PAM]})
    provider = GoogleCalendarProvider(t, secret="rt", config={"client_id": "c"})
    result = await provider.sync(window_start=WIN_START, window_end=WIN_END)
    assert result.truncated == [SHARED]
    assert t.event_calendars().count(SHARED) == MAX_EVENT_PAGES


async def test_ask_calendar_adapter_sees_shared_calendar_event(tmp_path) -> None:
    t = Scripted([[_primary(), _shared()]], {PRIMARY: [MINE], SHARED: [PAM]})
    engine, registry, acct, runner = await _make_runner(tmp_path, t)
    await runner.sync_account(acct.id)
    now = datetime(2026, 11, 18, 12, tzinfo=UTC)
    state = SimpleNamespace(repository_registry=registry)
    records = await ask_calendar(state, parse_intent("what is on my calendar", now), 50)
    pam = [r for r in records or [] if r.title == "see Pam"]
    assert len(pam) == 1
    assert pam[0].id == record_id("calendar", f"{acct.id}:pam-1")
    await engine.dispose()
