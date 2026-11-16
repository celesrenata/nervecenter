"""Agent runner coverage (NS-ADR-099): lifecycle, cadence, back-off, Skill Hub."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from backend.agents.runner import AgentRunner, RunnerStore
from backend.agents.runner.backoff import (
    BACKED_OFF,
    DEGRADED,
    NORMAL,
    BackoffConfig,
    BackoffController,
)
from backend.agents.runner.runner import compute_next_run, validate_schedule
from backend.agents.runner.skillhub import SkillHubClient
from backend.api.app import canonical_aliases
from backend.api.endpoints import agent_runner as runner_ep
from backend.api.endpoints import agents as agents_ep
from backend.persistence.connection import make_session_factory
from backend.persistence.models import Agent as AgentORM
from backend.persistence.models import AgentRun, AgentSchedule
from backend.services.incident_agent import AgentService

T0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


class FakeClock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw: float) -> None:
        self.now += timedelta(**kw)


@pytest.fixture
async def engine():
    eng = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    async with eng.begin() as conn:
        for model in (AgentORM, AgentSchedule, AgentRun):
            await conn.run_sync(model.__table__.create)
    yield eng
    await eng.dispose()


@pytest.fixture
def client(engine):
    app = FastAPI()
    app.include_router(runner_ep.router)
    app.include_router(agents_ep.router)
    app.include_router(canonical_aliases())
    app.state.db_engine = engine
    with TestClient(app) as c:
        yield c, app


# -- create / list / edit / delete ---------------------------------------------


def test_create_list_edit_delete_lifecycle(client):
    c, _ = client
    created = c.post("/api/agents", json={"name": "watcher", "type": "planner", "description": "d"})
    assert created.status_code == 201
    agent_id = created.json()["id"]

    # Persisted across "reloads": both list prefixes and both detail prefixes see it.
    assert agent_id in [a["id"] for a in c.get("/agents/").json()["data"]]
    assert agent_id in [a["id"] for a in c.get("/api/agents").json()["data"]]
    assert c.get(f"/agents/{agent_id}").json()["name"] == "watcher"
    assert c.get(f"/api/agents/{agent_id}").json()["name"] == "watcher"

    edited = c.patch(f"/api/agents/{agent_id}", json={"description": "new"})
    assert edited.status_code == 200 and edited.json()["description"] == "new"

    assert c.delete(f"/api/agents/{agent_id}").status_code == 204
    assert c.get(f"/agents/{agent_id}").status_code == 404
    assert agent_id not in [a["id"] for a in c.get("/agents/").json()["data"]]
    assert c.delete(f"/api/agents/{agent_id}").status_code == 404


def test_delete_refused_for_builtin(client):
    c, _ = client
    resp = c.delete("/api/agents/planner")
    assert resp.status_code == 405
    assert "Built-in" in resp.json()["detail"]


def test_builtin_rows_explain_what_they_do(client):
    c, _ = client
    row = c.get("/agents/planner").json()
    assert row["description"] and row["runs_when"]


def test_schedule_validation_and_endpoints(client):
    c, _ = client
    agent_id = c.post("/api/agents", json={"name": "s", "type": "planner"}).json()["id"]
    assert (
        c.put(
            f"/api/agents/{agent_id}/schedule", json={"mode": "interval", "interval_seconds": 5}
        ).status_code
        == 422
    )
    ok = c.put(
        f"/api/agents/{agent_id}/schedule",
        json={"mode": "interval", "interval_seconds": 300, "priority": "time_critical"},
    )
    assert ok.status_code == 200 and ok.json()["next_run_at"]
    assert c.get(f"/api/agents/{agent_id}/schedule").json()["interval_seconds"] == 300
    assert c.put("/api/agents/nope/schedule", json={"mode": "on_demand"}).status_code == 404
    assert c.get("/api/agents/runner/status").json()["state"] == NORMAL


def test_runner_without_db_degrades_honestly():
    app = FastAPI()
    app.include_router(runner_ep.router)
    with TestClient(app) as c:
        status = c.get("/api/agents/runner/status")
        assert status.status_code == 200 and status.json()["running"] is False
        assert c.post("/api/agents/x/run").status_code == 503


# -- runner ---------------------------------------------------------------------


async def _make_agent(engine, name="a1") -> str:
    svc = AgentService(engine, session_factory=make_session_factory(engine))
    return (await svc.create(name=name, agent_type="planner", description="watches things"))["id"]


def _runner(engine, infer, clock, **kw) -> AgentRunner:
    return AgentRunner(
        RunnerStore(make_session_factory(engine)),
        infer,
        clock=clock,
        skillhub=SkillHubClient(key=""),
        **kw,
    )


async def test_scheduled_agent_runs_on_cadence_and_counts(engine):
    clock = FakeClock()
    calls: list[str] = []
    outcomes = iter(["ok", "boom", "ok"])

    async def infer(agent, prompt):
        calls.append(prompt)
        if next(outcomes) == "boom":
            raise RuntimeError("model exploded")
        return {"content": "report", "input_tokens": 3, "output_tokens": 4, "e2e_latency_ms": 120}

    agent_id = await _make_agent(engine)
    runner = _runner(engine, infer, clock)
    await runner.set_schedule(agent_id, {"mode": "interval", "interval_seconds": 600})

    assert await runner.tick() == []  # not due yet
    clock.advance(seconds=600)
    assert [r["status"] for r in await runner.tick()] == ["completed"]
    assert await runner.tick() == []  # next run is 600s out
    clock.advance(seconds=600)
    assert [r["status"] for r in await runner.tick()] == ["failed"]
    clock.advance(seconds=600)
    assert [r["status"] for r in await runner.tick()] == ["completed"]
    assert len(calls) == 3

    runs = await runner.store.runs(agent_id)
    assert [r["status"] for r in runs] == ["completed", "failed", "completed"]
    assert runs[0]["output"] == "report" and "model exploded" in runs[1]["error"]

    svc = AgentService(engine, session_factory=make_session_factory(engine))
    row = await svc.get(agent_id)
    assert (row["tasksCompleted"], row["tasksFailed"]) == (2, 1)


async def test_on_demand_run_and_missing_agent(engine):
    async def infer(agent, prompt):
        return {"content": "hi", "input_tokens": 1, "output_tokens": 1}

    runner = _runner(engine, infer, FakeClock())
    agent_id = await _make_agent(engine)
    run = await runner.run_agent(agent_id, "on_demand")
    assert run["status"] == "completed" and run["trigger"] == "on_demand"
    from backend.agents.runner import AgentMissingError

    with pytest.raises(AgentMissingError):
        await runner.run_agent("missing")


async def test_throttle_signal_backs_off_and_stretches_interval(engine):
    clock = FakeClock()

    class Throttled(Exception):
        reasons = {("m", "m"): "http_429"}

    async def infer(agent, prompt):
        raise Throttled()

    agent_id = await _make_agent(engine)
    runner = _runner(engine, infer, clock)
    await runner.set_schedule(agent_id, {"mode": "interval", "interval_seconds": 600})
    clock.advance(seconds=600)
    await runner.tick()
    assert runner.backoff.evaluate(clock()) == DEGRADED
    sched = await runner.get_schedule(agent_id)
    assert datetime.fromisoformat(sched["next_run_at"]) == clock() + timedelta(seconds=1200)


async def test_skipped_when_agent_inactive(engine):
    async def infer(agent, prompt):
        raise AssertionError("must not run")

    agent_id = await _make_agent(engine)
    async with make_session_factory(engine)() as s:
        row = await s.get(AgentORM, agent_id)
        row.status = "paused"
        await s.commit()
    run = await _runner(engine, infer, FakeClock()).run_agent(agent_id, "on_demand")
    assert run["status"] == "skipped"


# -- schedule math ----------------------------------------------------------------


def test_daily_next_run_uses_timezone():
    sched = validate_schedule(
        {"mode": "daily", "daily_time": "07:30", "timezone": "America/Los_Angeles"}
    )
    sched["enabled"] = True
    nxt = compute_next_run(sched, T0)  # 12:00 UTC = 04:00 PST
    assert nxt == datetime(2026, 1, 1, 15, 30, tzinfo=UTC)
    assert compute_next_run({**sched, "enabled": False}, T0) is None


def test_validate_schedule_rejects_bad_input():
    for bad in (
        {"mode": "weekly"},
        {"mode": "daily", "daily_time": "25:00"},
        {"mode": "daily", "daily_time": "07:00", "timezone": "Mars/Base"},
        {"mode": "interval"},
        {"mode": "interval", "interval_seconds": 120, "priority": "urgent"},
    ):
        with pytest.raises(ValueError):
            validate_schedule(bad)


# -- back-off -----------------------------------------------------------------------


def test_backoff_stretches_background_before_time_critical_and_recovers():
    b = BackoffController(BackoffConfig(recover_after=timedelta(minutes=5)))
    assert b.evaluate(T0) == NORMAL and b.stretch("background") == 1.0

    b.record_throttle(T0)
    assert b.evaluate(T0) == DEGRADED
    assert b.stretch("background") > 1.0
    assert b.stretch("time_critical") == 1.0

    for i in range(3):
        b.record_throttle(T0 + timedelta(seconds=i))
    assert b.evaluate(T0 + timedelta(seconds=3)) == BACKED_OFF
    assert b.stretch("background") > b.stretch("time_critical") > 1.0

    # Signals age out of the 10 minute window; recovery goes one step at a time.
    t = T0 + timedelta(minutes=8)
    assert b.evaluate(t) == BACKED_OFF  # throttles still in the window
    t = T0 + timedelta(minutes=11)
    assert b.evaluate(t) == BACKED_OFF  # signals clear, 5 minute timer running
    t = T0 + timedelta(minutes=14)
    assert b.evaluate(t) == DEGRADED  # one step down
    assert b.evaluate(t + timedelta(minutes=2)) == DEGRADED
    assert b.evaluate(t + timedelta(minutes=5)) == NORMAL


def test_backoff_latency_and_health_signals():
    b = BackoffController()
    for _ in range(3):
        b.record_latency(T0, 25_000)
    assert b.evaluate(T0) == DEGRADED
    b2 = BackoffController()
    b2.record_health(False)
    assert b2.evaluate(T0) == BACKED_OFF
    b2.record_health(None)  # unknown is not a signal
    assert b2.snapshot(T0)["health_ok"] is None


# -- skill hub ------------------------------------------------------------------------


async def test_skillhub_without_key_is_inert():
    hub = SkillHubClient(key="")
    assert not hub.configured
    assert await hub.skills_context("billing reminders helper") == ""
    assert await hub.call_tool("omniroute_agent_skills_list") is None


async def test_skillhub_reads_catalog_and_never_calls_unlisted_tools():
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = __import__("json").loads(request.content)
        seen.append({"auth": request.headers.get("authorization"), "method": body["method"]})
        if body["method"] == "initialize":
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": 1, "result": {}},
                headers={"mcp-session-id": "sid"},
            )
        skills = [{"id": "omni-bills", "name": "bills", "description": "Track billing reminders"}]
        text = __import__("json").dumps(skills)
        payload = __import__("json").dumps(
            {"jsonrpc": "2.0", "id": 2, "result": {"content": [{"type": "text", "text": text}]}}
        )
        return httpx.Response(200, text=f"event: message\ndata: {payload}\n\n")

    hub = SkillHubClient(
        key="secret-test-key", url="https://hub.test/mcp", transport=httpx.MockTransport(handler)
    )
    block = await hub.skills_context("billing reminders helper")
    assert "omni-bills" in block
    assert await hub.call_tool("omniroute_delete_everything") is None
    assert seen[0]["method"] == "initialize" and seen[0]["auth"] == "Bearer secret-test-key"


def test_skillhub_key_falls_back_to_the_omniroute_key(monkeypatch) -> None:
    """Owner decision: Skill Hub reuses NerveCenter's OmniRoute key."""
    from backend.agents.runner.skillhub import SkillHubClient

    monkeypatch.delenv("NERVE_SKILLHUB_KEY", raising=False)
    monkeypatch.setenv("NERVE_OMNIROUTE_API_KEY", "omni-key")
    assert SkillHubClient()._key == "omni-key"
    monkeypatch.setenv("NERVE_SKILLHUB_KEY", "hub-key")
    assert SkillHubClient()._key == "hub-key"
    monkeypatch.delenv("NERVE_SKILLHUB_KEY")
    monkeypatch.delenv("NERVE_OMNIROUTE_API_KEY")
    assert SkillHubClient()._key is None
