"""Kube monitor: cadence, claim-before-work and the new-issue trigger rules (fakes only)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from backend.agents.runner.runner import AgentBusyError, AgentRunCapError
from backend.kube.issues import IssueStore, ReportStore
from backend.kube.monitor import KubeMonitor
from backend.kube.settings import KubeOverrideStore, KubeSettingsStore
from backend.persistence.connection import make_session_factory
from backend.persistence.models.agent_runner import AgentRun
from backend.persistence.models.kube import (
    KubeIssue,
    KubeOverride,
    KubeReport,
    KubeSettings,
)

START = datetime(2030, 1, 1, 4, 0, tzinfo=UTC)


def _pod(ns: str, name: str, reason: str = "CrashLoopBackOff") -> dict[str, Any]:
    return {"key": f"pod/{ns}/{name}/{reason}", "kind": "pod", "namespace": ns, "name": name,
            "reason": reason, "severity": "critical", "restarts": 7}


class Rig:
    def __init__(self, factory: Any) -> None:
        self.factory = factory
        self.now = START
        self.pods: list[dict[str, Any]] = []
        self.calls = {"collect": 0, "waste": 0}
        self.runs: list[dict[str, Any]] = []
        self.tasks: list[dict[str, Any]] = []
        self.busy = False
        self.other_busy = False
        self.naps = 0
        self.events: list[dict[str, Any]] = [
            {"kind": "tool_call", "data": {"server": "nerve", "tool": "kube_describe"}}]
        self.output = ('Report.\nDIAGNOSIS: {"summary": "bad image", "cause": "tag", '
                       '"evidence": ["manifest unknown"], "confidence": "high"}')
        self.raise_exc: Exception | None = None
        self.fail_collect = False
        self.settings = KubeSettingsStore(factory)
        self.issues = IssueStore(factory)
        self.reports = ReportStore(factory)
        self.overrides = KubeOverrideStore(factory, self.settings)
        self.monitor = KubeMonitor(
            factory, self.collect, self.waste, self.run_agent, settings_store=self.settings,
            issue_store=self.issues, report_store=self.reports, override_store=self.overrides,
            create_task=self.create_task, inflight=lambda: self.busy,
            other_busy=lambda: self.other_busy, tool_events=lambda: self.events,
            stagger_seconds=0, sleep=self.nap, now=lambda: self.now)

    async def nap(self, seconds: float) -> None:
        self.naps += 1
        if self.naps > 3:
            self.other_busy = False  # the other agent run ends

    async def go(self) -> dict[str, Any]:
        """A check, then the background focus pass it started."""
        result = await self.monitor.check()
        await self.monitor.wait_focus()
        return result

    async def collect(self) -> dict[str, Any]:
        self.calls["collect"] += 1
        if self.fail_collect:
            raise RuntimeError("no cluster")
        return {"pods_unhealthy": list(self.pods), "warnings": []}

    async def waste(self, url: str | None) -> dict[str, Any]:
        self.calls["waste"] += 1
        return {"idle_pods": {"count": 0, "items": []}}

    async def run_agent(self, agent_id: str, trigger: str = "", context: str = "") -> Any:
        if self.raise_exc:
            raise self.raise_exc
        self.runs.append({"agent": agent_id, "trigger": trigger, "context": context})
        async with self.factory() as s:
            s.add(AgentRun(agent_id=agent_id, started_at=self.now, status="completed",
                           trigger=trigger))
            await s.commit()
        return {"id": f"run-{len(self.runs)}", "status": "completed", "output": self.output}

    async def create_task(self, payload: dict[str, Any]) -> None:
        self.tasks.append(payload)


@pytest.fixture
async def rig(tmp_path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'k.db'}", poolclass=StaticPool,
                                 connect_args={"check_same_thread": False})
    async with engine.begin() as conn:
        await conn.run_sync(KubeSettings.__table__.metadata.create_all, tables=[
            KubeSettings.__table__, KubeIssue.__table__, KubeOverride.__table__,
            KubeReport.__table__, AgentRun.__table__])
    yield Rig(make_session_factory(engine))
    await engine.dispose()


async def test_check_runs_hourly_and_waste_daily(rig: Rig) -> None:
    await rig.monitor.tick()
    assert rig.calls == {"collect": 1, "waste": 1}
    rig.now = START + timedelta(minutes=59)
    await rig.monitor.tick()
    assert rig.calls["collect"] == 1
    rig.now = START + timedelta(hours=1, minutes=1)
    await rig.monitor.tick()
    assert rig.calls == {"collect": 2, "waste": 1}
    rig.now = START + timedelta(hours=24, minutes=1)
    await rig.monitor.tick()
    assert rig.calls == {"collect": 3, "waste": 2}
    assert (await rig.reports.latest("waste")) is not None
    assert (await rig.reports.latest("snapshot")) is not None


async def test_state_is_claimed_before_work_so_a_crash_does_not_loop(rig: Rig) -> None:
    rig.fail_collect = True
    await rig.monitor.tick()
    assert rig.monitor.last["warnings"] and rig.calls["collect"] == 1  # a warning, not a raise
    await rig.monitor.tick()
    assert rig.calls["collect"] == 1
    again = KubeMonitor(rig.factory, rig.collect, rig.waste, rig.run_agent,
                        settings_store=rig.settings, issue_store=rig.issues,
                        report_store=rig.reports, now=lambda: rig.now)
    await again.tick()
    assert rig.calls["collect"] == 1


def _grouped(ns: str, workload: str, pods: list[str], reason: str = "ImagePullBackOff"
             ) -> dict[str, Any]:
    return {**_pod(ns, workload, reason), "pods": pods, "pod": pods[0]}
async def test_related_pods_are_one_group_and_one_focus_run(rig: Rig) -> None:
    rig.pods = [_grouped("stonks-oracle", "api", ["api-1", "api-2"]),
                _grouped("stonks-oracle", "worker", ["worker-1"], "ErrImagePull"),
                _pod("apps", "web")]
    result = await rig.go()
    assert result["open"] == 3 and len(rig.runs) == 2  # one per group, not per pod or issue
    run = next(r for r in rig.runs if "stonks-oracle" in r["context"])
    assert run["agent"] == "kube_doctor" and run["trigger"] == "kube_issue"
    assert "id=stonks-oracle:imagepull" in run["context"] and "api-1, api-2" in run["context"]
    assert "apps" not in run["context"].split("pods (")[1]  # only that group
    diag = (await rig.monitor._diagnoses.all())["stonks-oracle:imagepull"]
    assert diag["status"] == "done" and diag["summary"] == "bad image"
    assert diag["evidence"] == ["manifest unknown"] and diag["run_id"].startswith("run-")
    rig.now += timedelta(hours=1)
    await rig.go()
    assert len(rig.runs) == 2  # inside the 6 h cooldown: no repeat
async def test_group_runs_wait_for_other_agents_and_are_staggered(rig: Rig) -> None:
    rig.pods = [_pod("apps", "a"), _pod("apps", "b")]
    rig.other_busy = True
    await rig.go()
    assert len(rig.runs) == 2 and rig.naps >= 4  # waited for the other agent, then paced
async def test_known_down_group_and_diagnose_only_group(rig: Rig) -> None:
    rig.pods = [_pod("stonks-beta", "x"), _pod("nervecenter", "api")]
    await rig.go()
    assert len(rig.runs) == 1 and "mode=diagnose_only" in rig.runs[0]["context"]
async def test_run_without_a_describe_logs_or_events_call_is_failed(rig: Rig) -> None:
    rig.pods = [_pod("apps", "a")]
    rig.events = [{"kind": "tool_call", "data": {"server": "nerve", "tool": "kube_health"}}]
    await rig.go()
    diag = (await rig.monitor._diagnoses.all())["apps:crashloopbackoff-a"]
    assert diag["status"] == "failed" and "no evidence" in diag["error"]
    rig.events = [{"kind": "tool_call", "data": {"tool": "kubectl", "args": {
        "args": ["get", "pods", "-A"]}}}]
    rig.now += timedelta(hours=2)  # a failed group is retried after an hour, not six
    await rig.go()
    assert len(rig.runs) == 2
    rig.events = [{"kind": "tool_call", "data": {"tool": "kubectl", "args": {
        "args": ["describe", "pod", "a"]}}}]
    rig.now += timedelta(hours=2)
    await rig.go()
    assert (await rig.monitor._diagnoses.all())["apps:crashloopbackoff-a"]["status"] == "done"
async def test_cooldown_applies_to_a_recurring_group(rig: Rig) -> None:
    rig.pods = [_pod("apps", "a")]
    await rig.go()
    rig.pods = []
    rig.now += timedelta(hours=1)
    await rig.go()
    rig.pods = [_pod("apps", "a")]
    rig.now += timedelta(hours=1)
    await rig.go()
    assert len(rig.runs) == 1
    rig.now = START + timedelta(hours=7)
    await rig.go()
    assert len(rig.runs) == 2
async def test_daily_cap_is_respected(rig: Rig) -> None:
    await rig.settings.update({"daily_run_cap": 2}, "t")
    rig.pods = [_pod("apps", f"p{n}") for n in range(4)]
    result = await rig.go()
    assert len(rig.runs) == 2 and any("cap reached" in w for w in result["warnings"])
async def test_default_daily_cap_is_24(rig: Rig) -> None:
    assert (await rig.settings.get())["daily_run_cap"] == 24
async def test_known_down_never_triggers(rig: Rig) -> None:
    rig.pods = [_pod("stonks-beta", "x")]
    result = await rig.go()
    assert rig.runs == [] and rig.tasks == []
    assert result["known_down"] == 1 and result["open"] == 0
async def test_in_flight_run_waits_then_runs(rig: Rig) -> None:
    rig.pods = [_pod("apps", "a")]
    rig.busy = True
    result = await rig.monitor.check()
    assert result["triggered"] == ["apps:crashloopbackoff-a"]
    await rig.monitor.wait_focus()
    assert rig.runs == [] or rig.naps > 0
async def test_busy_and_cap_errors_are_recorded_not_raised(rig: Rig) -> None:
    rig.pods = [_pod("apps", "a")]
    for exc in (AgentBusyError("x"), AgentRunCapError("x")):
        rig.raise_exc = exc
        rig.now += timedelta(hours=2)
        await rig.go()
        diag = (await rig.monitor._diagnoses.all())["apps:crashloopbackoff-a"]
        assert diag["status"] == "failed" and "not started" in diag["error"]
async def test_tick_starts_a_pending_group_without_a_new_check(rig: Rig) -> None:
    rig.pods = [_pod("apps", "a")]
    await rig.settings.update({"auto_agent_enabled": False}, "t")
    await rig.monitor.check()
    await rig.settings.update({"auto_agent_enabled": True}, "t")
    rig.now += timedelta(minutes=10)
    await rig.monitor.tick()
    await rig.monitor.wait_focus()
    assert len(rig.runs) == 1
async def test_draft_tasks_for_new_normal_issues_capped_at_five(rig: Rig) -> None:
    rig.pods = [_pod("apps", f"p{n}") for n in range(8)] + [_pod("nervecenter", "own")]
    result = await rig.monitor.check()
    assert len(rig.tasks) == 5 and result["draft_tasks"] == 5
    task = rig.tasks[0]
    assert task["status"] == "draft" and task["inputs"]["tag"] == "agent:kube_doctor"
    assert all("nervecenter" not in t["title"] for t in rig.tasks)  # protected: diagnose only


async def test_master_switch_stops_the_hourly_check_but_not_check_now(rig: Rig) -> None:
    await rig.settings.update({"check_enabled": False}, "celes")
    await rig.monitor.tick()
    assert rig.calls["collect"] == 0 and rig.calls["waste"] == 1
    await rig.monitor.check()  # the owner's "Check now" still works
    assert rig.calls["collect"] == 1


async def test_waste_switch_stops_the_daily_report(rig: Rig) -> None:
    await rig.settings.update({"waste_enabled": False}, "celes")
    await rig.monitor.tick()
    assert rig.calls == {"collect": 1, "waste": 0}


async def test_auto_agent_switch_stops_runs_but_not_the_check(rig: Rig) -> None:
    rig.pods = [_pod("apps", "web")]
    await rig.settings.update({"auto_agent_enabled": False}, "celes")
    result = await rig.go()
    assert rig.runs == [] and result["new"] == 1 and result["triggered"] is None
    assert any("switched off" in w for w in result["warnings"])
    await rig.settings.update({"auto_agent_enabled": True}, "celes")
    rig.now = START + timedelta(hours=1)
    await rig.go()
    assert len(rig.runs) == 1  # the issue was never looked at, so it gets its turn


async def test_switching_known_down_off_makes_the_namespace_checked(rig: Rig) -> None:
    rig.pods = [_pod("stonks-beta", "x")]
    first = await rig.go()
    assert first["known_down"] == 1 and rig.runs == []
    await rig.settings.update({"known_down_off": ["stonks-beta"]}, "celes")
    rig.now = START + timedelta(hours=1)
    second = await rig.go()
    assert second["known_down"] == 0 and second["open"] == 1
    assert len(rig.runs) == 1 and "stonks-beta" in rig.runs[0]["context"]
