"""Ask calendar deletion end to end with fakes: propose, confirm, series, caps.

Google is a recording fake behind the injectable ``HttpTransport`` (series
masters, instances, PATCH, DELETE, failure modes). No network, no real events.
"""
from __future__ import annotations

import json
import pathlib
import re
import urllib.parse
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool
from test_calendar_integration import CALENDAR_TABLES

from backend.api.endpoints import calendar as calendar_endpoints
from backend.api.endpoints import calendar_deletions
from backend.api.endpoints import context as context_endpoints
from backend.calendar.delete_proposals import DeletionStore
from backend.calendar.providers import HttpResponse
from backend.calendar.series import HORIZON_DAYS, MAX_INSTANCES
from backend.calendar.service import CalendarService
from backend.contacts.service import ContactsService
from backend.context.ask import AskService
from backend.persistence.connection import make_session_factory
from backend.persistence.models.personal import ConnectionRow
from backend.persistence.repositories import RepositoryRegistry
from backend.persistence.repositories.connections_repo import IdentitySecretCodec

TOKEN_URL = "https://oauth2.googleapis.com/token"
SENTENCE = ("I no longer see Laura for therapy, can you remove all my future "
            "appointments with her?")
#: Monday 2026-06-01 18:22 in Los Angeles (PDT, UTC-7).
NOW = datetime(2026, 6, 2, 1, 22, tzinfo=UTC)
LA = "-07:00"
CAL_A = "frameshift@cal.test"
CAL_B = "personal@cal.test"


class ScriptedModel:
    fixture_mode = True
    base_url = "http://omniroute.invalid"

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.extract_reply = '{"is_delete": true, "person": "Laura", "topic": "therapy"}'

    def check(self, model: str) -> None:
        return None

    def generate(self, model, prompt, parameters=None, timeout=None, *, kind=None, **kw):
        self.calls.append({"kind": kind, "prompt": prompt})
        return {"content": self.extract_reply if kind == "extract" else "plain", "input_tokens": 1,
                "output_tokens": 1}

    def generate_stream(self, model, prompt, **kw):
        self.calls.append({"kind": "stream", "prompt": prompt})
        yield "plain"


def item(eid: str, title: str, start: str, **extra: Any) -> dict[str, Any]:
    out = {"id": eid, "summary": title, "status": "confirmed",
           "start": {"dateTime": start, "timeZone": "America/Los_Angeles"},
           "end": {"dateTime": (datetime.fromisoformat(start) + timedelta(hours=1)).isoformat(),
                   "timeZone": "America/Los_Angeles"}}
    out.update(extra)
    return out


