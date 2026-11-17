"""Per-step approval of infra plans: selection, requires, on_failure_of (NS-ADR-120)."""
from __future__ import annotations

from typing import Any

import pytest

from backend.infra.service import InfraInputError, InfraService

from test_infra import _app, _kube, svc  # noqa: F401 - svc is a fixture

OC = "oc-" + "a1b2c3d4" * 6


def _step(name: str, **extra: Any) -> dict[str, Any]:
    return {"connector": "home", "args": ["rollout", "restart", f"deploy/{name}"], **extra}


async def _plan(svc: InfraService, steps: list[dict[str, Any]]) -> str:
    if not await svc.list_connectors():
        await _kube(svc)
    return (await svc.propose("a", {"title": "t", "steps": steps}))["id"]


async def _approve(svc: InfraService, pid: str, sel: list[int] | None = None) -> dict[str, Any]:
    return await svc.decide(pid, approve=True, actor="owner", act_mode="opencode", selected_steps=sel)


async def test_step_fields_are_validated_and_stored(svc) -> None:
    await _kube(svc)
    plan = await svc.propose("a", {"title": "t", "steps": [
        _step("a", kind="read"), _step("b", requires=[0], kind="change"),
        _step("c", on_failure_of=[1])]})
    assert plan["steps"][1]["requires"] == [0] and plan["steps"][2]["on_failure_of"] == [1]
    assert plan["selected_steps"] is None and plan["guard"] is None
    for bad in ({"requires": [1]}, {"requires": [0, True]}, {"requires": "0"},
                {"on_failure_of": [5]}, {"kind": "nuke"}):
        with pytest.raises(InfraInputError):
            await svc.propose("a", {"title": "t", "steps": [_step("a"), _step("b", **bad)]})


async def test_a_subset_runs_only_selected_steps_in_order(svc) -> None:
    pid = await _plan(svc, [_step("a"), _step("b"), _step("c")])
    plan = await _approve(svc, pid, [2, 0])
    assert plan["selected_steps"] == [0, 2]
    await svc.run_all(pid)
    done = await svc.get_plan(pid)
    assert [r["index"] for r in done["results"]] == [0, 2] and done["status"] == "done"
    with pytest.raises(InfraInputError):
        await svc.run_step(pid, 1)


async def test_run_step_refuses_unselected_and_out_of_order(svc) -> None:
    pid = await _plan(svc, [_step("a"), _step("b"), _step("c")])
    await svc.decide(pid, approve=True, actor="o", act_mode="opencode", selected_steps=[0, 2])
    for idx in (1, 2):
        with pytest.raises(InfraInputError, match="in order"):
            await svc.run_step(pid, idx)
    assert (await svc.run_step(pid, 0))["ok"]
    with pytest.raises(InfraInputError, match="in order"):
        await svc.run_step(pid, 1)
    assert (await svc.run_step(pid, 2))["ok"]
    assert (await svc.get_plan(pid))["status"] == "done"
    assert "0, 2" in svc._test_handoffs.calls[0]["brief"]


async def test_selection_must_include_required_steps(svc) -> None:
    pid = await _plan(svc, [_step("a"), _step("b", requires=[0])])
    for bad in ([1], [], [0, 0], [3], "x", [True]):
        with pytest.raises(InfraInputError):
            await _approve(svc, pid, bad)  # type: ignore[arg-type]
    with pytest.raises(InfraInputError, match="step 1 requires step 0"):
        await _approve(svc, pid, [1])
    assert (await svc.get_plan(pid))["status"] == "proposed"
    assert (await _approve(svc, pid, [0, 1]))["selected_steps"] == [0, 1]


async def test_null_selection_runs_everything(svc) -> None:
    pid = await _plan(svc, [_step("a"), _step("b")])
    plan = await _approve(svc, pid)
    await svc.run_all(pid)
    assert plan["selected_steps"] is None
    done = await svc.get_plan(pid)
    assert done["status"] == "done" and len(done["results"]) == 2


