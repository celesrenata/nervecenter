"""Runbook builders: every plan passes the guard and the kube exec policy; manifests parse."""
from __future__ import annotations

from typing import Any

import pytest
import yaml
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from backend.infra.policy import check_kubectl
from backend.kube import runbooks as rb
from backend.kube.guard import KubeGuard
from backend.kube.settings import KubeOverrideStore, KubeSettingsStore
from backend.persistence.connection import make_session_factory
from backend.persistence.models.kube import KubeSettings

SETTINGS = {"backup_nfs_server": "192.168.42.8", "backup_nfs_base": "/volume1/Kubernetes",
            "retention_daily": 7, "retention_weekly": 4}
KEY = "pod/apps/db/CrashLoopBackOff"
STAMP = "20300101040000"


@pytest.fixture
async def guard(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'r.db'}", poolclass=StaticPool,
                                 connect_args={"check_same_thread": False})
    from backend.persistence.models.kube import KubeOverride
    async with engine.begin() as conn:
        await conn.run_sync(KubeSettings.__table__.metadata.create_all,
                            tables=[KubeSettings.__table__, KubeOverride.__table__])
    factory = make_session_factory(engine)
    settings = KubeSettingsStore(factory)
    g = KubeGuard(settings, KubeOverrideStore(factory, settings))
    g.overrides = g._overrides  # type: ignore[attr-defined]
    yield g
    await engine.dispose()


async def _passes(guard: KubeGuard, steps: list[dict[str, Any]], *, overrides: tuple[str, ...] = (),
                  ) -> None:
    assert steps and len(steps) <= 25
    for ns in overrides:
        await guard.overrides.create(KEY, ns, "owner")
    await guard.check_plan(steps, KEY)
    for i, step in enumerate(steps):
        if "args" in step:
            check_kubectl(step["args"], act=True, stdin=step.get("stdin"), kube_guard=True)
        for field in ("requires", "on_failure_of"):
            assert all(0 <= j < i for j in step.get(field, [])), (i, field)
        assert step["kind"] in ("read", "backup", "backup_verify", "change", "repair",
                                "restore", "prune")


