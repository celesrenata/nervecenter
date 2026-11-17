"""Built-in agent controls (NS-ADR-116): run, schedule, edit, reset; custom delete."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from backend.agents.overrides import effective_builtin
from backend.agents.registry import AgentRegistry
from backend.agents.runner import AgentRunner, RunnerStore
from backend.agents.runner.skillhub import SkillHubClient
from backend.api.app import canonical_aliases
from backend.api.endpoints import agent_runner as runner_ep
from backend.api.endpoints import agents as agents_ep
from backend.persistence.connection import make_session_factory
from backend.persistence.models import Agent as AgentORM
from backend.persistence.models import AgentOverride, AgentRun, AgentSchedule

PLANNER_PROMPT = AgentRegistry().get("planner").prompt_template


@pytest.fixture
async def engine():
    eng = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    async with eng.begin() as conn:
        for model in (AgentORM, AgentSchedule, AgentRun, AgentOverride):
            await conn.run_sync(model.__table__.create)
    yield eng
    await eng.dispose()


@pytest.fixture
def api(engine):
    prompts: list[str] = []

    async def infer(agent, prompt):
        prompts.append(prompt)
        return {"content": f"A complete report from {agent.id}, forty characters or more", "input_tokens": 1, "output_tokens": 2}

    app = FastAPI()
    app.include_router(runner_ep.router)
    app.include_router(agents_ep.router)
    app.include_router(canonical_aliases())
    app.state.db_engine = engine
    app.state.agent_runner = AgentRunner(
        RunnerStore(make_session_factory(engine)), infer, skillhub=SkillHubClient(key="")
    )
    with TestClient(app) as c:
        yield c, prompts


def test_builtin_rows_are_flagged_and_explained(api):
    c, _ = api
    rows = c.get("/api/agents").json()["data"]
    planner = next(r for r in rows if r["id"] == "planner")
    assert planner["builtin"] is True and planner["description"] and planner["runs_when"]
    assert planner["has_override"] is False and planner["status"] == "active"


def test_builtin_run_now_records_history(api):
    c, prompts = api
    run = c.post("/api/agents/planner/run")
    assert run.status_code == 201 and run.json()["status"] == "completed"
    assert run.json()["output"].startswith("A complete report from planner")
    assert PLANNER_PROMPT in prompts[0]
    history = c.get("/api/agents/planner/runs").json()["data"]
    assert [r["id"] for r in history] == [run.json()["id"]]
    assert c.get("/api/agents/planner").json()["tasksCompleted"] == 1


def test_builtin_schedule_round_trips(api):
    c, _ = api
    put = c.put("/api/agents/planner/schedule", json={"mode": "interval", "interval_seconds": 600})
    assert put.status_code == 200 and put.json()["next_run_at"]
    assert c.get("/api/agents/planner/schedule").json()["interval_seconds"] == 600


def test_builtin_edit_is_an_override_on_top_of_the_manifest(api):
    c, prompts = api
    patched = c.patch(
        "/api/agents/planner",
        json={
            "prompt_addition": "Always list risks first.",
            "skills": ["imported:ops/runbooks"],
            "inference": {"provider": "omniroute", "model": "secure/coder"},
        },
    )
    assert patched.status_code == 200
    row = patched.json()
    assert row["has_override"] is True and row["builtin"] is True
    assert row["prompt_addition"] == "Always list risks first."
    assert row["skills"] == ["imported:ops/runbooks"]
    assert row["inference"]["model"] == "secure/coder"
    assert row["prompt_template"] == PLANNER_PROMPT  # the manifest stays read-only
    assert c.get("/agents/planner").json()["prompt_addition"] == "Always list risks first."
    c.post("/api/agents/planner/run")
    assert PLANNER_PROMPT in prompts[0] and "Always list risks first." in prompts[0]


def test_builtin_edit_refuses_manifest_fields(api):
    c, _ = api
    for body in ({"name": "x"}, {"description": "x"}, {"prompt_template": "x"}, {"tools": []}):
        resp = c.patch("/api/agents/planner", json=body)
        assert resp.status_code == 422 and "defined in code" in resp.json()["detail"]
    assert c.get("/agents/planner").json()["has_override"] is False


def test_disabled_builtin_does_not_execute(api):
    c, prompts = api
    c.patch("/api/agents/planner", json={"enabled": False})
    assert c.get("/agents/planner").json()["status"] == "disabled"
    run = c.post("/api/agents/planner/run").json()
    assert run["status"] == "skipped" and prompts == []


def test_reset_removes_override_and_schedule_but_keeps_history(api):
    c, _ = api
    c.patch("/api/agents/planner", json={"prompt_addition": "extra", "enabled": False})
    c.put("/api/agents/planner/schedule", json={"mode": "interval", "interval_seconds": 600})
    c.patch("/api/agents/planner", json={"enabled": True})
    run = c.post("/api/agents/planner/run").json()
    reset = c.post("/api/agents/planner/reset")
    assert reset.status_code == 200
    assert reset.json()["has_override"] is False and reset.json()["prompt_addition"] == ""
    assert c.get("/api/agents/planner/schedule").json()["mode"] == "on_demand"
    assert [r["id"] for r in c.get("/api/agents/planner/runs").json()["data"]] == [run["id"]]


def test_builtin_cannot_be_deleted_and_custom_cannot_be_reset(api):
    c, _ = api
    gone = c.delete("/api/agents/planner")
    assert gone.status_code == 405 and "Reset to defaults" in gone.json()["detail"]
    custom = c.post("/api/agents", json={"name": "mine", "type": "planner"}).json()
    assert c.post(f"/api/agents/{custom['id']}/reset").status_code == 404
    assert c.patch(f"/api/agents/{custom['id']}", json={"enabled": False}).status_code == 422


def test_custom_agent_keeps_full_edit_and_delete(api):
    c, _ = api
    created = c.post("/api/agents", json={"name": "mine", "type": "planner"}).json()
    assert created["builtin"] is False
    agent_id = created["id"]
    assert c.patch(f"/api/agents/{agent_id}", json={"name": "renamed"}).json()["name"] == "renamed"
    c.put(f"/api/agents/{agent_id}/schedule", json={"mode": "interval", "interval_seconds": 600})
    assert c.post(f"/api/agents/{agent_id}/run").status_code == 201
    assert c.delete(f"/api/agents/{agent_id}").status_code == 204
    assert c.get(f"/agents/{agent_id}").status_code == 404
    assert c.get(f"/api/agents/{agent_id}/runs").status_code == 404


def test_builtin_tool_gateway_uses_the_effective_agent(api):
    c, _ = api
    resp = c.post("/api/agents/planner/tools/call", json={"server": "fetch", "tool": "nope"})
    assert resp.status_code == 200 and resp.json()["denied_reason"] == "tool_not_allowed"


def test_effective_builtin_merges_without_mutating_the_manifest():
    manifest = AgentRegistry().get("planner")
    row = AgentOverride(agent_id="planner", prompt_addition="more", skills=["a"], enabled=False)
    eff = effective_builtin(manifest, row)
    assert eff.status == "disabled" and eff.skills == ["a"] and eff.builtin is True
    assert (
        eff.prompt_template.startswith(manifest.prompt_template) and "more" in eff.prompt_template
    )
    assert "more" not in manifest.prompt_template
