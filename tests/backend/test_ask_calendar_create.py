"""Ask calendar-create: gate, extraction, confirm-first proposal, Google write (mocked).

No network and no real calendar events: Google is a recording fake behind the
injectable ``HttpTransport``; the model is a scripted fake secure transport.
"""
from __future__ import annotations

import json
import pathlib
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool
from test_calendar_integration import CALENDAR_TABLES

from backend.api.endpoints import calendar as calendar_endpoints
from backend.api.endpoints import context as context_endpoints
from backend.calendar.proposals import ProposalStore
from backend.calendar.providers import GoogleCalendarProvider, HttpResponse
from backend.calendar.service import CalendarService
from backend.context import ask_actions
from backend.context.ask import AskService
from backend.persistence.connection import make_session_factory
from backend.persistence.models.personal import ConnectionRow
from backend.persistence.repositories import RepositoryRegistry
from backend.persistence.repositories.connections_repo import IdentitySecretCodec

TOKEN_URL = "https://oauth2.googleapis.com/token"
SENTENCE = (
    "hey! can you add my appointment with Michal Inspektor to my calendar "
    "for tomorrow, Tuesday, at 4:30?"
)
#: Monday 2026-06-01 18:22 in Los Angeles (PDT, UTC-7).
NOW = datetime(2026, 6, 2, 1, 22, tzinfo=UTC)


def draft_reply(**overrides: Any) -> str:
    data: dict[str, Any] = {
        "is_event": True,
        "title": "Appointment with Michal Inspektor",
        "start": "2026-06-02T16:30",
        "end": None,
        "duration_minutes": 60,
        "all_day": False,
        "location": None,
        "calendar_hint": None,
        "assumptions": ["No am/pm given: read 4:30 as 16:30 (PM)."],
    }
    data.update(overrides)
    return json.dumps(data)


class ScriptedModel:
    """Fake secure transport: records every call, answers from a script."""

    fixture_mode = True
    base_url = "http://omniroute.invalid"

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.extract_reply = draft_reply()
        self.chat_reply = "A plain answer."

    def check(self, model: str) -> None:  # marks this as a secure transport
        return None

    def generate(self, model, prompt, parameters=None, timeout=None, *, kind=None,
                 sensitive=True, max_tokens=None, **kw):
        self.calls.append({"kind": kind, "max_tokens": max_tokens, "prompt": prompt})
        content = self.extract_reply if kind == "extract" else self.chat_reply
        return {"content": content, "input_tokens": 1, "output_tokens": 1}

    def generate_stream(self, model, prompt, headers=None, max_tokens=None, timeout=None):
        self.calls.append({"kind": "stream", "max_tokens": max_tokens, "prompt": prompt})
        yield from ("A ", "streamed ", "answer.")