def _manifests(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [yaml.safe_load(s["stdin"]) for s in steps if s.get("stdin")]


async def test_backup_writes_to_the_nas_directory_and_ends_in_a_verified_step(guard) -> None:
    steps = rb.backup_steps("mysql", "apps", "db", SETTINGS, connector="home",
                            ssh_connector="nas", stamp=STAMP, secret_name="db-credentials")
    await _passes(guard, steps)
    assert steps[0]["command"] == "mkdir -p /volume1/Kubernetes/mysql/backups"
    assert [s["kind"] for s in steps] == ["change", "backup", "backup", "change", "backup_verify"]
    jobs = _manifests(steps)
    assert len(jobs) == 2
    for job in jobs:
        assert job["kind"] == "Job" and job["metadata"]["namespace"] == "apps"
        nfs = job["spec"]["template"]["spec"]["volumes"][0]["nfs"]
        assert nfs["server"] == "192.168.42.8"
        assert nfs["path"] == "/volume1/Kubernetes/mysql/backups"
    env = jobs[0]["spec"]["template"]["spec"]["containers"][0]["env"]
    assert {"name": "MYSQL_PWD", "valueFrom": {"secretKeyRef": {
        "name": "db-credentials", "key": "password"}}} in env
    assert "mysql-20300101040000.sql.gz" in jobs[0]["spec"]["template"]["spec"][
        "containers"][0]["command"][-1]
    assert "mariadb-check" in jobs[1]["spec"]["template"]["spec"]["containers"][0]["command"][-1]
    without = rb.backup_steps("postgres", "apps", "pg", SETTINGS, connector="home", stamp=STAMP)
    assert without[0]["kind"] == "backup" and "must create" in rb.nfs_dir_note(SETTINGS, "postgres")
    assert rb.nfs_dir(SETTINGS, "redis") == "/volume1/Kubernetes/redis/backups"


async def test_builders_offset_indexes_with_start(guard) -> None:
    steps = rb.backup_steps("redis", "apps", "cache", SETTINGS, connector="home", start=3,
                            stamp=STAMP)
    assert steps[1]["requires"] == [3] and steps[-1]["requires"] == [5]


async def test_prune_keeps_the_configured_retention(guard) -> None:
    steps = rb.prune_steps("mysql", "apps", SETTINGS, connector="home", stamp=STAMP)
    script = _manifests(steps)[0]["spec"]["template"]["spec"]["containers"][0]["command"][-1]
    assert "find . -maxdepth 1" in script and "-mtime +6" in script and "-mtime -35" in script
    assert steps[0]["kind"] == "prune"


async def test_mysql_repair_is_check_repair_restore_check(guard) -> None:
    steps = rb.mysql_repair_steps("apps", "db-0", "db", SETTINGS, connector="home",
                                  ssh_connector="nas", stamp=STAMP)
    await _passes(guard, steps)
    kinds = [s["kind"] for s in steps]
    repair = kinds.index("repair")
    assert kinds[repair - 1] == "read" and kinds[-1] == "read"
    assert steps[repair]["on_failure_of"] == [repair - 1]
    restores = [i for i, k in enumerate(kinds) if k == "restore"]
    assert restores and all(steps[i]["on_failure_of"] == [repair] for i in restores)
    assert steps[repair]["args"][-3:] == ["mysqlcheck", "--auto-repair", "--all-databases"]
    # with a backup done earlier in the plan
    again = rb.mysql_repair_steps("apps", "db-0", "db", SETTINGS, connector="home", start=0,
                                  verified_by=4, stamp=STAMP)
    assert all(4 not in s.get("requires", []) or s["kind"] in ("repair", "restore")
               for s in again)


async def test_redis_repair_restores_into_the_pvc(guard) -> None:
    steps = rb.redis_repair_steps("apps", "cache-0", "cache", SETTINGS, connector="home",
                                  data_pvc="cache-data", stamp=STAMP,
                                  workload_kind="statefulset")
    await _passes(guard, steps)
    assert ["scale", "statefulset/cache", "--replicas=0", "-n", "apps"] in [
        s.get("args") for s in steps]
    assert any(s["args"][:1] == ["scale"] and "--replicas=1" in s["args"] for s in steps)
    assert any(s.get("args", [""])[-3:-1] == ["--fix"] or "--fix" in s.get("args", [])
               for s in steps)
    with pytest.raises(ValueError, match="data_pvc"):
        rb.restore_steps("redis", "apps", "cache", SETTINGS, connector="home", stamp=STAMP,
                         verified_by=0)


async def test_postgres_uses_jobs_not_exec(guard) -> None:
    steps = rb.postgres_steps("apps", "pg", SETTINGS, connector="home", stamp=STAMP,
                              secret_name="pg-credentials")
    await _passes(guard, steps)
    assert not any(s.get("args", [""])[0] == "exec" for s in steps)
    scripts = " ".join(m["spec"]["template"]["spec"]["containers"][0]["command"][-1]
                       for m in _manifests(steps))
    assert "pg_amcheck" in scripts and "reindexdb" in scripts and "psql" in scripts
    assert {s["kind"] for s in steps} >= {"backup", "backup_verify", "repair", "restore", "read"}


async def test_nextcloud_upgrade_backs_up_then_runs_occ_without_maintenance_mode(guard) -> None:
    steps = rb.nextcloud_upgrade_steps(
        "nextcloud", "nc-0", SETTINGS, connector="home", db_workload="nc-db",
        config_pvc="nc-config", ssh_connector="nas", stamp=STAMP)
    await _passes(guard, steps)
    execs = [s["args"][s["args"].index("--") + 1:] for s in steps
             if s.get("args", [""])[0] == "exec"]
    assert [e[-1] if e[-1] == "upgrade" else e[-2] if e[-1] == "--no-interaction" else e[-1]
            for e in execs] == ["upgrade", "db:add-missing-indices", "db:add-missing-columns",
                                "db:convert-filecache-bigint"]
    assert all(e[:5] == ["runuser", "-u", "www-data", "--", "php"] for e in execs)
    assert not any("maintenance" in str(s) for s in steps)
    upgrade_at = next(i for i, s in enumerate(steps) if "upgrade" in s.get("args", []))
    assert sum(1 for s in steps[:upgrade_at] if s["kind"] == "backup_verify") == 2
    assert "/volume1/Kubernetes/nextcloud/backups" in [
        v["nfs"]["path"] for m in _manifests(steps)
        for v in m["spec"]["template"]["spec"]["volumes"] if "nfs" in v]


async def test_crashloop_and_imagepull(guard) -> None:
    await _passes(guard, rb.crashloop_steps("apps", "web", connector="home"))
    await _passes(guard, rb.imagepull_steps("apps", "web", connector="home"))
    fixed = rb.imagepull_steps("apps", "web", connector="home", container="app",
                               fixed_image="registry.celestium.life/web:1.2.3")
    await _passes(guard, fixed)
    assert any(s.get("args", [""])[0] == "set" for s in fixed)
    with pytest.raises(ValueError, match="container"):
        rb.imagepull_steps("apps", "web", connector="home", fixed_image="x:1")


async def test_pvc_steps_snapshot_first_and_need_an_override_in_longhorn(guard) -> None:
    steps = rb.pvc_steps("apps", "data", "pvc-1234", connector="home", stamp=STAMP, pod="db-0",
                         delete_pvc=True)
    assert steps[0]["kind"] == "backup" and steps[1]["kind"] == "backup_verify"
    await _passes(guard, steps, overrides=("longhorn-system",))
    snap = _manifests(steps)[0]
    assert snap["kind"] == "Snapshot" and snap["metadata"]["namespace"] == "longhorn-system"
    assert all(s["requires"] == [1] for s in steps[2:])


async def test_pvc_deletion_without_the_snapshot_is_refused(guard) -> None:
    from backend.kube.guard import GuardError
    with pytest.raises(GuardError) as err:
        await guard.check_plan([{"connector": "home", "kind": "change",
                                 "args": ["delete", "pvc", "data", "-n", "apps"]}], KEY)
    assert err.value.code == "backup_chain_missing"


@pytest.mark.parametrize("bad", ["Apps", "a b", "a;rm", "", "-x"])
def test_names_are_validated_before_they_reach_a_manifest(bad) -> None:
    with pytest.raises(ValueError):
        rb.backup_steps("mysql", bad, "db", SETTINGS, connector="home", stamp=STAMP)
    with pytest.raises(ValueError):
        rb.crashloop_steps("apps", bad, connector="home")


def test_other_inputs_are_validated() -> None:
    with pytest.raises(ValueError):
        rb.backup_steps("oracle", "apps", "db", SETTINGS, connector="home")
    with pytest.raises(ValueError):
        rb.backup_steps("mysql", "apps", "db", SETTINGS, connector="home", stamp="x")
    with pytest.raises(ValueError):
        rb.backup_steps("mysql", "apps", "db", SETTINGS, connector="home", stamp=STAMP,
                        image="bad image; rm")
    with pytest.raises(ValueError):
        rb.mysql_repair_steps("apps", "db-0", "db", SETTINGS, connector="home",
                              secret_name="Bad Name")
