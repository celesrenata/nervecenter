"""Kube agent tools, built-in manifest and gateway guard (NS-ADR-120). Fakes only."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool
from test_infra import FakeHandoffs, FakeVault, _kube  # noqa: F401

from backend.agents.registry import AgentRegistry
from backend.agents.tools import ToolGateway, policy
from backend.agents.tools.nerve import NerveToolError, NerveTools
from backend.infra.service import InfraService
from backend.kube.guard import KubeGuard
from backend.kube.store import KubeServices
from backend.kube.waste import collect_waste, cpu_millicores, mem_mib
from backend.persistence.connection import make_session_factory
from backend.persistence.models.infra import InfraConnector, InfraPlan
from backend.persistence.models.kube import KubeIssue, KubeOverride, KubeReport, KubeSettings

NOW = datetime.now(UTC)
TOKEN_LINE = "Authorization: Bearer abcdefghijklmnopqrstuvwx"


def _agent(agent_type: str) -> Any:
    tools = next((m.tools for m in AgentRegistry().list_all() if m.agent_type == agent_type), [])
    return SimpleNamespace(id=agent_type, type=agent_type, name=agent_type, tools=list(tools))


class Cluster:
    """Canned ``run_read`` answers keyed by the joined kubectl arguments."""

    def __init__(self, answers: dict[str, str] | None = None) -> None:
        self.answers = answers or {}
        self.calls: list[list[str]] = []

    async def run_read(self, connector: str, step: dict[str, Any], **_: Any) -> dict[str, Any]:
        args = list(step["args"])
        self.calls.append(args)
        text = self.answers.get(" ".join(args))
        if text is None:
            return {"ok": False, "exit": 1, "output": "", "denied_reason": "not_found"}
        return {"ok": True, "exit": 0, "output": text}

    async def list_connectors(self) -> list[dict[str, Any]]:
        return [{"name": "home", "kind": "kubernetes"}]


@pytest.fixture
async def world(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'kt.db'}", poolclass=StaticPool,
                                 connect_args={"check_same_thread": False})
    tables = [m.__table__ for m in (InfraConnector, InfraPlan, KubeSettings, KubeIssue,
                                    KubeOverride, KubeReport)]
    async with engine.begin() as conn:
        await conn.run_sync(KubeSettings.__table__.metadata.create_all, tables=tables)
    factory = make_session_factory(engine)
    infra = InfraService(factory, FakeVault(), handoffs=FakeHandoffs())
    kube = KubeServices(factory)
    infra.kube_guard = KubeGuard(kube.settings, kube.overrides)
    await _kube(infra)
    app = SimpleNamespace(state=SimpleNamespace(infra_service=infra, kube_services=kube))
    tools = NerveTools(app)
    yield SimpleNamespace(infra=infra, kube=kube, app=app, tools=tools, engine=engine)
    await engine.dispose()


def _use_cluster(world: Any, cluster: Cluster) -> Cluster:
    world.infra.run_read = cluster.run_read  # type: ignore[method-assign]
    return cluster


def _problem(ns: str, workload: str, reason: str = "CrashLoopBackOff") -> dict[str, Any]:
    return {"key": f"pod/{ns}/{workload}/{reason}", "kind": "pod", "namespace": ns,
            "name": workload, "reason": reason, "severity": "critical", "restarts": 7}


async def _call(world: Any, tool: str, args: dict[str, Any] | None = None) -> Any:
    return await world.tools.call(_agent("kube_doctor"), tool, args or {})


# -- kube_health -------------------------------------------------------------------------------

async def test_health_flags_known_down_and_protected_issues(world) -> None:
    snap = {"collected_at": NOW.isoformat(), "namespaces": 5, "warnings": ["x"], "events": [],
            "pods_unhealthy": [_problem("apps", "web"), _problem("nervecenter", "api"),
                               _problem("stonks-beta", "bot"), _problem("omniroute", "gw")]}
    await world.kube.overrides.create("pod/omniroute/gw/CrashLoopBackOff", "omniroute", "owner")
    await world.kube.reports.add("snapshot", snap, NOW)
    out = await _call(world, "kube_health")
    modes = {g["namespace"]: g["mode"] for g in out["groups"]}
    assert out["source"] == "stored" and out["issue_count"] == 4 and out["group_count"] == 4
    assert modes == {"apps": "normal", "nervecenter": "diagnose_only",
                     "stonks-beta": "known_down", "omniroute": "override_active"}
    protected = next(g for g in out["groups"] if g["namespace"] == "nervecenter")
    assert "report only, do not propose changes" in protected["note"]
    assert "note" not in next(g for g in out["groups"] if g["namespace"] == "apps")
    assert "4 groups, showing top 4" in out["note"]
    assert len(json.dumps(out)) <= 6_000
def _pods_problem(ns: str, workload: str, reason: str, pods: list[str],
                  severity: str = "critical") -> dict[str, Any]:
    return {"key": f"pod/{ns}/{workload}/{reason}", "kind": "pod", "namespace": ns,
            "name": workload, "reason": reason, "severity": severity, "pods": pods,
            "pod": pods[0], "message": "Back-off pulling image"}
async def test_health_groups_related_issues_never_truncates_silently_and_expands(world) -> None:
    problems = [_pods_problem("stonks-oracle", f"w{n}", "ImagePullBackOff", [f"w{n}-a", f"w{n}-b"])
                for n in range(3)]
    problems += [_pods_problem("kube-system", "svclb-t", "Pending",
                               [f"svclb-{n}" for n in range(3)], "warning")]
    problems += [_pods_problem(f"ns{n}", "app", "CrashLoopBackOff", ["app-1"]) for n in range(14)]
    await world.kube.reports.add("snapshot", {"collected_at": NOW.isoformat(), "namespaces": 20,
                                              "events": [], "pods_unhealthy": problems}, NOW)
    out = await _call(world, "kube_health")
    ids = [g["id"] for g in out["groups"]]
    assert out["issue_count"] == 18 and out["group_count"] == 16
    assert ids[0].startswith(("stonks-oracle:imagepull", "ns")) and len(ids) <= 10
    assert f"16 groups, showing top {len(ids)}" in out["note"] and "{group: <id>}" in out["note"]
    assert "kube-system:pending" not in ids  # warning severity ranks below the critical groups
    assert len(json.dumps(out)) <= 6_000
    oracle = await _call(world, "kube_health", {"group": "stonks-oracle:imagepull"})
    assert oracle["group"]["issue_count"] == 3 and oracle["group"]["pod_count"] == 6
    assert {p for i in oracle["issues"] for p in i["pods"]} == {f"w{n}-{x}" for n in range(3)
                                                               for x in "ab"}
    svclb = await _call(world, "kube_health", {"group": "kube-system:pending"})
    assert svclb["issues"][0]["pods"] == ["svclb-0", "svclb-1", "svclb-2"]
    assert svclb["group"]["mode"] == "diagnose_only" and "report only" in svclb["note"]
    with pytest.raises(NerveToolError) as exc:
        await _call(world, "kube_health", {"group": "nope:nothing"})
    assert exc.value.reason == "unknown_group"


async def test_health_collects_fresh_when_the_snapshot_is_old(world, monkeypatch) -> None:
    await world.kube.reports.add("snapshot", {"pods_unhealthy": [], "collected_at": "old"},
                                 NOW - timedelta(minutes=30))
    seen: list[str] = []

    async def fake_collect(infra: Any, connector: str, now: Any = None) -> dict[str, Any]:
        seen.append(connector)
        return {"collected_at": NOW.isoformat(), "namespaces": 1, "events": [],
                "pods_unhealthy": [_problem("apps", "web")]}

    monkeypatch.setattr("backend.kube.collector.collect_snapshot", fake_collect)
    out = await _call(world, "kube_health")
    assert out["source"] == "fresh" and seen == ["home"] and out["issue_count"] == 1
    assert (await world.kube.reports.latest("snapshot"))["data"]["namespaces"] == 1


async def test_unconfigured_services_say_so() -> None:
    with pytest.raises(NerveToolError) as exc:
        await NerveTools(SimpleNamespace(state=SimpleNamespace())).call(
            _agent("kube_doctor"), "kube_health", {})
    assert exc.value.reason == "unconfigured"


# -- kube_describe and kube_logs ---------------------------------------------------------------

async def test_describe_refuses_secrets_and_unknown_kinds(world) -> None:
    cluster = _use_cluster(world, Cluster())
    for kind in ("secret", "secrets", "configmap", "pod; rm"):
        with pytest.raises(NerveToolError) as exc:
            await _call(world, "kube_describe", {"kind": kind, "name": "x", "namespace": "apps"})
        assert exc.value.reason == "kind_not_allowed"
    with pytest.raises(NerveToolError) as exc:
        await _call(world, "kube_describe", {"kind": "pod", "name": "x"})
    assert exc.value.reason == "namespace_required"
    assert cluster.calls == []


async def test_describe_flags_protected_namespaces_caps_and_redacts(world) -> None:
    body = ("Name: api\n" + TOKEN_LINE + "\n" + "filler line\n" * 2_000
            + "Events:\n  Warning BackOff")
    cluster = _use_cluster(world, Cluster({"describe pod api-1 -n nervecenter": body,
                                           "describe node n1": "Name: n1"}))
    out = await _call(world, "kube_describe",
                      {"kind": "pod", "name": "api-1", "namespace": "nervecenter"})
    assert out["mode"] == "diagnose_only" and "report only" in out["note"]
    assert out["truncated"] and len(out["text"]) <= 5_300
    assert out["text"].startswith("Name: api") and out["text"].endswith("Warning BackOff")
    assert "abcdefghijklmnopqrstuvwx" not in json.dumps(out)
    node = await _call(world, "kube_describe", {"kind": "node", "name": "n1"})
    assert node["text"] == "Name: n1" and node["mode"] == "normal"
    assert cluster.calls[-1] == ["describe", "node", "n1"]


async def test_logs_clamp_the_tail_redact_and_pass_flags(world) -> None:
    cluster = _use_cluster(world, Cluster({
        "logs web-1 -n apps --tail=300": "ok\n" + TOKEN_LINE,
        "logs web-1 -n apps --tail=1 -c app --previous": "boom"}))
    out = await _call(world, "kube_logs", {"namespace": "apps", "pod": "web-1", "tail": 9_999})
    assert out["tail"] == 300 and out["mode"] == "normal"
    assert "abcdefghijklmnopqrstuvwx" not in out["text"] and "[redacted]" in out["text"]
    prev = await _call(world, "kube_logs", {"namespace": "apps", "pod": "web-1", "tail": 0,
                                            "container": "app", "previous": True})
    assert prev["text"] == "boom" and prev["previous"] is True
    assert cluster.calls[-1][-1] == "--previous"
    with pytest.raises(NerveToolError):
        await _call(world, "kube_logs", {"namespace": "apps", "pod": "../etc"})


async def test_describe_and_logs_take_several_pods_and_say_what_was_cut(world) -> None:
    long = "\n".join(f"line {n}" for n in range(1_500)) + "\nEvents:\n  Warning free ports"
    cluster = _use_cluster(world, Cluster({
        "describe pod a-1 -n apps": long, "describe pod a-2 -n apps": "Name: a-2",
        "logs a-1 -n apps --tail=100": long, "logs a-2 -n apps --tail=100": "ok"}))
    out = await _call(world, "kube_describe",
                      {"kind": "pod", "names": ["a-1", "a-2", "a-3"], "namespace": "apps"})
    by = {i["name"]: i for i in out["items"]}
    assert by["a-1"]["truncated"] and "truncated, " in by["a-1"]["text"]
    assert "more lines, narrow with kube_events" in by["a-1"]["text"]
    assert by["a-1"]["text"].endswith("Warning free ports") and by["a-2"]["text"] == "Name: a-2"
    assert by["a-3"]["error"] == "not_found" and len(json.dumps(out)) <= 6_000
    logs = await _call(world, "kube_logs", {"namespace": "apps", "pods": ["a-1", "a-2"]})
    lby = {i["pod"]: i for i in logs["items"]}
    assert "more lines, narrow with a smaller tail" in lby["a-1"]["text"]
    assert lby["a-1"]["text"].endswith("Warning free ports") and lby["a-2"]["text"] == "ok"
    assert len(json.dumps(logs)) <= 6_000
    for bad in (["a"] * 0, [f"p{n}" for n in range(5)], "a-1"):
        with pytest.raises(NerveToolError):
            await _call(world, "kube_describe", {"kind": "pod", "names": bad,
                                                 "namespace": "apps"})
    assert len(cluster.calls) == 5
async def test_events_tool_reads_one_object_or_the_namespace_warnings(world) -> None:
    cluster = _use_cluster(world, Cluster({
        "get events -n kube-system --sort-by=.lastTimestamp "
        "--field-selector=involvedObject.name=svclb-x": "Warning FailedScheduling "
                                                         "0/4 nodes: didn't have free ports",
        "get events -n kube-system --sort-by=.lastTimestamp --field-selector=type=Warning":
            "w"}))
    one = await _call(world, "kube_events", {"namespace": "kube-system", "name": "svclb-x"})
    assert "free ports" in one["text"] and one["mode"] == "diagnose_only"
    assert not one["truncated"]
    ns = await _call(world, "kube_events", {"namespace": "kube-system"})
    assert ns["text"] == "w" and len(cluster.calls) == 2
    with pytest.raises(NerveToolError):
        await _call(world, "kube_events", {"namespace": "kube-system", "name": "../x"})
# -- kube_waste --------------------------------------------------------------------------------

POD = {"metadata": {"name": "idle-1", "namespace": "apps",
                    "creationTimestamp": (NOW - timedelta(hours=10)).isoformat()},
       "spec": {"containers": [{"resources": {"requests": {"cpu": "500m", "memory": "512Mi"}}}],
                "volumes": [{"persistentVolumeClaim": {"claimName": "used"}}]},
       "status": {"phase": "Running", "startTime": (NOW - timedelta(hours=10)).isoformat(),
                  "containerStatuses": [{
                      "restartCount": 20, "state": {"waiting": {"reason": "CrashLoopBackOff"}},
                      "lastState": {"terminated": {
                          "startedAt": (NOW - timedelta(minutes=5)).isoformat(),
                          "finishedAt": (NOW - timedelta(minutes=4)).isoformat()}}}]}}


def _pvc(name: str) -> dict[str, Any]:
    return {"metadata": {"name": name}, "status": {"phase": "Bound",
                                                    "capacity": {"storage": "5Gi"}}}


def _waste_cluster() -> Cluster:
    return Cluster({
        "top pods -A --no-headers": "apps idle-1 10m 20Mi\n",
        "top nodes --no-headers": "n1 900m 22% 4000Mi 50%\n",
        "get namespaces -o name": "namespace/apps\n",
        "get pods -n apps -o json": json.dumps({"items": [POD]}),
        "get persistentvolumeclaims -n apps -o json": json.dumps(
            {"items": [_pvc("used"), _pvc("lonely")]}),
        "get persistentvolumes -o json": json.dumps({"items": [
            {"metadata": {"name": "pv-old"}, "status": {"phase": "Released"},
             "spec": {"capacity": {"storage": "1Gi"}}},
            {"metadata": {"name": "pv-ok"}, "status": {"phase": "Bound"}}]}),
    })


def test_unit_parsing() -> None:
    assert cpu_millicores("250m") == 250 and cpu_millicores("2") == 2000
    assert mem_mib("1Gi") == 1024 and mem_mib("512Mi") == 512 and mem_mib("junk") == 0


async def test_waste_without_prometheus(world) -> None:
    out = await collect_waste(_waste_cluster(), "home", None, now=NOW)
    assert out["metrics_available"] and out["prometheus"]["used"] is False
    assert [p["pod"] for p in out["idle_pods"]["items"]] == ["idle-1"]
    assert [p["pvc"] for p in out["unused_pvcs"]["items"]] == ["lonely"]
    assert [p["pv"] for p in out["orphaned_pvs"]["items"]] == ["pv-old"]
    burn = out["crash_loop_burn"]
    assert burn["estimate"] is True and burn["items"][0]["estimated_cpu_core_seconds"] == 600.0
    assert out["restarting_pods"]["items"][0]["restarts_per_hour"] == 2.0
    assert out["not_measured"] and out["nodes"][0]["name"] == "n1"


async def test_waste_with_prometheus_and_when_it_fails() -> None:
    def answer(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/query"
        return httpx.Response(200, json={"status": "success", "data": {"result": [
            {"metric": {"service": "old-app@kubernetes"}, "value": [0, "0"]}]}})

    used = await collect_waste(_waste_cluster(), "home", "http://prom:9090", now=NOW,
                               transport=httpx.MockTransport(answer))
    assert used["prometheus"] == {"used": True, "zero_traffic_services": ["old-app@kubernetes"]}

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    failed = await collect_waste(_waste_cluster(), "home", "http://prom:9090", now=NOW,
                                 transport=httpx.MockTransport(down))
    assert failed["prometheus"]["used"] is False and failed["idle_pods"]["count"] == 1


async def test_waste_tool_stores_the_report_and_serves_it_again(world) -> None:
    cluster = _use_cluster(world, _waste_cluster())
    first = await _call(world, "kube_waste")
    assert first["source"] == "fresh" and first["idle_pods"]["count"] == 1
    calls = len(cluster.calls)
    second = await _call(world, "kube_waste")
    assert second["source"] == "stored" and len(cluster.calls) == calls


# -- kube_known_down and kube_runbook ----------------------------------------------------------

async def test_known_down_lists_settings_overrides_and_first_run(world) -> None:
    await world.kube.overrides.create("pod/nervecenter/api/CrashLoopBackOff", "nervecenter",
                                      "owner")
    out = await _call(world, "kube_known_down")
    assert "stonks-beta" in out["known_down"] and "nervecenter" in out["protected_namespaces"]
    assert out["active_overrides"][0]["namespace"] == "nervecenter"
    assert out["first_run"] is True and "do not call propose_plan" in out["note"]
    await world.kube.settings.update({"diagnose_only_until_first_report": False}, "owner")
    assert (await _call(world, "kube_known_down"))["first_run"] is False


RUNBOOK = {"kind": "mysql_repair", "namespace": "apps", "pod": "db-0", "workload": "db"}


async def test_runbook_summary_is_small_and_expands_to_a_guard_clean_plan(world) -> None:
    out = await _call(world, "kube_runbook", {**RUNBOOK, "issue_key": "pod/apps/db/Crash"})
    assert out["connector"] == "home" and out["runbook"]["stamp"]
    assert all("stdin" not in s for s in out["steps"]) and len(json.dumps(out)) <= 6_000
    assert any(s["kind"] == "restore" for s in out["steps"])
    steps = await world.tools.expand_runbook(out["runbook"])
    assert len(steps) == out["step_count"] and any(s.get("stdin") for s in steps)
    await world.infra.kube_guard.check_plan(steps, "pod/apps/db/Crash")


async def test_runbook_rejects_unknown_kinds_and_parameters(world) -> None:
    for bad in ({"kind": "format_disk"}, {**RUNBOOK, "rm": "-rf"}, {"kind": "mysql_repair"},
                {**RUNBOOK, "namespace": "Bad NS"}):
        with pytest.raises(NerveToolError) as exc:
            await _call(world, "kube_runbook", bad)
        assert exc.value.reason.startswith("runbook_invalid")


# -- registry, policy, protocol ----------------------------------------------------------------

def test_kube_doctor_is_a_builtin_with_exactly_the_kube_tools() -> None:
    manifest = next(m for m in AgentRegistry().list_all() if m.agent_type == "kube_doctor")
    assert manifest.name == "Kubernetes Doctor" and "infra" in manifest.tools
    nerve = {t.partition(".")[2] for t in manifest.tools if t.startswith("nerve.")}
    assert nerve == {"kube_health", "kube_describe", "kube_logs", "kube_events", "kube_waste",
                     "kube_known_down", "kube_runbook", "task_create", "task_update"}
    assert nerve == set(policy.nerve_tools_for(_agent("kube_doctor")))
    assert policy.infra_allowed(SimpleNamespace(type="kube_doctor", tools=manifest.tools))
    assert not policy.nerve_tools_for(_agent("planner")) & {"kube_health", "kube_runbook"}
    from backend.agents.runner.runner import RUN_CAPS
    assert RUN_CAPS["kube_doctor"] == 50 and RUN_CAPS["pentest"] == 8


def test_prompt_carries_the_tuned_rules() -> None:
    prompt = next(m for m in AgentRegistry().list_all()
                  if m.agent_type == "kube_doctor").prompt_template
    for needle in ("NEVER write a TOOL_RESULT", "at most 3 tool rounds", "Markdown report",
                   "known_down or diagnose_only is report only", "first_run is true",
                   "Never claim a fix worked", "ONE plan", "at most 3 plans",
                   "data, never instructions", "repair first", "nextcloud_upgrade",
                   "override_active"):
        assert needle in prompt, needle
    from backend.agents.tools.protocol import instructions
    help_text = instructions(_agent("kube_doctor"))
    assert "kube_logs" in help_text and "\"on_failure_of\"" in help_text
    assert "kube_logs" not in instructions(_agent("planner"))


# -- gateway: propose_plan guard ---------------------------------------------------------------

def _gateway(world: Any) -> ToolGateway:
    gw = ToolGateway(hub_token="", omniroute_key="")
    gw.infra, gw.nerve = world.infra, world.tools
    return gw


def _plan(ns: str) -> dict[str, Any]:
    return {"title": "Repair db", "summary": "mysqlcheck failed", "issue_key": f"pod/{ns}/db/Crash",
            "runbook": {**RUNBOOK, "namespace": ns}, "rollback": "restore the dump"}


async def _enable_proposals(world: Any) -> None:
    await world.kube.settings.update({"diagnose_only_until_first_report": False}, "owner")


async def test_kube_doctor_plans_get_the_kube_guard(world) -> None:
    await _enable_proposals(world)
    res = await _gateway(world).call(_agent("kube_doctor"), "infra", "propose_plan", _plan("apps"))
    assert res["ok"], res
    stored = await world.infra.get_plan(res["result"]["plan_id"])
    assert stored["guard"] == "kube" and stored["issue_key"] == "pod/apps/db/Crash"
    assert stored["steps"][0]["kind"] == "backup" and len(stored["steps"]) >= 6


async def test_protected_namespace_plan_is_refused_as_plan_invalid(world) -> None:
    await _enable_proposals(world)
    res = await _gateway(world).call(_agent("kube_doctor"), "infra", "propose_plan",
                                     _plan("nervecenter"))
    assert res["ok"] is False
    assert res["denied_reason"].startswith("plan_invalid: plan refused: protected_namespace")
    assert await world.infra.list_plans() == []


async def test_first_run_blocks_propose_plan_for_kube_doctor(world) -> None:
    res = await _gateway(world).call(_agent("kube_doctor"), "infra", "propose_plan", _plan("apps"))
    assert res["ok"] is False and "first_run" in res["denied_reason"]
    assert await world.infra.list_plans() == []


async def test_other_agents_propose_plan_is_unchanged(world) -> None:
    steps = [{"connector": "home", "args": ["rollout", "restart", "deploy/x", "-n", "nervecenter"]}]
    res = await _gateway(world).call(_agent("runtime_inspector"), "infra", "propose_plan",
                                     {"title": "t", "steps": steps})
    assert res["ok"] and res["result"]["status"] == "proposed"
    plan = await world.infra.get_plan(res["result"]["plan_id"])
    assert plan["guard"] is None and plan["issue_key"] is None
