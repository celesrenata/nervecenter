"""Long-term researcher coverage (NS-ADR-103): resume, combo, pause, back-off."""

from __future__ import annotations

import asyncio
import json
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from backend.agents.runner.backoff import BACKED_OFF, BackoffController
from backend.api.endpoints import research as research_ep
from backend.persistence.connection import make_session_factory
from backend.persistence.models import ResearchCheckpoint, ResearchProject, ResearchStep
from backend.research import COMBO, ResearchEngine
from backend.research.engine import ResearchStateError
from backend.research.planner import parse_plan

T0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


class FakeClock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw: float) -> None:
        self.now += timedelta(**kw)


class FakeTransport:
    """Records every (model, prompt); plan, step and report replies by prompt kind."""

    def __init__(self, goals: int = 4) -> None:
        self.calls: list[tuple[str, str]] = []
        self.goals = goals
        self.gate: threading.Event | None = None
        self.gate_on_call: int | None = None
        self.entered = threading.Event()
        self.fail: Exception | None = None

    def generate(self, model: str, prompt: str, *a: Any, **k: Any) -> dict[str, Any]:
        self.calls.append((model, prompt))
        if self.fail is not None:
            raise self.fail
        if self.gate is not None and len(self.calls) == self.gate_on_call:
            self.entered.set()
            self.gate.wait(5)
        if prompt.startswith("You are a long-term research planner"):
            content = json.dumps([f"goal {i}" for i in range(self.goals)])
        elif prompt.startswith("Write the final research report"):
            content = "FINAL REPORT"
        else:
            content = "finding for " + prompt.split("Now do step ")[1].split(":")[0]
        return {"content": content, "input_tokens": 5, "output_tokens": 7, "e2e_latency_ms": 100.0}


@pytest.fixture
async def factory():
    eng = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    async with eng.begin() as conn:
        for model in (ResearchProject, ResearchStep, ResearchCheckpoint):
            await conn.run_sync(model.__table__.create)
    yield make_session_factory(eng), eng
    await eng.dispose()


def make_engine(factory, transport, clock=None, backoff=None) -> ResearchEngine:
    return ResearchEngine(factory[0], transport, clock=clock or FakeClock(), backoff=backoff)


def step_prompts(transport: FakeTransport) -> list[str]:
    return [p for _, p in transport.calls if "Now do step" in p]


def test_parse_plan_json_and_numbered_fallback():
    assert parse_plan('Here: ["a", {"goal": "b"}, ""]') == ["a", "b"]
    assert parse_plan("1. first\n2) second\n- third") == ["first", "second", "third"]
    assert parse_plan("nothing useful") == []


async def test_full_run_uses_only_the_combo_and_writes_a_checkpoint_per_step(factory):
    t = FakeTransport(goals=3)
    eng = make_engine(factory, t)
    proj = await eng.create("T", "Q?", 4.0, 30)
    for _ in range(6):
        await eng.advance(proj["id"])
    detail = await eng.detail(proj["id"])
    assert detail["status"] == "done"
    assert [s["status"] for s in detail["steps"]] == ["done"] * 3
    assert (await eng.report(proj["id"]))["report"] == "FINAL REPORT"
    # plan + 3 steps + report, all on the combo
    assert len(t.calls) == 5
    assert {m for m, _ in t.calls} == {COMBO} == {"secure/planner"}
    async with factory[0]() as s:
        cps = (
            (await s.execute(select(ResearchCheckpoint).order_by(ResearchCheckpoint.step_cursor)))
            .scalars()
            .all()
        )
    assert [c.step_cursor for c in cps][:4] == [0, 1, 2, 3]
    assert "Step 2" in detail["working_notes"] or detail["checkpoint_cursor"] == 3


async def test_restart_resumes_from_checkpoint_without_repeating_steps(factory):
    t1 = FakeTransport(goals=4)
    t1.gate, t1.gate_on_call = threading.Event(), 4  # call 1 plan, 2-3 steps 0-1, 4 = step 2
    eng1 = make_engine(factory, t1)
    proj = await eng1.create("T", "Q?", 4.0, 30)
    pid = proj["id"]

    async def loop_forever() -> None:
        while True:
            await eng1.advance(pid)

    task = asyncio.create_task(loop_forever())
    while not t1.entered.is_set():
        await asyncio.sleep(0.01)
    task.cancel()  # kill the loop mid-step
    with pytest.raises(asyncio.CancelledError):
        await task
    t1.gate.set()
    await asyncio.sleep(0.05)
    async with factory[0]() as s:
        statuses = [
            r.status
            for r in (
                await s.execute(select(ResearchStep).order_by(ResearchStep.ordinal))
            ).scalars()
        ]
    assert statuses == ["done", "done", "running", "pending"]

    # A fresh engine (new process) picks up from the checkpoint.
    t2 = FakeTransport(goals=4)
    eng2 = make_engine(factory, t2)
    for _ in range(4):
        await eng2.advance(pid)
    detail = await eng2.detail(pid)
    assert detail["status"] == "done"
    ran_again = step_prompts(t2)
    assert len(ran_again) == 2
    assert "step 3:" in ran_again[0] and "step 4:" in ran_again[1]
    assert not any("step 1:" in p or "step 2:" in p for p in ran_again)
    assert not any(p.startswith("You are a long-term research planner") for _, p in t2.calls)
    assert {m for m, _ in t2.calls} == {COMBO}


