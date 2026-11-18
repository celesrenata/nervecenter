"""Kube snapshot collector: classification, keys, missing CRDs, oversized pages (fakes only)."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from backend.kube.collector import (
    collect_snapshot,
    snapshot_problems,
    workload_of,
)

NOW = datetime(2030, 1, 1, 12, 0, tzinfo=UTC)
OLD = (NOW - timedelta(hours=3)).isoformat().replace("+00:00", "Z")


class FakeInfra:
    """run_read answers from a table of kubectl args; unknown commands fail like a CRD miss."""

    def __init__(self, table: dict[tuple[str, ...], str]) -> None:
        self.table = table
        self.calls: list[tuple[str, ...]] = []

    async def run_read(self, connector: str, step: dict[str, Any], **_: Any) -> dict[str, Any]:
        args = tuple(step["args"])
        self.calls.append(args)
        if args in self.table:
            return {"ok": True, "exit": 0, "output": self.table[args]}
        return {"ok": False, "denied_reason": "not_found"}


def _list(*items: dict[str, Any]) -> str:
    return json.dumps({"items": list(items)})


def _pod(name: str, ns: str, rs: str | None, status: dict[str, Any]) -> dict[str, Any]:
    meta: dict[str, Any] = {"name": name, "namespace": ns, "creationTimestamp": OLD}
    if rs:
        meta["ownerReferences"] = [{"kind": "ReplicaSet", "name": rs, "controller": True}]
    return {"metadata": meta, "status": status}


def _waiting(reason: str, restarts: int = 0, exit_code: int | None = None) -> dict[str, Any]:
    cs: dict[str, Any] = {"name": "app", "restartCount": restarts,
                          "state": {"waiting": {"reason": reason, "message": "boom"}}}
    if exit_code is not None:
        cs["lastState"] = {"terminated": {"reason": "Error", "exitCode": exit_code,
                                          "finishedAt": OLD}}
    return {"phase": "Running", "containerStatuses": [cs]}


def _table() -> dict[tuple[str, ...], str]:
    g = ("get",)
    return {
        (*g, "namespaces", "-o", "name"): "namespace/app\nnamespace/data\n",
        (*g, "pods", "-n", "app", "-o", "json"): _list(
            _pod("web-7d9c8b6f5d-abcde", "app", "web-7d9c8b6f5d",
                 _waiting("CrashLoopBackOff", 9, 1)),
            _pod("web-7d9c8b6f5d-fghij", "app", "web-7d9c8b6f5d",
                 _waiting("CrashLoopBackOff", 4, 1)),
            _pod("img-5c6d7f8b9c-zzzzz", "app", "img-5c6d7f8b9c", _waiting("ImagePullBackOff")),
            _pod("ok-5c6d7f8b9c-qqqqq", "app", "ok-5c6d7f8b9c",
                 {"phase": "Running", "containerStatuses": [
                     {"name": "a", "restartCount": 0, "state": {"running": {}}}]}),
        ),
        (*g, "pods", "-n", "data", "-o", "json"): _list(
            _pod("pend-0", "data", None, {"phase": "Pending", "conditions": [
                {"type": "PodScheduled", "status": "False",
                 "message": "0/4 nodes are available: persistentvolumeclaim unbound"}]})),
        (*g, "persistentvolumeclaims", "-n", "app", "-o", "json"): _list(),
        (*g, "persistentvolumeclaims", "-n", "data", "-o", "json"): _list(
            {"metadata": {"name": "dbdata", "namespace": "data"},
             "spec": {"storageClassName": "longhorn"}, "status": {"phase": "Pending"}}),
        (*g, "events", "-n", "app", "-o", "json"): _list(
            {"type": "Warning", "reason": "BackOff", "message": "Back-off restarting",
             "count": 3, "lastTimestamp": (NOW - timedelta(minutes=5)).isoformat(),
             "involvedObject": {"kind": "Pod", "name": "web-1", "namespace": "app"}},
            {"type": "Warning", "reason": "BackOff", "message": "Back-off restarting",
             "count": 2, "lastTimestamp": (NOW - timedelta(minutes=2)).isoformat(),
             "involvedObject": {"kind": "Pod", "name": "web-1", "namespace": "app"}},
            {"type": "Warning", "reason": "Old", "message": "m", "count": 1,
             "lastTimestamp": (NOW - timedelta(hours=5)).isoformat(),
             "involvedObject": {"kind": "Pod", "name": "x", "namespace": "app"}},
            {"type": "Normal", "reason": "Pulled", "lastTimestamp": NOW.isoformat(),
             "involvedObject": {"kind": "Pod", "name": "x", "namespace": "app"}}),
        (*g, "events", "-n", "data", "-o", "json"): _list(),
        (*g, "persistentvolumes", "-o", "json"): _list(
            {"metadata": {"name": "pv-gone"}, "status": {"phase": "Failed", "message": "x"},
             "spec": {"claimRef": {"namespace": "data", "name": "dbdata"}}},
            {"metadata": {"name": "pv-ok"}, "status": {"phase": "Bound"}}),
        (*g, "nodes", "-o", "json"): _list(
            {"metadata": {"name": "gremlin-2"}, "status": {"conditions": [
                {"type": "Ready", "status": "True"},
                {"type": "DiskPressure", "status": "True", "message": "low disk"}]}}),
        (*g, "crd", "volumes.longhorn.io", "-o", "name"): "crd/volumes.longhorn.io",
        (*g, "replicas.longhorn.io", "-n", "longhorn-system", "-o", "json"): _list(
            {"metadata": {"name": "r1"}, "spec": {"volumeName": "vol-a", "nodeID": "n1"},
             "status": {"currentState": "running"}},
            {"metadata": {"name": "r2"}, "spec": {"volumeName": "vol-a", "nodeID": "n2",
                                                 "failedAt": "2030-01-01T10:00:00Z"},
             "status": {"currentState": "error"}}),
        (*g, "volumes.longhorn.io", "-n", "longhorn-system", "-o", "json"): _list(
            {"metadata": {"name": "vol-a"}, "status": {"robustness": "degraded",
                                                       "state": "attached"}},
            {"metadata": {"name": "vol-b"}, "status": {"robustness": "healthy",
                                                       "state": "attached"}}),
        (*g, "crd", "certificates.cert-manager.io", "-o", "name"): "crd/certificates",
        (*g, "certificates.cert-manager.io", "-n", "app", "-o", "json"): _list(
            {"metadata": {"name": "soon", "namespace": "app"},
             "status": {"notAfter": (NOW + timedelta(days=3)).isoformat()}},
            {"metadata": {"name": "later", "namespace": "app"},
             "status": {"notAfter": (NOW + timedelta(days=60)).isoformat()}},
            {"metadata": {"name": "gone", "namespace": "app"},
             "status": {"notAfter": (NOW - timedelta(days=1)).isoformat()}}),
        (*g, "certificates.cert-manager.io", "-n", "data", "-o", "json"): _list(),
    }


async def test_snapshot_classifies_every_problem_kind() -> None:
    snap = await collect_snapshot(FakeInfra(_table()), "home", NOW)
    by_key = {p["key"]: p for p in snapshot_problems(snap)}
    assert set(by_key) == {
        "pod/app/web/CrashLoopBackOff", "pod/app/img/ImagePullBackOff", "pod/data/pend-0/Pending",
        "pvc/data/dbdata/Pending", "pv/_cluster/pv-gone/Failed",
        "node/_cluster/gremlin-2/DiskPressure", "volume/longhorn-system/vol-a/degraded",
        "cert/app/soon/Expiring", "cert/app/gone/Expired"}
    crash = by_key["pod/app/web/CrashLoopBackOff"]
    assert crash["pods"] == ["web-7d9c8b6f5d-abcde", "web-7d9c8b6f5d-fghij"]
    assert crash["restarts"] == 9 and crash["last_termination"]["exit_code"] == 1
    assert "unbound" in by_key["pod/data/pend-0/Pending"]["scheduler_message"]
    assert snap["pending_pods"][0]["key"] == "pod/data/pend-0/Pending"
    vol = by_key["volume/longhorn-system/vol-a/degraded"]
    assert [r["node"] for r in vol["replicas"]] == ["n1", "n2"]
    assert vol["replicas"][1]["failed_at"]
    assert snap["warnings"] == []


async def test_events_are_recent_warnings_deduped() -> None:
    snap = await collect_snapshot(FakeInfra(_table()), "home", NOW)
    assert len(snap["events"]) == 1
    ev = snap["events"][0]
    assert ev["reason"] == "BackOff" and ev["count"] == 5


async def test_missing_crds_are_skipped_with_a_warning() -> None:
    table = {k: v for k, v in _table().items() if "crd" not in k}
    snap = await collect_snapshot(FakeInfra(table), "home", NOW)
    assert snap["longhorn_volumes"] == [] and snap["certificates"] == []
    assert any("Longhorn" in w for w in snap["warnings"])
    assert any("certificates" in w for w in snap["warnings"])
    assert snap["pods_unhealthy"]  # the rest of the snapshot is intact


async def test_oversized_page_is_read_object_by_object() -> None:
    table = _table()
    big = "x" * 21_000
    table[("get", "pods", "-n", "app", "-o", "json")] = big
    table[("get", "pods", "-n", "app", "-o", "name")] = "pod/img-5c6d7f8b9c-zzzzz"
    table[("get", "pod/img-5c6d7f8b9c-zzzzz", "-n", "app", "-o", "json")] = json.dumps(
        _pod("img-5c6d7f8b9c-zzzzz", "app", "img-5c6d7f8b9c", _waiting("ImagePullBackOff")))
    snap = await collect_snapshot(FakeInfra(table), "home", NOW)
    keys = {p["key"] for p in snapshot_problems(snap)}
    assert "pod/app/img/ImagePullBackOff" in keys
    assert "pod/app/web/CrashLoopBackOff" not in keys  # not listed by name: not invented


async def test_unreadable_namespace_pods_are_a_warning_not_silence() -> None:
    table = _table()
    del table[("get", "pods", "-n", "data", "-o", "json")]
    snap = await collect_snapshot(FakeInfra(table), "home", NOW)
    assert any("pods in namespace data" in w for w in snap["warnings"])


async def test_only_read_verbs_and_never_secrets() -> None:
    infra = FakeInfra(_table())
    await collect_snapshot(infra, "home", NOW)
    assert infra.calls
    assert {c[0] for c in infra.calls} == {"get"}
    assert not any("secret" in a.lower() for c in infra.calls for a in c)


def test_workload_name_is_stable_across_pod_names() -> None:
    a = _pod("web-7d9c8b6f5d-abcde", "n", "web-7d9c8b6f5d", {})
    b = _pod("web-5555555555-zzzzz", "n", "web-5555555555", {})
    assert workload_of(a) == workload_of(b) == "web"
    sts = {"metadata": {"name": "db-0", "ownerReferences": [
        {"kind": "StatefulSet", "name": "db", "controller": True}]}}
    assert workload_of(sts) == "db"
    job = {"metadata": {"name": "bk-28391234-x7k2p", "ownerReferences": [
        {"kind": "Job", "name": "bk-28391234", "controller": True}]}}
    assert workload_of(job) == "bk"
    assert workload_of(_pod("lone-7d9c8b6f5d-abcde", "n", None, {})) == "lone"
async def test_coverage_counts_unreadable_namespaces_and_raises_a_blind_check_issue() -> None:
    table = _table()
    del table[("get", "pods", "-n", "data", "-o", "json")]
    snap = await collect_snapshot(FakeInfra(table), "home", NOW)
    cov = snap["coverage"]
    assert (cov["read"], cov["total"], cov["unreadable"]) == (1, 2, ["data"])
    assert cov["line"].startswith("read 1 of 2 namespaces, 1 unreadable (data)")
    blind = [p for p in snapshot_problems(snap) if p["kind"] == "coverage"]
    assert [p["key"] for p in blind] == ["coverage/_cluster/cluster/Unreadable"]
    assert "could not be read" in blind[0]["message"] and "50%" in blind[0]["message"]


async def test_full_coverage_raises_no_issue() -> None:
    snap = await collect_snapshot(FakeInfra(_table()), "home", NOW)
    assert snap["coverage"]["percent"] == 100 and snap["coverage"]["unreadable"] == []
    assert not [p for p in snapshot_problems(snap) if p["kind"] == "coverage"]


async def test_unreadable_nodes_are_listed_as_not_read() -> None:
    table = {k: v for k, v in _table().items() if k[:2] != ("get", "nodes")}
    snap = await collect_snapshot(FakeInfra(table), "home", NOW)
    assert "nodes" in snap["coverage"]["cluster_unreadable"]
    assert "nodes" in snap["coverage"]["line"]


async def test_namespaces_beyond_the_read_cap_count_as_unread(monkeypatch) -> None:
    from backend.kube import collector
    monkeypatch.setattr(collector, "MAX_NAMESPACES", 1)
    snap = await collect_snapshot(FakeInfra(_table()), "home", NOW)
    assert snap["coverage"]["total"] == 2 and snap["coverage"]["read"] == 1
    assert snap["coverage"]["over_cap"] == 1


def _restarting(restarts: int, exit_code: int, reason: str, state: dict[str, Any] | None = None
                ) -> dict[str, Any]:
    return {"phase": "Running", "startTime": OLD, "containerStatuses": [{
        "name": "worker", "restartCount": restarts, "state": state or {"running": {}},
        "lastState": {"terminated": {"reason": reason, "exitCode": exit_code,
                                     "startedAt": OLD, "finishedAt": NOW.isoformat()}}}]}


def _classify(status: dict[str, Any]) -> dict[str, Any] | None:
    from backend.kube.collector import classify_pod
    return classify_pod(_pod("w-1", "nc", None, status), NOW)


def test_clean_exit_worker_is_not_an_issue_but_real_loops_are() -> None:
    assert _classify(_restarting(2_295, 0, "Completed")) is None
    exit_one = _classify(_restarting(40, 1, "Error"))
    assert exit_one and exit_one["reason"] == "HighRestarts" and exit_one["severity"] == "warning"
    oom = _classify(_restarting(12, 137, "OOMKilled"))
    assert oom and oom["reason"] == "HighRestarts"
    assert oom["last_termination"]["reason"] == "OOMKilled"
    loop = _classify(_restarting(9, 0, "Completed", {"waiting": {"reason": "CrashLoopBackOff"}}))
    assert loop and loop["reason"] == "CrashLoopBackOff"  # still a real crash loop


async def test_clean_exit_worker_shows_as_churn_in_the_waste_report() -> None:
    from backend.kube.waste import collect_waste
    worker = _pod("worker-1", "nc", None, _restarting(2_295, 0, "Completed"))
    worker["status"]["startTime"] = (NOW - timedelta(days=7)).isoformat()
    bad = _pod("bad-1", "nc", None, _restarting(40, 1, "Error"))
    table = {("top", "pods", "-A", "--no-headers"): "", ("top", "nodes", "--no-headers"): "",
             ("get", "namespaces", "-o", "name"): "namespace/nc\n",
             ("get", "pods", "-n", "nc", "-o", "json"): _list(worker, bad),
             ("get", "persistentvolumeclaims", "-n", "nc", "-o", "json"): _list(),
             ("get", "persistentvolumes", "-o", "json"): _list()}
    report = await collect_waste(FakeInfra(table), "home", None, now=NOW)
    items = report["clean_exit_churn"]["items"]
    assert report["clean_exit_churn"]["count"] == 1 and items[0]["pod"] == "worker-1"
    assert items[0]["restart_every_seconds"] == round(7 * 86400 / 2_295)
    assert items[0]["restarts_per_day"] == round(2_295 / 7)
    assert "clean exit" in items[0]["suggestion"] and "CronJob" in items[0]["suggestion"]
    assert [r["pod"] for r in report["restarting_pods"]["items"]] == ["bad-1"]


async def test_old_clean_exit_issue_closes_on_the_next_check() -> None:
    snap = await collect_snapshot(FakeInfra(_table_with_worker()), "home", NOW)
    assert not [p for p in snapshot_problems(snap) if "worker" in p["key"]]


def _table_with_worker() -> dict[tuple[str, ...], str]:
    table = _table()
    worker = _pod("worker-1", "app", None, _restarting(2_295, 0, "Completed"))
    table[("get", "pods", "-n", "app", "-o", "json")] = _list(worker)
    return table