class GoogleFake:
    """Two calendars of events; series have a weekly step and an optional COUNT."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.cals: dict[str, dict[str, dict[str, Any]]] = {CAL_A: {}, CAL_B: {}}
        self.deleted: set[str] = set()
        self.series_delete = "ok"  # ok | error | forbidden
        self.forbidden = False

    def add(self, cal: str, event: dict[str, Any]) -> None:
        self.cals[cal][event["id"]] = event

    def writes(self, method: str) -> list[dict[str, Any]]:
        return [c for c in self.calls if c["method"] == method and "/events" in c["url"]]

    def instances(self, master: dict[str, Any], t_min: datetime, t_max: datetime, limit: int):
        rule = ";".join(master.get("recurrence", []))
        count = int(m.group(1)) if (m := re.search(r"COUNT=(\d+)", rule)) else None
        until = None
        if (m := re.search(r"UNTIL=(\d{8})(?:T(\d{6})Z)?", rule)):
            until = datetime.strptime(m.group(1) + (m.group(2) or "235959"), "%Y%m%d%H%M%S").replace(tzinfo=UTC)
        start = datetime.fromisoformat(master["start"]["dateTime"])
        out, k = [], 0
        while len(out) < limit and k < 2000:
            when = start + timedelta(weeks=k)
            k += 1
            if count is not None and k > count:
                break
            if until is not None and when.astimezone(UTC) > until:
                break
            if when > t_max:
                break
            iid = f"{master['id']}_{when.astimezone(UTC):%Y%m%dT%H%M%SZ}"
            if when >= t_min and iid not in self.deleted:
                out.append(item(iid, master["summary"], when.isoformat(), recurringEventId=master["id"]))
        return out

    async def request(self, method, url, *, headers=None, params=None, body=None):
        payload = json.loads(body) if body else None
        self.calls.append({"method": method, "url": url, "body": payload, "params": dict(params or {})})
        if url == TOKEN_URL:
            return HttpResponse(200, {}, json.dumps({"access_token": "fake-access-token"}).encode())
        m = re.match(r"https://www.googleapis.com/calendar/v3/calendars/([^/]+)/events(?:/([^/]+))?(/instances)?$", url)
        if not m:
            return HttpResponse(404, {}, b"")
        cal, eid, inst = urllib.parse.unquote(m.group(1)), m.group(2), m.group(3)
        events = self.cals[cal]
        eid = urllib.parse.unquote(eid) if eid else None
        if method != "GET" and self.forbidden:
            return HttpResponse(403, {}, b"{}")
        if method == "GET" and eid is None:
            q = (params or {}).get("q", "").lower()
            t_min = datetime.fromisoformat(params["timeMin"].replace("Z", "+00:00"))
            assert params.get("singleEvents") == "false"
            items = []
            for e in events.values():
                blob = json.dumps(e).lower()
                if q not in blob:
                    continue
                if not e.get("recurrence") and datetime.fromisoformat(e["start"]["dateTime"]) < t_min:
                    continue
                items.append(e)
            return HttpResponse(200, {}, json.dumps({"items": items}).encode())
        if method == "GET" and inst:
            t_min = datetime.fromisoformat(params["timeMin"].replace("Z", "+00:00"))
            t_max = datetime.fromisoformat(params["timeMax"].replace("Z", "+00:00"))
            assert t_max - t_min <= timedelta(days=3650), "unbounded instance window"
            items = self.instances(events[eid], t_min, t_max, int(params["maxResults"]))
            return HttpResponse(200, {}, json.dumps({"items": items}).encode())
        if method == "GET":
            return HttpResponse(200, {}, json.dumps(events[eid]).encode()) if eid in events else HttpResponse(404, {}, b"")
        if method == "PATCH":
            events[eid].update(payload)
            return HttpResponse(200, {}, json.dumps(events[eid]).encode())
        if method == "DELETE":
            if eid in events:
                if events[eid].get("recurrence") and self.series_delete == "error":
                    return HttpResponse(500, {}, b"{}")
                del events[eid]
            else:
                self.deleted.add(eid)
            return HttpResponse(204, {}, b"")
        return HttpResponse(404, {}, b"")


class World:
    def __init__(self) -> None:
        self.sub = "test-person"
        self.clock = NOW
        self.model = ScriptedModel()
        self.google = GoogleFake()
        self.app: FastAPI = None  # type: ignore[assignment]
        self.client: TestClient = None  # type: ignore[assignment]
        self.registry: Any = None
        self.frameshift = ""
        self.personal = ""
        self.frameshift_conn = ""

    def ask(self, question: str = SENTENCE) -> dict[str, Any]:
        response = self.client.post("/api/context/ask", json={"question": question})
        assert response.status_code == 200, response.text
        return response.json()

    def propose(self, question: str = SENTENCE) -> dict[str, Any]:
        body = self.ask(question)
        assert body["status"] == "delete_proposal", body
        return body["delete_proposal"]

    def confirm(self, pid: str, **payload: Any):
        return self.client.post(f"/api/calendar/deletions/{pid}/confirm", json=payload)


def seed(google: GoogleFake) -> None:
    google.add(CAL_A, item("series1", "Therapy with Laura", f"2026-04-07T10:00:00{LA}",
                           recurrence=["RRULE:FREQ=WEEKLY;BYDAY=TU"], organizer={"self": True}))
    google.add(CAL_A, item("single1", "Laura follow-up", f"2026-06-20T11:00:00{LA}"))
    google.add(CAL_A, item("future1", "Laura check-in", f"2026-06-10T09:00:00{LA}",
                           recurrence=["RRULE:FREQ=WEEKLY;COUNT=4"], organizer={"self": True}))
    google.add(CAL_A, item("maybe1", "Hour", f"2026-06-15T09:00:00{LA}", description="ask Laura"))
    google.add(CAL_A, item("past1", "Lunch with Laura", f"2026-05-01T12:00:00{LA}"))
    google.add(CAL_A, item("other1", "Dentist", f"2026-06-12T12:00:00{LA}"))
    google.add(CAL_B, item("series2", "Laura group", f"2026-03-03T18:00:00{LA}",
                           recurrence=["RRULE:FREQ=WEEKLY;BYDAY=TU"], organizer={"self": False},
                           attendees=[{"email": "x@y.test", "displayName": "Laura Smith"}]))


@pytest.fixture
async def world(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'del.db'}", poolclass=StaticPool,
        connect_args={"check_same_thread": False})
    async with engine.begin() as conn:
        await conn.run_sync(ConnectionRow.__table__.metadata.create_all,
                            tables=[*CALENDAR_TABLES, ConnectionRow.__table__])
    w = World()
    w.registry = reg = RepositoryRegistry(make_session_factory(engine), secret_codec=IdentitySecretCodec())

    async def link(address: str, label: str, cal: str, granted: list[str]) -> tuple[str, str]:
        conn = await reg.connections.create_connection(
            kind="gmail", label=address, config={"account": address, "granted_extra": granted})
        await reg.connections.upsert_secret(kind="refresh_token", owner_kind="connection",
                                            owner_id=conn.id, plaintext="rt-secret")
        acct = await reg.calendar.create_account(
            provider="google", label=label, account_ref=address, timezone="America/Los_Angeles",
            config={"connection_id": conn.id, "calendar_ids": [cal]})
        return acct.id, conn.id

    w.frameshift, w.frameshift_conn = await link("celes@frameshift.net", "Frameshift", CAL_A,
                                                 ["calendar", "calendar_write"])
    w.personal, _ = await link("celes@gmail.example", "Personal", CAL_B, ["calendar", "calendar_write"])
    app = FastAPI()

    @app.middleware("http")
    async def principal(request, call_next):
        request.state.principal = {"sub": w.sub}
        return await call_next(request)

    app.include_router(calendar_endpoints.router)
    app.include_router(calendar_deletions.router)
    app.include_router(context_endpoints.router)
    app.state.repository_registry = reg
    app.state.calendar_service = CalendarService()
    app.state.calendar_transport = w.google
    app.state.ask_calendar_now = lambda: NOW
    app.state.ask_delete_proposals = DeletionStore(clock=lambda: w.clock)
    contacts = ContactsService()
    contacts.ingest({"person_id": "p1", "display_name": "Laura Smith", "emails": ["laura@x.test"]})
    app.state.contacts_service = contacts
    app.state.ask_service = AskService(transport=w.model, remote_provider_allowed=True)
    w.app, w.client = app, TestClient(app)
    seed(w.google)
    yield w
    await engine.dispose()


def rows_by_title(view: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {r["title"]: r for r in view["rows"]}


async def test_proposal_lists_every_future_match_and_writes_nothing(world: World) -> None:
    body = world.ask()
    view = body["delete_proposal"]
    rows = rows_by_title(view)
    assert set(rows) == {"Therapy with Laura", "Laura follow-up", "Laura check-in", "Hour", "Laura group"}
    assert view["scope"] == "all_future" and view["scope_choices"] == []
    assert rows["Therapy with Laura"]["kind"] == "series" and rows["Therapy with Laura"]["repeats"] == "repeats weekly"
    assert rows["Therapy with Laura"]["path"] == "series_end" and rows["Therapy with Laura"]["upcoming_count"] > 3
    assert len(rows["Therapy with Laura"]["next_dates"]) == 3
    assert rows["Laura check-in"]["path"] == "series_deleted" and rows["Laura check-in"]["upcoming_count"] == 4
    assert rows["Laura follow-up"]["kind"] == "single" and rows["Laura follow-up"]["path"] == "delete"
    assert rows["Hour"]["confidence"] == "possible" and rows["Hour"]["selected"] is False
    assert rows["Laura group"]["account"]["label"] == "Personal" and rows["Laura group"]["path"] == "series_removed"
    assert rows["Laura group"]["fallback"]["max_instances"] == MAX_INSTANCES
    assert "Lunch with Laura" not in rows and "Dentist" not in rows
    assert all(r["selected"] for t, r in rows.items() if t != "Hour")
    assert "Nothing is deleted until you press Confirm" in body["answer"]
    for method in ("DELETE", "PATCH", "POST"):
        assert [c for c in world.google.writes(method) if "/events" in c["url"]] == []
    assert [c["kind"] for c in world.model.calls] == ["extract"]


async def test_confirm_ends_past_series_deletes_future_and_leaves_possible_rows(world: World) -> None:
    view = world.propose()
    response = world.confirm(view["id"])
    assert response.status_code == 200, response.text
    results = {r["title"]: r for r in response.json()["results"]}
    assert results["Therapy with Laura"]["status"] == "series_ended"
    assert results["Laura follow-up"]["status"] == "deleted"
    assert results["Laura check-in"]["status"] == "deleted"
    assert results["Laura group"]["status"] == "deleted" and results["Laura group"]["path"] == "series_removed"
    assert "Hour" not in results
    cal_a, cal_b = world.google.cals[CAL_A], world.google.cals[CAL_B]
    assert "series1" in cal_a  # the master of a past-started series is never deleted
    assert cal_a["series1"]["recurrence"][0].startswith("RRULE:FREQ=WEEKLY;BYDAY=TU;UNTIL=20260602T0121")
    assert "single1" not in cal_a and "future1" not in cal_a and "maybe1" in cal_a and "past1" in cal_a
    assert "series2" not in cal_b
    patches = world.google.writes("PATCH")
    assert len(patches) == 1 and set(patches[0]["body"]) == {"recurrence"}
    deletes = {c["url"].rsplit("/", 1)[-1] for c in world.google.writes("DELETE")}
    assert deletes == {"single1", "future1", "series2"}
    # past occurrences are untouched: instances before the cutoff still exist
    past = world.google.instances(cal_a["series1"], datetime(2026, 4, 1, tzinfo=UTC), NOW, 100)
    assert len(past) >= 8


async def test_repeat_confirm_makes_no_second_write(world: World) -> None:
    pid = world.propose()["id"]
    first = world.confirm(pid).json()
    count = len(world.google.writes("DELETE")) + len(world.google.writes("PATCH"))
    second = world.confirm(pid).json()
    assert first == second
    assert len(world.google.writes("DELETE")) + len(world.google.writes("PATCH")) == count


async def test_only_listed_rows_are_accepted_and_explicit_selection_wins(world: World) -> None:
    pid = world.propose()["id"]
    bad = world.confirm(pid, item_ids=["not-a-row"])
    assert bad.status_code == 422 and bad.json()["code"] == "unknown_item"
    assert world.confirm(pid, item_ids=[]).status_code == 422
    assert world.google.writes("DELETE") == [] and world.google.writes("PATCH") == []
    maybe = rows_by_title(world.propose())["Hour"]["id"]
    pid2 = world.client.post("/api/context/ask", json={"question": SENTENCE}).json()["delete_proposal"]
    hour = rows_by_title(pid2)["Hour"]["id"]
    ok = world.confirm(pid2["id"], item_ids=[hour])
    assert ok.status_code == 200 and [r["title"] for r in ok.json()["results"]] == ["Hour"]
    assert "maybe1" not in world.google.cals[CAL_A] and maybe


async def test_wrong_sub_expired_and_cancel(world: World) -> None:
    pid = world.propose()["id"]
    world.sub = "someone-else"
    assert world.confirm(pid).status_code == 404
    assert world.client.post(f"/api/calendar/deletions/{pid}/cancel").status_code == 404
    world.sub = "test-person"
    world.clock = NOW + timedelta(minutes=16)
    assert world.confirm(pid).status_code == 404
    world.clock = NOW
    pid = world.propose()["id"]
    assert world.client.post(f"/api/calendar/deletions/{pid}/cancel").json()["status"] == "cancelled"
    assert world.confirm(pid).status_code == 404
    assert world.google.writes("DELETE") == [] and world.google.writes("PATCH") == []


async def test_missing_write_scope_is_409_consent_required(world: World) -> None:
    pid = world.propose()["id"]
    world.google.forbidden = True
    response = world.confirm(pid)
    assert response.status_code == 409
    assert response.json()["code"] == "consent_required" and response.json()["connection_id"]
    world.google.forbidden = False
    assert world.confirm(pid).status_code == 200  # the proposal survives


async def test_attendee_series_falls_back_to_bounded_instance_deletes(world: World) -> None:
    world.google.series_delete = "error"
    view = world.propose()
    row = rows_by_title(view)["Laura group"]
    result = world.confirm(view["id"], item_ids=[row["id"]]).json()["results"][0]
    assert result["status"] == "instances_deleted" and result["path"] == "instances_deleted"
    assert result["deleted"] == MAX_INSTANCES
    assert result["still_open"] is True and "ask the organiser" in result["detail"]
    deletes = world.google.writes("DELETE")
    assert len(deletes) == 1 + MAX_INSTANCES  # the refused series delete, then at most the cap
    # every enumeration call was bounded
    for call in world.google.calls:
        if call["url"].endswith("/instances"):
            assert int(call["params"]["maxResults"]) <= MAX_INSTANCES + 1 or call["params"]["maxResults"] == "1"
    assert "series2" in world.google.cals[CAL_B]  # master untouched: it is still open


async def test_infinite_rule_is_never_walked_to_the_end(world: World) -> None:
    world.google.series_delete = "error"
    view = world.propose()
    row = rows_by_title(view)["Laura group"]
    assert row["upcoming_capped"] is True and row["fallback"]["count"] == MAX_INSTANCES
    before = len(world.google.calls)
    world.confirm(view["id"], item_ids=[row["id"]])
    assert len(world.google.calls) - before < MAX_INSTANCES + 10
    assert HORIZON_DAYS <= 400


async def test_finite_attendee_series_fallback_reports_not_open(world: World) -> None:
    world.google.series_delete = "error"
    world.google.add(CAL_B, item("fin1", "Laura workshop", f"2026-06-09T18:00:00{LA}",
                                 recurrence=["RRULE:FREQ=WEEKLY;COUNT=3"], organizer={"self": False}))
    view = world.propose()
    row = rows_by_title(view)["Laura workshop"]
    result = world.confirm(view["id"], item_ids=[row["id"]]).json()["results"][0]
    assert result["status"] == "instances_deleted" and result["deleted"] == 3 and result["still_open"] is False


async def test_ambiguous_wording_asks_and_confirm_needs_a_choice(world: World) -> None:
    view = world.propose("cancel my appointments with Laura")
    assert view["scope"] == "ambiguous" and [c["id"] for c in view["scope_choices"]] == ["next", "all_future"]
    response = world.confirm(view["id"])
    assert response.status_code == 422 and response.json()["code"] == "scope_required"
    assert world.google.writes("DELETE") == []
    ok = world.confirm(view["id"], scope="next", item_ids=[rows_by_title(view)["Laura follow-up"]["id"]])
    assert ok.status_code == 200


async def test_next_scope_deletes_only_the_next_occurrence_of_a_series(world: World) -> None:
    view = world.propose("cancel my next session with Laura")
    assert view["scope"] == "next"
    selected = [r for r in view["rows"] if r["selected"]]
    assert len(selected) == 1
    result = world.confirm(view["id"]).json()["results"][0]
    assert result["status"] == "deleted"
    assert world.google.writes("PATCH") == []


async def test_unknown_person_gets_a_plain_answer_and_no_proposal(world: World) -> None:
    world.model.extract_reply = '{"is_delete": true, "person": "Zelda"}'
    body = world.ask("remove all my appointments with Zelda")
    assert body["status"] == "answered" and "found no upcoming appointments with Zelda" in body["answer"]
    assert "delete_proposal" not in body


async def test_no_name_asks_who(world: World) -> None:
    world.model.extract_reply = '{"is_delete": true, "person": null}'
    body = world.ask("remove all my appointments with her")
    assert body["status"] == "answered" and "whose appointments" in body["answer"]


async def test_stream_emits_a_delete_proposal_event(world: World) -> None:
    with world.client.stream("POST", "/api/context/ask/stream", json={"question": SENTENCE}) as r:
        text = "".join(r.iter_text())
    assert "event: delete_proposal" in text and "event: done" in text
    assert world.google.writes("DELETE") == []