async def test_pause_and_resume(factory):
    clock = FakeClock()
    t = FakeTransport(goals=2)
    eng = make_engine(factory, t, clock)
    pid = (await eng.create("T", "Q?", 4.0, 30))["id"]
    assert (await eng.tick(wait=True)) == [pid]  # plans
    assert (await eng.pause(pid))["status"] == "paused"
    clock.advance(hours=1)
    calls = len(t.calls)
    assert await eng.tick(wait=True) == []
    assert await eng.advance(pid) == "idle"
    assert len(t.calls) == calls
    with pytest.raises(ResearchStateError):
        await eng.pause(pid)
    assert (await eng.resume(pid))["status"] == "running"
    assert await eng.tick(wait=True) == [pid]
    assert len(t.calls) == calls + 1


async def test_cadence_spaces_steps_and_backoff_stretches_and_holds(factory):
    clock = FakeClock()
    t = FakeTransport(goals=3)
    backoff = BackoffController()
    eng = make_engine(factory, t, clock, backoff)
    pid = (await eng.create("T", "Q?", 4.0, 100))["id"]
    await eng.tick(wait=True)  # plan; next step due in 100 s (normal)
    assert await eng.tick(wait=True) == []  # not due yet
    clock.advance(seconds=101)
    assert await eng.tick(wait=True) == [pid]
    steps_run = len(step_prompts(t))
    assert steps_run == 1

    # Inference becomes congested: three throttles back the controller off.
    for _ in range(3):
        backoff.record_throttle(clock.now)
    assert backoff.evaluate(clock.now) == BACKED_OFF
    clock.advance(seconds=101)
    assert await eng.tick(wait=True) == []  # due, but held while backed off
    assert len(step_prompts(t)) == steps_run
    detail = await eng.detail(pid)
    at = datetime.fromisoformat(detail["next_step_at"])
    assert at - clock.now >= timedelta(seconds=100 * 6 - 1)  # background stretch 6x

    # Recovery: after the clear period the controller steps down and work resumes.
    clock.advance(minutes=30)
    for _ in range(3):
        backoff.evaluate(clock.now)
        clock.advance(minutes=6)
    assert backoff.state != BACKED_OFF
    assert await eng.tick(wait=True) == [pid]


async def test_throttled_failure_feeds_backoff_and_fails_after_three(factory):
    clock = FakeClock()
    t = FakeTransport(goals=2)
    backoff = BackoffController()
    eng = make_engine(factory, t, clock, backoff)
    pid = (await eng.create("T", "Q?", 4.0, 30))["id"]
    t.fail = RuntimeError("http_503")
    for _ in range(3):
        await eng.advance(pid)
    detail = await eng.detail(pid)
    assert detail["status"] == "failed" and "http_503" in detail["error"]
    assert backoff.snapshot(clock.now)["throttles_in_window"] == 3
    t.fail = None
    assert (await eng.resume(pid))["status"] == "planned"
    for _ in range(5):
        await eng.advance(pid)
    assert (await eng.detail(pid))["status"] == "done"


async def test_budget_exhaustion_skips_remaining_steps_and_reports(factory):
    t = FakeTransport(goals=5)
    eng = make_engine(factory, t)
    pid = (await eng.create("T", "Q?", 0.0002, 30))["id"]  # ~0.72 s; each call counts 0.1 s
    async with factory[0]() as s:
        p = await s.get(ResearchProject, pid)
        p.spent_seconds = 0.5
        await s.commit()
    for _ in range(6):
        await eng.advance(pid)
    detail = await eng.detail(pid)
    assert detail["status"] == "done"
    statuses = [s["status"] for s in detail["steps"]]
    assert statuses.count("skipped") >= 1 and statuses.count("done") >= 1


def test_no_other_provider_call_in_research_package():
    root = Path(__file__).resolve().parents[2] / "backend" / "research"
    text = "".join(p.read_text() for p in root.glob("*.py"))
    for needle in ("openai", "anthropic", "httpx", "requests", "providers"):
        assert needle not in text.lower().replace("omniroute", ""), needle
    assert text.count("self._transport.generate") == 1


@pytest.fixture
def client(factory):
    app = FastAPI()
    app.include_router(research_ep.router)
    app.state.db_engine = factory[1]
    with TestClient(app) as c:
        yield c


def test_api_create_detail_pause_resume_notes_report(client):
    created = client.post("/api/research", json={"title": "T", "question": "Q?"})
    assert created.status_code == 201 and created.json()["combo"] == COMBO
    pid = created.json()["id"]
    assert (
        client.post(
            "/api/research", json={"title": "T", "question": "Q?", "cadence_seconds": 1}
        ).status_code
        == 422
    )
    assert [p["id"] for p in client.get("/api/research").json()["projects"]] == [pid]
    detail = client.get(f"/api/research/{pid}").json()
    assert detail["steps"] == [] and detail["status"] == "planned"
    assert client.post(f"/api/research/{pid}/pause").json()["status"] == "paused"
    assert client.post(f"/api/research/{pid}/pause").status_code == 409
    assert client.post(f"/api/research/{pid}/resume").json()["status"] == "planned"
    assert (
        client.put(f"/api/research/{pid}/notes", json={"notes": "mine"}).json()["notes"] == "mine"
    )
    assert client.get(f"/api/research/{pid}/report").json()["report"] is None
    assert client.get("/api/research/nope").status_code == 404


def test_api_without_db_is_503():
    app = FastAPI()
    app.include_router(research_ep.router)
    with TestClient(app) as c:
        assert c.get("/api/research").status_code == 503
