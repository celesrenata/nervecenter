"""Kube guard (brief feature F): protected/known-down namespaces, overrides, Secrets, backup
chain, restore path and the exec allowlist. Fakes only; nothing reaches a cluster."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool
from test_infra import _kube, svc  # noqa: F401 - svc is a fixture

from backend.infra.policy import PolicyError, check_kubectl, exec_is_mutating
from backend.infra.service import InfraInputError
from backend.kube.guard import GuardError, KubeGuard
from backend.kube.runbooks import backup_steps, mysql_repair_steps
from backend.kube.settings import KubeOverrideStore, KubeSettingsStore
from backend.persistence.connection import make_session_factory
from backend.persistence.models.kube import KubeIssue, KubeOverride, KubeReport, KubeSettings

KEY = "pod/nervecenter/api/CrashLoopBackOff"
SETTINGS = {"backup_nfs_server": "192.168.42.8", "backup_nfs_base": "/volume1/Kubernetes",
            "retention_daily": 7, "retention_weekly": 4}


@pytest.fixture
async def kube(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'g.db'}", poolclass=StaticPool,
                                 connect_args={"check_same_thread": False})
    tables = [m.__table__ for m in (KubeSettings, KubeIssue, KubeOverride, KubeReport)]
    async with engine.begin() as conn:
        await conn.run_sync(KubeSettings.__table__.metadata.create_all, tables=tables)
    factory = make_session_factory(engine)
    settings = KubeSettingsStore(factory)
    overrides = KubeOverrideStore(factory, settings)
    guard = KubeGuard(settings, overrides)
    guard.overrides = overrides  # type: ignore[attr-defined]
    guard.factory = factory  # type: ignore[attr-defined]
    yield guard
    await engine.dispose()


def _restart(ns: str | None, name: str = "x") -> dict[str, Any]:
    args = ["rollout", "restart", f"deploy/{name}"] + (["-n", ns] if ns else [])
    return {"connector": "home", "args": args, "kind": "change"}


async def _refused(guard: KubeGuard, steps: list[dict[str, Any]], code: str,
                   key: Any = KEY) -> None:
    with pytest.raises(GuardError) as err:
        await guard.check_plan(steps, key)
    assert err.value.code == code


# -- namespaces -----------------------------------------------------------------------------
async def test_protected_namespace_needs_an_active_override_for_that_issue(kube) -> None:
    step = _restart("nervecenter")
    await _refused(kube, [step], "protected_namespace_no_override")
    await kube.overrides.create("some/other/issue", "nervecenter", "owner")
    await _refused(kube, [step], "protected_namespace_no_override")  # other issue's override
    await kube.overrides.create(KEY, "omniroute", "owner")
    await _refused(kube, [step], "protected_namespace_no_override")  # other namespace
    await kube.overrides.create(KEY, "nervecenter", "owner")
    await kube.check_plan([step], KEY)
    await kube.check_plan([_restart("apps")], "any/key")  # normal namespaces need nothing


async def test_expired_and_revoked_overrides_do_not_count(kube) -> None:
    step = _restart("nervecenter")
    old = datetime.now(UTC) - timedelta(hours=25)
    await kube.overrides.create(KEY, "nervecenter", "owner", now=old)  # 24 h: expired an hour ago
    await _refused(kube, [step], "protected_namespace_no_override")
    live = await kube.overrides.create(KEY, "nervecenter", "owner")
    await kube.check_plan([step], KEY)
    await kube.overrides.revoke(live["id"])
    await _refused(kube, [step], "protected_namespace_no_override")


async def test_known_down_is_refused_even_with_an_override(kube) -> None:
    await kube.overrides.create(KEY, "stonks-beta", "owner")
    await _refused(kube, [_restart("stonks-beta")], "known_down")
    await _refused(kube, [_restart("comfyui-service")], "known_down")


async def test_changes_must_name_one_explicit_namespace(kube) -> None:
    await _refused(kube, [_restart(None)], "namespace_required")
    await _refused(kube, [{"connector": "h", "args": ["delete", "pod", "x", "-A"]}],
                   "all_namespaces_forbidden")
    for args in (["delete", "pv", "pvc-1"], ["delete", "namespace", "apps", "-n", "apps"],
                 ["delete", "clusterrole/x", "-n", "apps"], ["delete", "node", "gremlin-1"]):
        await _refused(kube, [{"connector": "h", "args": args}], "cluster_scope_forbidden")
    manifest = "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: c\n"
    await _refused(kube, [{"connector": "h", "args": ["apply", "-f", "-"], "stdin": manifest}],
                   "namespace_required")
    await _refused(kube, [{"connector": "h", "args": ["apply", "-f", "-"],
                           "stdin": "kind: Namespace\nmetadata:\n  name: z\n"}],
                   "cluster_scope_forbidden")
    await _refused(kube, [{"connector": "h", "args": ["apply", "-f", "-"], "stdin": ": [bad"}],
                   "bad_manifest")


async def test_namespaces_inside_stdin_manifests_are_checked(kube) -> None:
    docs = ("apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: a\n  namespace: apps\n---\n"
            "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: b\n  namespace: kube-system\n")
    step = {"connector": "h", "args": ["apply", "-n", "apps", "-f", "-"], "stdin": docs}
    await _refused(kube, [step], "protected_namespace_no_override")
    await kube.overrides.create(KEY, "kube-system", "owner")
    await kube.check_plan([step], KEY)
    ok = {"connector": "h", "args": ["apply", "-f", "-"], "stdin": docs.split("---")[0]}
    await kube.check_plan([ok], KEY)  # the manifest's own namespace is enough


async def test_reads_in_protected_namespaces_stay_possible(kube) -> None:
    await kube.check_plan([{"connector": "h", "args": ["get", "pods", "-n", "kube-system"],
                            "kind": "read"}], KEY)


# -- secrets --------------------------------------------------------------------------------
async def test_secrets_are_never_touched(kube) -> None:
    for args in (["get", "secrets", "-n", "apps"], ["get", "secret/db", "-n", "apps"],
                 ["delete", "secret", "db", "-n", "apps"], ["get", "pods,secrets", "-n", "apps"],
                 ["describe", "secrets", "-n", "apps"]):
        await _refused(kube, [{"connector": "h", "args": args}], "secrets_forbidden")
    await _refused(kube, [{"connector": "h", "args": ["apply", "-n", "apps", "-f", "-"],
                           "stdin": "kind: Secret\nmetadata:\n  name: s\n  namespace: apps\n"}],
                   "secrets_forbidden")
    with pytest.raises(PolicyError, match="secrets_forbidden"):  # read mode, existing policy
        check_kubectl(["get", "secrets", "-A"])


# -- backup chain and restore path ----------------------------------------------------------
def _repair(requires: list[int] | None = None) -> dict[str, Any]:
    step: dict[str, Any] = {
        "connector": "h", "kind": "repair",
        "args": ["exec", "-n", "apps", "db-0", "--", "mysqlcheck", "--auto-repair", "--all"]}
    if requires is not None:
        step["requires"] = requires
    return step


async def test_data_steps_need_a_verified_backup_chain(kube) -> None:
    await _refused(kube, [_repair()], "backup_chain_missing")
    plain = {"connector": "h", "args": ["get", "pods", "-n", "apps"], "kind": "read"}
    verify_only = {"connector": "h", "kind": "backup_verify", "requires": [0],
                   "args": ["wait", "--for=condition=complete", "job/v", "-n", "apps"]}
    await _refused(kube, [plain, verify_only, _repair([1])], "backup_chain_missing")
    # nextcloud upgrade and a delete of a PVC are data steps too
    upgrade = {"connector": "h", "kind": "change", "args": [
        "exec", "-n", "apps", "nc-0", "--", "php", "occ", "upgrade"]}
    await _refused(kube, [upgrade], "backup_chain_missing")
    await _refused(kube, [{"connector": "h", "args": ["delete", "pvc", "d", "-n", "apps"]}],
                   "backup_chain_missing")
    await _refused(kube, [{"connector": "h", "kind": "restore",
                           "args": ["delete", "pod", "x", "-n", "apps"]}], "backup_chain_missing")


async def test_repair_needs_a_restore_fallback_and_restore_path_is_allowed(kube) -> None:
    backup = backup_steps("mysql", "apps", "db", SETTINGS, connector="home")
    verified = len(backup) - 1
    repair = _repair([verified])
    await _refused(kube, [*backup, repair], "restore_path_missing")
    no_failure_link = {"connector": "h", "kind": "restore", "requires": [verified],
                       "args": ["delete", "pod", "x", "-n", "apps"]}
    await _refused(kube, [*backup, repair, no_failure_link], "restore_path_missing")
    unverified = {**no_failure_link, "requires": [0], "on_failure_of": [len(backup)]}
    await _refused(kube, [*backup, repair, unverified], "backup_chain_missing")
    restore = {**no_failure_link, "on_failure_of": [len(backup)]}
    await kube.check_plan([*backup, repair, restore], KEY)  # restore path allowed
    await kube.check_plan(mysql_repair_steps("apps", "db-0", "db", SETTINGS, connector="home"),
                          KEY)


async def test_plan_needs_an_issue_key(kube) -> None:
    for key in (None, "", 5):
        await _refused(kube, [_restart("apps")], "issue_key_required", key)


async def test_ssh_steps_only_create_backup_directories(kube) -> None:
    ok = {"connector": "nas", "command": "mkdir -p /volume1/Kubernetes/mysql/backups"}
    await kube.check_plan([ok], KEY)
    for cmd in ("rm -rf /volume1", "mkdir -p /etc/x", "mkdir -p /volume1/Kubernetes/x/backups; id"):
        await _refused(kube, [{"connector": "nas", "command": cmd}], "ssh_not_allowed")


# -- exec allowlist -------------------------------------------------------------------------
def _exec(*argv: str, ns: str | None = "apps", pod: str = "nc-0") -> list[str]:
    head = ["exec"] + (["-n", ns] if ns else []) + [pod, "--"]
    return [*head, *argv]


def test_exec_is_limited_to_the_owners_commands() -> None:
    allowed = [
        _exec("php", "occ", "upgrade"),
        _exec("runuser", "-u", "www-data", "--", "php", "occ", "upgrade"),
        _exec("php", "/var/www/html/occ", "db:add-missing-indices"),
        _exec("occ", "db:convert-filecache-bigint", "--no-interaction"),
        _exec("mysqlcheck", "--check", "--all-databases"),
        _exec("redis-check-aof", "--fix", "/data/appendonly.aof"),
        _exec("redis-check-rdb", "/data/dump.rdb"),
        ["exec", "--namespace=apps", "-c", "app", "nc-0", "--", "mysqlcheck", "-A"],
    ]
    for args in allowed:
        assert check_kubectl(args, act=True, kube_guard=True) == args
    refused = [
        _exec("sh"), _exec("bash", "-c", "id"), _exec("cat", "/etc/passwd"),
        _exec("php", "occ", "maintenance:mode", "--on"), _exec("occ", "status"),
        _exec("php", "-r", "echo 1;"), _exec("php", "occ"), _exec("mysqlcheck_x"),
        _exec("runuser", "-u", "root", "--", "php", "occ", "upgrade"),
        ["exec", "-n", "apps", "deploy/x", "--", "php", "occ", "upgrade"],
        _exec("php", "occ", "upgrade", ns=None),
        ["exec", "-n", "apps", "a", "b", "--", "mysqlcheck"],
        ["exec", "-n", "apps", "nc-0", "mysqlcheck"],
        ["exec", "-n", "apps", "nc-0", "--stdin", "--", "mysqlcheck"],
        ["exec", "-n", "apps", "--", "mysqlcheck"],
        ["exec", "-n", "apps", "nc-0", "--"],
    ]
    for args in refused:
        with pytest.raises(PolicyError) as err:
            check_kubectl(args, act=True, kube_guard=True)
        assert err.value.code in ("exec_not_allowed", "flag_forbidden"), args
    # other agents keep today's behaviour
    assert check_kubectl(_exec("sh"), act=True) == _exec("sh")
    with pytest.raises(PolicyError, match="not_read_only"):
        check_kubectl(_exec("mysqlcheck"), kube_guard=True)  # plan mode never execs


def test_exec_is_mutating() -> None:
    mutating = [
        _exec("php", "occ", "upgrade"), _exec("occ", "db:add-missing-columns"),
        _exec("runuser", "-u", "www-data", "--", "php", "occ", "db:convert-filecache-bigint"),
        _exec("mysqlcheck", "--auto-repair", "--all-databases"),
        _exec("mysqlcheck", "--repair", "d"), _exec("mysqlcheck", "-r", "d"),
        _exec("mysqlcheck", "--optimize", "d"), _exec("mysqlcheck", "-o", "d"),
        _exec("redis-check-aof", "--fix", "/data/a"),
    ]
    reads = [
        _exec("mysqlcheck", "--check", "--all-databases"), _exec("mysqlcheck", "-c", "d"),
        _exec("redis-check-aof", "/data/a"), _exec("redis-check-rdb", "/data/d.rdb"),
        ["get", "pods", "-n", "apps"],
    ]
    assert all(exec_is_mutating(a) for a in mutating)
    assert not any(exec_is_mutating(a) for a in reads)


async def test_exec_in_protected_or_known_down_namespaces(kube) -> None:
    step = {"connector": "h", "args": _exec("mysqlcheck", "--check", ns="nervecenter"),
            "kind": "read"}
    await _refused(kube, [step], "protected_namespace_no_override")
    await kube.overrides.create(KEY, "nervecenter", "owner")
    await kube.check_plan([step], KEY)
    await kube.overrides.create(KEY, "stonks-beta", "owner")
    await _refused(kube, [{**step, "args": _exec("mysqlcheck", "--check", ns="stonks-beta")}],
                   "known_down")


# -- wired into InfraService ----------------------------------------------------------------
async def _guarded(svc, kube) -> None:
    await _kube(svc)
    svc.kube_guard = kube


async def test_service_refuses_guarded_plans_at_propose_time(svc, kube) -> None:
    await _guarded(svc, kube)
    body = {"title": "t", "issue_key": KEY, "steps": [
        {"connector": "home", "args": ["rollout", "restart", "deploy/api", "-n", "nervecenter"]}]}
    with pytest.raises(InfraInputError, match="protected_namespace_no_override"):
        await svc.propose("a", body, guard="kube")
    with pytest.raises(InfraInputError, match="issue_key"):
        await svc.propose("a", {**body, "issue_key": ""}, guard="kube")
    with pytest.raises(InfraInputError, match="exec_not_allowed"):
        await svc.propose("a", {**body, "steps": [{"connector": "home", "args": _exec("sh")}]},
                          guard="kube")
    # the same plan from another agent (no guard) is untouched, exec included
    plain = await svc.propose("a", {**body, "steps": [
        {"connector": "home", "args": _exec("sh")}]})
    assert plain["guard"] is None and plain["issue_key"] is None


async def test_run_step_refuses_after_the_override_expires(svc, kube) -> None:
    await _guarded(svc, kube)
    ov = await kube.overrides.create(KEY, "nervecenter", "owner")
    plan = await svc.propose("a", {"title": "t", "issue_key": KEY, "steps": [
        {"connector": "home", "args": ["rollout", "restart", "deploy/api", "-n", "nervecenter"]},
        {"connector": "home", "args": ["rollout", "status", "deploy/api", "-n", "nervecenter"]}]},
        guard="kube")
    assert plan["guard"] == "kube" and plan["issue_key"] == KEY
    await svc.decide(plan["id"], approve=True, actor="owner", act_mode="opencode")
    async with kube.factory() as s:
        await s.execute(update(KubeOverride).where(KubeOverride.id == ov["id"]).values(
            expires_at=datetime.now(UTC) - timedelta(minutes=1)))
        await s.commit()
    res = await svc.run_step(plan["id"], 0)
    assert res["ok"] is False and "[refused: protected_namespace_no_override]" in res["output"]
    assert (await svc.get_plan(plan["id"]))["status"] == "failed"


async def test_run_step_runs_a_guarded_plan_with_an_active_override(svc, kube) -> None:
    await _guarded(svc, kube)
    await kube.overrides.create(KEY, "nervecenter", "owner")
    plan = await svc.propose("a", {"title": "t", "issue_key": KEY, "steps": [
        {"connector": "home", "args": ["rollout", "restart", "deploy/api", "-n", "nervecenter"]}]},
        guard="kube")
    await svc.decide(plan["id"], approve=True, actor="owner", act_mode="opencode")
    res = await svc.run_step(plan["id"], 0)
    assert res["ok"] and "rollout restart deploy/api -n nervecenter" in res["output"]


async def test_guarded_plan_without_a_guard_object_is_refused(svc) -> None:
    await _kube(svc)
    with pytest.raises(InfraInputError, match="not available"):
        await svc.propose("a", {"title": "t", "issue_key": KEY, "steps": [
            {"connector": "home", "args": ["get", "pods"]}]}, guard="kube")