async def test_requires_a_successful_step(svc) -> None:
    pid = await _plan(svc, [_step("fail"), _step("b", requires=[0])])
    await _approve(svc, pid)
    await svc.run_step(pid, 0)
    with pytest.raises(InfraInputError):  # plan is failed
        await svc.run_step(pid, 1)
    pid2 = await _plan(svc, [_step("fail"), _step("x"), _step("y", requires=[0])])
    await _approve(svc, pid2, [0, 2])
    res = await svc.run_step(pid2, 0)
    assert not res["ok"]
    with pytest.raises(InfraInputError):
        await svc.run_step(pid2, 2)


async def test_fallback_skips_when_target_succeeds(svc) -> None:
    pid = await _plan(svc, [_step("a"), _step("b", on_failure_of=[0])])
    await _approve(svc, pid)
    await svc.run_all(pid)
    done = await svc.get_plan(pid)
    assert done["status"] == "done"
    assert done["results"][1] == {"index": 1, "ok": True, "skipped": True, "output": "[skipped]"}


async def test_handled_failure_does_not_fail_the_plan(svc) -> None:
    pid = await _plan(svc, [_step("fail"), _step("b", on_failure_of=[0])])
    await _approve(svc, pid)
    first = await svc.run_step(pid, 0)
    assert not first["ok"] and (await svc.get_plan(pid))["status"] == "running"
    assert (await svc.run_step(pid, 1))["ok"]
    done = await svc.get_plan(pid)
    assert done["status"] == "done" and not done["results"][1].get("skipped")


async def test_failed_fallback_and_unhandled_failure_stop_the_plan(svc) -> None:
    pid = await _plan(svc, [_step("fail"), _step("fail2", on_failure_of=[0]), _step("c")])
    await _approve(svc, pid)
    await svc.run_all(pid)
    got = await svc.get_plan(pid)
    assert got["status"] == "failed" and [r["index"] for r in got["results"]] == [0, 1]
    pid2 = await _plan(svc, [_step("fail"), _step("c")])
    await _approve(svc, pid2)
    await svc.run_all(pid2)
    got2 = await svc.get_plan(pid2)
    assert got2["status"] == "failed" and len(got2["results"]) == 1


async def test_mcp_run_step_follows_the_next_selected_rule(svc, monkeypatch) -> None:
    from fastapi.testclient import TestClient

    from backend.projects import mcp_server as pm
    monkeypatch.setenv("NERVE_MCP_PROJECTS_TOKENS", "opencode:" + OC)
    pid = await _plan(svc, [_step("a"), _step("b"), _step("c", requires=[0])])
    await svc.decide(pid, approve=True, actor="o", act_mode="opencode", selected_steps=[0, 2])
    app = _app(svc, {"sub": "owner", "typ": "access"})
    app.state.mcp_project_verifiers = pm.build_verifiers()

    def call(name: str, args: dict[str, Any]) -> dict[str, Any]:
        r = c.post("/api/mcp/infra", headers={"Authorization": f"Bearer {OC}"}, json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": name, "arguments": args}})
        return r.json()["result"]

    with TestClient(app) as c:
        assert call("infra_get_plan", {"plan_id": pid})["structuredContent"][
            "selected_steps"] == [0, 2]
        assert call("infra_run_step", {"plan_id": pid, "index": 1})["isError"] is True
        assert call("infra_run_step", {"plan_id": pid, "index": 2})["isError"] is True
        assert call("infra_run_step", {"plan_id": pid, "index": 0})["isError"] is False
        assert call("infra_run_step", {"plan_id": pid, "index": 1})["isError"] is True
        assert call("infra_run_step", {"plan_id": pid, "index": 2})["isError"] is False


async def test_approve_endpoint_validates_selected_steps(svc) -> None:
    from fastapi.testclient import TestClient
    pid = await _plan(svc, [_step("a"), _step("b", requires=[0])])
    app = _app(svc, {"sub": "owner", "typ": "access"})
    with TestClient(app) as c:
        r = c.post(f"/api/infra/plans/{pid}/approve",
                   json={"act_mode": "opencode", "selected_steps": "all"})
        assert r.status_code == 422
        r = c.post(f"/api/infra/plans/{pid}/approve",
                   json={"act_mode": "opencode", "selected_steps": [1]})
        assert r.status_code == 422 and "requires step 0" in r.text
        r = c.post(f"/api/infra/plans/{pid}/approve",
                   json={"act_mode": "opencode", "selected_steps": [0]})
        assert r.status_code == 200 and r.json()["selected_steps"] == [0]