class GoogleFake:
    """Recording Google Calendar + token endpoint."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.mode = "ok"
        self.events: dict[str, dict[str, Any]] = {}

    def writes(self, method: str = "POST") -> list[dict[str, Any]]:
        return [c for c in self.calls if c["method"] == method and "/events" in c["url"]]

    async def request(self, method, url, *, headers=None, params=None, body=None):
        payload = json.loads(body) if body else None
        self.calls.append({"method": method, "url": url, "headers": dict(headers or {}),
                           "body": payload})
        if url == TOKEN_URL:
            return HttpResponse(200, {}, json.dumps({"access_token": "fake-access-token"}).encode())
        if "/calendar/v3/calendars/" not in url:
            return HttpResponse(404, {}, b"")
        if method == "POST":
            if self.mode == "forbidden":
                return HttpResponse(403, {}, b'{"error":{"message":"insufficientPermissions"}}')
            eid = payload["id"]
            if eid in self.events:
                return HttpResponse(409, {}, b'{"error":{"message":"duplicate"}}')
            self.events[eid] = {**payload, "status": "confirmed"}
            return HttpResponse(200, {}, json.dumps(self.events[eid]).encode())
        eid = url.rsplit("/", 1)[-1]
        if method == "GET":
            if eid in self.events:
                return HttpResponse(200, {}, json.dumps(self.events[eid]).encode())
            return HttpResponse(404, {}, b"")
        if method == "DELETE":
            self.events.pop(eid, None)
            return HttpResponse(204, {}, b"")
        return HttpResponse(404, {}, b"")


class World:
    def __init__(self) -> None:
        self.sub = "test-person"
        self.clock = NOW
        self.model = ScriptedModel()
        self.google = GoogleFake()
        self.registry: Any = None
        self.app: FastAPI = None  # type: ignore[assignment]
        self.client: TestClient = None  # type: ignore[assignment]
        self.frameshift = ""
        self.personal = ""
        self.frameshift_conn = ""

    def ask(self, question: str = SENTENCE, **extra: Any):
        return self.client.post("/api/context/ask", json={"question": question, **extra})

    def propose(self, **extra: Any) -> dict[str, Any]:
        response = self.ask(**extra)
        assert response.status_code == 200, response.text
        return response.json()


@pytest.fixture
async def world(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv(ask_actions.ENV_CONFIRM_FIRST, raising=False)
    monkeypatch.delenv(ask_actions.ENV_DEFAULT_ACCOUNT, raising=False)
    monkeypatch.delenv(ask_actions.ENV_USER_TIMEZONE, raising=False)
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'ask.db'}",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(
            ConnectionRow.__table__.metadata.create_all,
            tables=[*CALENDAR_TABLES, ConnectionRow.__table__],
        )
    w = World()
    w.registry = RepositoryRegistry(make_session_factory(engine), secret_codec=IdentitySecretCodec())
    reg = w.registry

    async def link(address: str, label: str, granted: list[str]) -> tuple[str, str]:
        conn = await reg.connections.create_connection(
            kind="gmail", label=address,
            config={"account": address, "granted_extra": granted},
        )
        await reg.connections.upsert_secret(
            kind="refresh_token", owner_kind="connection", owner_id=conn.id, plaintext="rt-secret"
        )
        acct = await reg.calendar.create_account(
            provider="google", label=label, account_ref=address,
            timezone="America/Los_Angeles", config={"connection_id": conn.id},
        )
        return acct.id, conn.id

    w.frameshift, w.frameshift_conn = await link(
        "celes@frameshift.net", "Frameshift", ["calendar", "calendar_write"]
    )
    w.personal, _ = await link("celes@gmail.example", "Personal", ["calendar", "calendar_write"])

    app = FastAPI()

    @app.middleware("http")
    async def principal(request, call_next):  # stands in for the default-deny gate
        request.state.principal = {"sub": w.sub}
        return await call_next(request)

    app.include_router(calendar_endpoints.router)
    app.include_router(context_endpoints.router)
    app.state.repository_registry = reg
    app.state.calendar_service = CalendarService()
    app.state.calendar_transport = w.google
    app.state.ask_calendar_now = lambda: NOW
    app.state.ask_proposals = ProposalStore(clock=lambda: w.clock)
    app.state.ask_service = AskService(transport=w.model, remote_provider_allowed=True)
    w.app = app
    w.client = TestClient(app)
    yield w
    await engine.dispose()


# -- gate ---------------------------------------------------------------------
@pytest.mark.parametrize("question", [
    SENTENCE,
    "add lunch with Sam to my calendar friday at noon",
    "Schedule a meeting with Dana tomorrow at 10am",
    "put the dentist appointment on my calendar",
    "book an appointment for Thursday",
    "set up a reminder to call mom tomorrow",
])
def test_gate_matches_calendar_requests(question: str) -> None:
    assert ask_actions.looks_like_calendar_create(question)


@pytest.mark.parametrize("question", [
    "what is on my calendar tomorrow?",
    "hello world",
    "summarize my unread email",
    "who is Michal Inspektor?",
    "",
])
def test_gate_ignores_ordinary_questions(question: str) -> None:
    assert not ask_actions.looks_like_calendar_create(question)


def test_timezone_priority(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ask_actions.ENV_USER_TIMEZONE, raising=False)
    assert ask_actions.user_timezone("America/Los_Angeles") == "America/Los_Angeles"
    assert ask_actions.user_timezone(None) == "UTC"
    monkeypatch.setenv(ask_actions.ENV_USER_TIMEZONE, "Europe/Warsaw")
    assert ask_actions.user_timezone(None) == "Europe/Warsaw"
    assert ask_actions.user_timezone("UTC") == "Europe/Warsaw"  # unset account default
    assert ask_actions.user_timezone("America/Denver") == "America/Denver"


# -- proposal -------------------------------------------------------------------
async def test_exact_sentence_proposes_1630_tomorrow_on_the_default_account(world: World) -> None:
    body = world.propose()
    assert body["status"] == "proposal" and body["executed"] is False
    proposal = body["proposal"]
    assert "Michal Inspektor" in proposal["title"]
    assert proposal["start"] == "2026-06-02T16:30:00-07:00"
    assert proposal["end"] == "2026-06-02T17:30:00-07:00"
    assert proposal["tz"] == "America/Los_Angeles"
    assert proposal["account"] == {"id": world.frameshift, "label": "Frameshift", "can_write": True}
    assert {a["id"] for a in proposal["accounts"]} == {world.frameshift, world.personal}
    assert any("PM" in a for a in proposal["assumptions"])
    assert proposal["requires_confirmation"] is True and proposal["expires_at"]
    assert "Tue Jun 2 at 4:30 PM" in body["answer"] and body["answer"].endswith("Confirm?")
    # one extraction call on the reader route, with date, weekday and zone in the prompt
    extract = [c for c in world.model.calls if c["kind"] == "extract"]
    assert len(extract) == 1 and extract[0]["max_tokens"] == 300
    assert "2026-06-01" in extract[0]["prompt"] and "Monday" in extract[0]["prompt"]
    assert "America/Los_Angeles" in extract[0]["prompt"]
    assert world.google.writes() == []  # nothing written before Confirm


async def test_pm_assumption_is_flagged_in_code_even_if_the_model_forgets(world: World) -> None:
    world.model.extract_reply = draft_reply(assumptions=[])
    proposal = world.propose()["proposal"]
    assert any("PM" in a for a in proposal["assumptions"])


async def test_weekday_mismatch_warns_and_uses_the_named_weekday(world: World) -> None:
    world.model.extract_reply = draft_reply(start="2026-06-02T16:30")  # a Tuesday
    proposal = world.propose(question="add dentist to my calendar this Wednesday at 4:30")["proposal"]
    assert proposal["warnings"] and "Wednesday" in proposal["warnings"][0]
    assert proposal["start"].startswith("2026-06-03T16:30")


async def test_past_start_is_rejected_without_a_proposal(world: World) -> None:
    world.model.extract_reply = draft_reply(start="2026-06-01T09:00")
    body = world.propose(question="add my dentist appointment to my calendar today at 9")
    assert body["status"] == "answered" and "past" in body["answer"]
    assert "proposal" not in body


async def test_unusable_extraction_falls_through_to_the_normal_answer(world: World) -> None:
    world.model.extract_reply = "I cannot do that"
    body = world.propose()
    assert body["status"] == "answered" and body["answer"].startswith("A plain answer.")


async def test_non_calendar_question_costs_no_extra_inference(world: World) -> None:
    body = world.propose(question="what is on my calendar tomorrow?")
    assert body["answer"].startswith("A plain answer.")
    assert [c["kind"] for c in world.model.calls] == [None]


async def test_conflicts_are_carried_on_the_proposal(world: World) -> None:
    world.app.state.calendar_service.ingest({
        "event_id": "x1", "title": "Standup", "start": "2026-06-02T23:30:00+00:00",
        "end": "2026-06-03T00:00:00+00:00", "source": "test",
    })
    proposal = world.propose()["proposal"]
    assert [c["title"] for c in proposal["conflicts"]] == ["Standup"]


# -- account resolution ---------------------------------------------------------
async def test_account_named_in_the_request_beats_the_default(world: World) -> None:
    world.model.extract_reply = draft_reply(calendar_hint="personal")
    assert world.propose()["proposal"]["account"]["id"] == world.personal


async def test_ui_account_beats_request_and_default(world: World) -> None:
    world.model.extract_reply = draft_reply(calendar_hint="frameshift")
    proposal = world.propose(account_id=world.personal)["proposal"]
    assert proposal["account"]["id"] == world.personal


async def test_default_account_setting_picks_another_default(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ask_actions.ENV_DEFAULT_ACCOUNT, "celes@gmail.example")
    assert world.propose()["proposal"]["account"]["id"] == world.personal


async def test_accounts_expose_can_write_and_connection_id(world: World) -> None:
    rows = world.client.get("/api/calendar/accounts").json()["data"]
    by_id = {r["id"]: r for r in rows}
    assert by_id[world.frameshift]["can_write"] is True
    assert by_id[world.frameshift]["connection_id"] == world.frameshift_conn
    await world.registry.connections.update_connection(
        world.frameshift_conn, config={"account": "celes@frameshift.net", "granted_extra": ["calendar"]}
    )
    row = world.client.get(f"/api/calendar/accounts/{world.frameshift}").json()
    assert row["can_write"] is False and row["connection_id"] == world.frameshift_conn


# -- confirm --------------------------------------------------------------------
async def test_confirm_creates_exactly_one_event_in_both_planes(world: World) -> None:
    proposal = world.propose()["proposal"]
    response = world.client.post(f"/api/calendar/proposals/{proposal['id']}/confirm")
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["status"] == "created" and result["undo"]["event_id"] == result["event"]["event_id"]

    posts = world.google.writes()
    assert len(posts) == 1
    assert posts[0]["url"] == "https://www.googleapis.com/calendar/v3/calendars/primary/events"
    assert posts[0]["headers"]["Authorization"] == "Bearer fake-access-token"
    sent = posts[0]["body"]
    assert sent["summary"] == "Appointment with Michal Inspektor"
    assert sent["start"] == {"dateTime": "2026-06-02T16:30:00-07:00", "timeZone": "America/Los_Angeles"}
    assert sent["end"]["dateTime"] == "2026-06-02T17:30:00-07:00"
    assert 5 <= len(sent["id"]) <= 1024 and set(sent["id"]) <= set("0123456789abcdefghijklmnopqrstuv")

    event_id = result["event"]["event_id"]
    assert world.app.state.calendar_service.get_event(event_id) is not None  # in-memory plane
    durable = await world.registry.calendar.list_events(account_id=world.frameshift)
    assert [r.title for r in durable] == ["Appointment with Michal Inspektor"]  # durable plane
    assert durable[0].start.astimezone(UTC) == datetime(2026, 6, 2, 23, 30, tzinfo=UTC)


async def test_double_confirm_is_idempotent(world: World) -> None:
    pid = world.propose()["proposal"]["id"]
    first = world.client.post(f"/api/calendar/proposals/{pid}/confirm").json()
    second = world.client.post(f"/api/calendar/proposals/{pid}/confirm").json()
    assert first["event"] == second["event"] and len(world.google.writes()) == 1
    assert len(await world.registry.calendar.list_events(account_id=world.frameshift)) == 1


async def test_google_duplicate_id_is_treated_as_created(world: World) -> None:
    provider = GoogleCalendarProvider(world.google, secret="rt", account_timezone="UTC")
    kwargs = dict(summary="Once", start=NOW + timedelta(days=1), end=None, tz="UTC",
                  event_id="abcde12345")
    first = await provider.create_event("primary", **kwargs)
    again = await provider.create_event("primary", **kwargs)
    assert first.ical_uid == again.ical_uid == "abcde12345"
    assert len(world.google.writes()) == 2 and len(world.google.events) == 1


async def test_confirm_edits_from_the_card_apply(world: World) -> None:
    pid = world.propose()["proposal"]["id"]
    response = world.client.post(
        f"/api/calendar/proposals/{pid}/confirm",
        json={"account_id": world.personal, "title": "Dentist", "start": "2026-06-02T17:00:00-07:00"},
    )
    assert response.status_code == 200, response.text
    sent = world.google.writes()[0]["body"]
    assert sent["summary"] == "Dentist" and sent["start"]["dateTime"].startswith("2026-06-02T17:00")
    assert sent["end"]["dateTime"].startswith("2026-06-02T18:00")  # duration kept
    assert response.json()["event"]["account_id"] == world.personal


async def test_missing_write_scope_is_a_409_consent_required_and_ingests_nothing(world: World) -> None:
    world.google.mode = "forbidden"
    pid = world.propose()["proposal"]["id"]
    response = world.client.post(f"/api/calendar/proposals/{pid}/confirm")
    assert response.status_code == 409
    body = response.json()
    assert body["code"] == "consent_required" and body["connection_id"] == world.frameshift_conn
    assert body["message"]
    assert await world.registry.calendar.list_events(account_id=world.frameshift) == []
    assert world.app.state.calendar_service.list_events(start=None, end=None, limit=10) == []
    # the proposal survives so the person can grant access and confirm again
    world.google.mode = "ok"
    assert world.client.post(f"/api/calendar/proposals/{pid}/confirm").status_code == 200


# -- ownership, expiry, cancel -----------------------------------------------------
async def test_wrong_sub_cannot_confirm_or_cancel(world: World) -> None:
    pid = world.propose()["proposal"]["id"]
    world.sub = "someone-else"
    assert world.client.post(f"/api/calendar/proposals/{pid}/confirm").status_code == 404
    assert world.client.post(f"/api/calendar/proposals/{pid}/cancel").status_code == 404
    assert world.google.writes() == []
    world.sub = "test-person"
    assert world.client.post(f"/api/calendar/proposals/{pid}/confirm").status_code == 200


async def test_expired_proposal_is_404(world: World) -> None:
    pid = world.propose()["proposal"]["id"]
    world.clock = NOW + timedelta(minutes=16)
    assert world.client.post(f"/api/calendar/proposals/{pid}/confirm").status_code == 404
    assert world.google.writes() == []


async def test_cancel_discards_the_proposal(world: World) -> None:
    pid = world.propose()["proposal"]["id"]
    assert world.client.post(f"/api/calendar/proposals/{pid}/cancel").json()["status"] == "cancelled"
    assert world.client.post(f"/api/calendar/proposals/{pid}/confirm").status_code == 404


# -- the one setting -----------------------------------------------------------------
@pytest.mark.parametrize("value,expected", [
    (None, True), ("true", True), ("TRUE", True), ("1", True), ("false", False), ("0", False),
])
def test_confirm_first_reads_one_setting(value, expected) -> None:
    env = {} if value is None else {ask_actions.ENV_CONFIRM_FIRST: value}
    assert ask_actions.confirm_first(env) is expected


async def test_confirm_first_off_creates_immediately_and_undo_deletes(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ask_actions.ENV_CONFIRM_FIRST, "false")
    body = world.propose()
    assert body["status"] == "created" and body["executed"] is True
    assert "proposal" not in body and len(world.google.writes()) == 1
    event_id = body["undo"]["event_id"]
    assert body["event"]["event_id"] == event_id
    assert world.app.state.calendar_service.get_event(event_id) is not None

    world.sub = "someone-else"
    assert world.client.post(f"/api/calendar/events/{event_id}/undo").status_code == 404
    world.sub = "test-person"
    undone = world.client.post(f"/api/calendar/events/{event_id}/undo")
    assert undone.status_code == 200 and undone.json()["undone"] is True
    assert len(world.google.writes("DELETE")) == 1 and world.google.events == {}
    assert world.app.state.calendar_service.get_event(event_id) is None
    assert world.client.post(f"/api/calendar/events/{event_id}/undo").status_code == 404


async def test_confirm_first_off_without_write_access_falls_back_to_a_proposal(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ask_actions.ENV_CONFIRM_FIRST, "false")
    world.google.mode = "forbidden"
    body = world.propose()
    assert body["status"] == "proposal"
    assert body["proposal"]["consent_required"]["code"] == "consent_required"
    assert await world.registry.calendar.list_events(account_id=world.frameshift) == []


async def test_undo_cannot_delete_an_event_ask_did_not_create(world: World) -> None:
    assert world.client.post("/api/calendar/events/not-ours/undo").status_code == 404
    assert world.google.writes("DELETE") == []
