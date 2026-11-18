"""Issue ledger: new / existing / resolved, known-down, key stability, report retention."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from backend.kube.issues import IssueStore, ReportStore
from backend.kube.settings import KubeSettingsError, KubeSettingsStore, classify_namespace
from backend.persistence.connection import make_session_factory
from backend.persistence.models.kube import KubeIssue, KubeOverride, KubeReport, KubeSettings

T0 = datetime(2030, 1, 1, 12, 0, tzinfo=UTC)


@pytest.fixture
async def factory(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'k.db'}", poolclass=StaticPool,
                                 connect_args={"check_same_thread": False})
    tables = [m.__table__ for m in (KubeSettings, KubeIssue, KubeOverride, KubeReport)]
    async with engine.begin() as conn:
        await conn.run_sync(KubeIssue.__table__.metadata.create_all, tables=tables)
    yield make_session_factory(engine)
    await engine.dispose()


def _p(ns: str, workload: str, reason: str, pod: str = "p") -> dict[str, Any]:
    return {"key": f"pod/{ns}/{workload}/{reason}", "kind": "pod", "namespace": ns,
            "name": workload, "reason": reason, "severity": "critical", "pod": pod}


def _snap(*problems: dict[str, Any]) -> dict[str, Any]:
    return {"pods_unhealthy": list(problems)}


async def test_new_existing_and_resolved(factory) -> None:
    settings = await KubeSettingsStore(factory).get()
    store = IssueStore(factory)
    a, b = _p("app", "web", "CrashLoopBackOff"), _p("app", "img", "ImagePullBackOff")
    first = await store.sync(_snap(a, b), settings, T0)
    assert {i["key"] for i in first["new"]} == {a["key"], b["key"]}
    second = await store.sync(_snap(a), settings, T0 + timedelta(hours=1))
    assert second["new"] == [] and [i["key"] for i in second["open"]] == [a["key"]]
    assert [i["key"] for i in second["resolved"]] == [b["key"]]
    row = next(i for i in await store.list(None) if i["key"] == a["key"])
    assert row["first_seen"] == T0 and row["last_seen"] == T0 + timedelta(hours=1)
    again = await store.sync(_snap(a, b), settings, T0 + timedelta(hours=2))
    assert [i["key"] for i in again["new"]] == [b["key"]]  # reopened counts as new
    assert next(i for i in await store.list("open") if i["key"] == b["key"])[
        "first_seen"] == T0 + timedelta(hours=2)


async def test_key_is_stable_across_pod_name_changes(factory) -> None:
    settings = await KubeSettingsStore(factory).get()
    store = IssueStore(factory)
    await store.sync(_snap(_p("app", "web", "CrashLoopBackOff", "web-aaaaa")), settings, T0)
    out = await store.sync(_snap(_p("app", "web", "CrashLoopBackOff", "web-bbbbb")), settings,
                           T0 + timedelta(hours=1))
    assert out["new"] == [] and out["resolved"] == [] and len(out["open"]) == 1


async def test_known_down_is_stored_but_never_a_failure(factory) -> None:
    settings = await KubeSettingsStore(factory).get()
    store = IssueStore(factory)
    out = await store.sync(_snap(_p("stonks-beta", "api", "CrashLoopBackOff"),
                                 _p("kube-system", "dns", "CrashLoopBackOff"),
                                 _p("app", "web", "CrashLoopBackOff")), settings, T0)
    assert [i["namespace"] for i in out["known_down"]] == ["stonks-beta"]
    assert {i["namespace"] for i in out["new"]} == {"kube-system", "app"}
    modes = {i["namespace"]: i["mode"] for i in await store.list("open")}
    assert modes == {"stonks-beta": "known_down", "kube-system": "diagnose_only",
                     "app": "normal"}
    follow = await store.sync(_snap(_p("stonks-beta", "api", "CrashLoopBackOff")), settings,
                              T0 + timedelta(hours=1))
    assert follow["open"] == [] and follow["new"] == []


async def test_override_turns_protected_issue_normal(factory) -> None:
    settings = await KubeSettingsStore(factory).get()
    p = _p("kube-system", "dns", "CrashLoopBackOff")
    ov = [{"issue_key": p["key"], "namespace": "kube-system"}]
    out = await IssueStore(factory).sync(_snap(p), settings, T0, ov)
    assert out["new"][0]["mode"] == "normal"


def test_classify_namespace() -> None:
    s = {"protected_namespaces": ["nervecenter"], "known_down": ["stonks-beta"]}
    ov = [{"issue_key": "k", "namespace": "nervecenter"},
          {"issue_key": "k", "namespace": "stonks-beta"}]
    assert classify_namespace("stonks-beta", s, ov, "k") == "known_down"
    assert classify_namespace("nervecenter", s, ov, "k") == "override_active"
    assert classify_namespace("nervecenter", s, ov, "other") == "protected"
    assert classify_namespace("nervecenter", s, []) == "protected"
    assert classify_namespace("apps", s, ov, "k") == "normal"


async def test_settings_are_seeded_and_validated(factory) -> None:
    store = KubeSettingsStore(factory)
    s = await store.get()
    assert "nervecenter" in s["protected_namespaces"] and s["known_down"] == [
        "stonks-beta", "comfyui-service"]
    assert (s["backup_nfs_server"], s["retention_daily"], s["retention_weekly"]) == (
        "192.168.42.8", 7, 4)
    new = await store.update({"known_down": ["a-b"], "override_hours": 12}, "owner")
    assert new["known_down"] == ["a-b"] and new["updated_by"] == "owner" and new["updated_at"]
    for bad in ({"known_down": ["Bad_Name"]}, {"known_down": ["x"] * 101},
                {"override_hours": 0}, {"retention_daily": True}, {"backup_nfs_base": "../x"},
                {"nope": 1}, {"backup_nfs_server": "a b"}):
        with pytest.raises(KubeSettingsError):
            await store.update(bad, "owner")


async def test_report_retention(factory) -> None:
    reports = ReportStore(factory)
    for i in range(30):
        await reports.add("snapshot", {"i": i}, T0 + timedelta(minutes=i))
    for i in range(20):
        await reports.add("waste", {"i": i}, T0 + timedelta(minutes=i))
    async with factory() as s:
        from sqlalchemy import func, select
        counts = dict((await s.execute(
            select(KubeReport.kind, func.count()).group_by(KubeReport.kind))).all())
    assert counts == {"snapshot": 24, "waste": 14}
    assert (await reports.latest("snapshot"))["data"] == {"i": 29}


async def test_issue_in_an_unreadable_namespace_is_not_resolved(factory) -> None:
    settings = await KubeSettingsStore(factory).get()
    store = IssueStore(factory)
    a = _p("app", "web", "CrashLoopBackOff")
    await store.sync(_snap(a), settings, T0)
    blind = {"pods_unhealthy": [], "coverage": {
        "unreadable": ["app"], "unreadable_by_kind": {"pods": ["app"]}, "cluster_unreadable": []}}
    out = await store.sync(blind, settings, T0 + timedelta(hours=1))
    assert out["resolved"] == []  # not seen is not fixed
    assert [i["key"] for i in await store.list("open")] == [a["key"]]
    seen = {"pods_unhealthy": [], "coverage": {"unreadable_by_kind": {}, "cluster_unreadable": []}}
    done = await store.sync(seen, settings, T0 + timedelta(hours=2))
    assert [i["key"] for i in done["resolved"]] == [a["key"]]  # readable and gone: resolved


async def test_switches_default_on_and_off_flags_follow_their_lists(factory) -> None:
    store = KubeSettingsStore(factory)
    s = await store.get()
    assert s["check_enabled"] and s["waste_enabled"] and s["auto_agent_enabled"]
    assert s["known_down_off"] == [] and s["protected_off"] == []
    s = await store.update({"known_down_off": ["stonks-beta", "ghost"],
                            "protected_off": ["argocd"]}, "celes")
    assert s["known_down_off"] == ["stonks-beta", "ghost"][:1]  # only names on the list count
    assert classify_namespace("stonks-beta", s) == "normal"
    assert classify_namespace("comfyui-service", s) == "known_down"
    assert classify_namespace("argocd", s) == "normal"
    assert classify_namespace("kube-system", s) == "protected"
    s = await store.update({"known_down": ["comfyui-service"]}, "celes")
    assert s["known_down_off"] == []  # removed from the list: its off flag goes too
    with pytest.raises(KubeSettingsError):
        await store.update({"check_enabled": "no"}, "celes")
    with pytest.raises(KubeSettingsError):
        await store.update({"known_down_off": ["Bad Name"]}, "celes")
