"""/api/kube: people only, settings validation, overrides and enable-proposals (fakes only)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from backend.api.auth import current_access
from backend.api.endpoints import kube as kube_ep
from backend.kube.monitor import CheckBusy
from backend.kube.store import KubeServices
from backend.persistence.connection import make_session_factory
from backend.persistence.models.kube import (
    KubeIssue,
    KubeOverride,
    KubeReport,
    KubeSettings,
)

PERSON = {"sub": "owner", "preferred_username": "celes", "typ": "access", "roles": ["owner"]}
DEVICE = {"sub": "device:mac", "typ": "device", "roles": ["device"]}
KEY = "pod/longhorn-system/mgr-1/CrashLoopBackOff"


class FakeMonitor:
    running = False
    last: dict[str, Any] | None = None

    def __init__(self) -> None:
        self.busy = False

    async def check(self) -> dict[str, Any]:
        if self.busy:
            raise CheckBusy("busy")
        return {"new": 0, "open": 1}


@pytest.fixture
async def env(tmp_path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'k.db'}", poolclass=StaticPool,
                                 connect_args={"check_same_thread": False})
    async with engine.begin() as conn:
        await conn.run_sync(KubeSettings.__table__.metadata.create_all, tables=[
            KubeSettings.__table__, KubeIssue.__table__, KubeOverride.__table__,
            KubeReport.__table__])
    svc = KubeServices(make_session_factory(engine))
    now = datetime.now(UTC)
    snap = {"pods_unhealthy": [
        {"key": KEY, "kind": "pod", "namespace": "longhorn-system", "name": "mgr-1",
         "reason": "CrashLoopBackOff", "severity": "critical"},
        {"key": "pod/apps/web/CrashLoopBackOff", "kind": "pod", "namespace": "apps",
         "name": "web", "reason": "CrashLoopBackOff", "severity": "critical"},
        {"key": "pod/stonks-beta/x/Error", "kind": "pod", "namespace": "stonks-beta",
         "name": "x", "reason": "Error", "severity": "warning"}]}
    await svc.issues.sync(snap, await svc.settings.get(), now)
    yield svc
    await engine.dispose()


def _client(svc: KubeServices, claims: dict[str, Any] = PERSON) -> tuple[TestClient, FakeMonitor]:
    app = FastAPI()
    app.include_router(kube_ep.router)
    app.state.kube_services = svc
    mon = FakeMonitor()
    app.state.kube_monitor = mon
    app.dependency_overrides[current_access] = lambda: claims
    return TestClient(app), mon


def test_every_route_needs_a_signed_in_person(env) -> None:
    c, _ = _client(env, DEVICE)
    for method, url in [("get", "/api/kube/status"), ("get", "/api/kube/issues"),
                        ("get", "/api/kube/waste"), ("put", "/api/kube/settings"),
                        ("post", "/api/kube/check"),
                        ("post", f"/api/kube/issues/{quote(KEY, safe='')}/override"),
                        ("delete", "/api/kube/overrides/x"),
                        ("post", "/api/kube/enable-proposals"),
                        ("get", "/api/kube/groups"), ("post", "/api/kube/known-down")]:
        assert getattr(c, method)(url).status_code == 403, url


def test_status_and_issues_show_modes(env) -> None:
    c, _ = _client(env)
    status = c.get("/api/kube/status").json()
    assert status["counts"] == {"open": 2, "known_down": 1, "diagnose_only": 1, "overrides": 0}
    assert status["settings"]["diagnose_only_until_first_report"] is True
    modes = {i["key"]: i["mode"] for i in c.get("/api/kube/issues").json()["issues"]}
    assert modes[KEY] == "diagnose_only" and modes["pod/apps/web/CrashLoopBackOff"] == "normal"
    assert modes["pod/stonks-beta/x/Error"] == "known_down"
    assert c.get("/api/kube/waste").json() == {"report": None}


def test_settings_are_validated_and_record_the_actor(env) -> None:
    c, _ = _client(env)
    ok = c.put("/api/kube/settings", json={"known_down": ["a-b"], "override_hours": 12,
                                           "prometheus_url": "http://prom:9090"})
    assert ok.status_code == 200
    assert ok.json()["known_down"] == ["a-b"] and ok.json()["updated_by"] == "celes"
    for bad in ({"known_down": ["Bad Name"]}, {"override_hours": 0}, {"override_hours": "x"},
                {"nope": 1}, {"prometheus_url": "file:///etc"}, {"daily_run_cap": True}):
        assert c.put("/api/kube/settings", json=bad).status_code == 422, bad


def test_override_only_for_protected_diagnose_only_issues(env) -> None:
    c, _ = _client(env)
    url = f"/api/kube/issues/{quote(KEY, safe='')}/override"
    r = c.post(url)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["created_by"] == "celes" and body["namespace"] == "longhorn-system"
    made, expires = (datetime.fromisoformat(body[k]) for k in ("created_at", "expires_at"))
    assert expires - made == timedelta(hours=24)
    listed = c.get("/api/kube/issues").json()
    assert [o["id"] for o in listed["overrides"]] == [body["id"]]
    issue = next(i for i in listed["issues"] if i["key"] == KEY)
    assert issue["mode"] == "normal" and issue["override"]["created_by"] == "celes"
    assert c.post(url).status_code == 422  # already overridden: nothing to override
    for key in ("pod/apps/web/CrashLoopBackOff", "pod/stonks-beta/x/Error"):
        assert c.post(f"/api/kube/issues/{quote(key, safe='')}/override").status_code == 422
    assert c.post("/api/kube/issues/pod%2Fnone%2Fx/override").status_code == 404


def test_override_can_be_revoked(env) -> None:
    c, _ = _client(env)
    url = f"/api/kube/issues/{quote(KEY, safe='')}/override"
    oid = c.post(url).json()["id"]
    assert c.delete(f"/api/kube/overrides/{oid}").status_code == 204
    assert c.get("/api/kube/issues").json()["overrides"] == []
    assert c.delete("/api/kube/overrides/missing").status_code == 404


def test_enable_proposals_clears_the_first_run_flag(env) -> None:
    c, _ = _client(env)
    r = c.post("/api/kube/enable-proposals")
    assert r.status_code == 200
    assert r.json()["diagnose_only_until_first_report"] is False
    assert r.json()["updated_by"] == "celes"


def test_check_now_returns_409_while_running(env) -> None:
    c, mon = _client(env)
    assert c.post("/api/kube/check").json() == {"new": 0, "open": 1}
    mon.busy = True
    assert c.post("/api/kube/check").status_code == 409


def test_switch_changes_are_logged_with_who_and_shown_in_status(env) -> None:
    c, _ = _client(env)
    ok = c.put("/api/kube/settings", json={"known_down_off": ["stonks-beta"],
                                           "auto_agent_enabled": False})
    assert ok.status_code == 200 and ok.json()["auto_agent_enabled"] is False
    assert c.put("/api/kube/settings", json={"auto_agent_enabled": False}).status_code == 200
    log = c.get("/api/kube/status").json()["settings_log"]
    assert len(log) == 1  # the unchanged second save is not a change
    assert log[0]["actor"] == "celes" and log[0]["at"]
    assert log[0]["changes"]["auto_agent_enabled"] == {"from": True, "to": False}
    assert log[0]["changes"]["known_down_off"]["to"] == ["stonks-beta"]
    modes = {i["key"]: i["mode"] for i in c.get("/api/kube/issues").json()["issues"]}
    assert modes["pod/stonks-beta/x/Error"] == "normal"  # switched off: checked like any other


def test_switches_cannot_be_changed_by_a_device(env) -> None:
    c, _ = _client(env, DEVICE)
    assert c.put("/api/kube/settings", json={"check_enabled": False}).status_code == 403

async def test_groups_carry_diagnosis_and_known_down_button_is_logged(env) -> None:
    from backend.kube.groups import DiagnosisStore
    await DiagnosisStore(env.reports).put(
        "apps:crashloopbackoff-web", {"status": "done", "summary": "bad config",
                                      "evidence": ["exit 1"], "confidence": "high",
                                      "run_id": "r1"}, datetime.now(UTC))
    c, _ = _client(env)
    groups = {g["id"]: g for g in c.get("/api/kube/groups").json()["groups"]}
    web = groups["apps:crashloopbackoff-web"]
    assert web["diagnosis"]["summary"] == "bad config" and web["can_add_known_down"] is True
    assert groups["stonks-beta:error-x"]["can_add_known_down"] is False  # already known down
    assert groups["longhorn-system:crashloopbackoff-mgr-1"]["diagnosis"] is None
    done = c.post("/api/kube/known-down", json={"namespace": "stonks-oracle"})
    assert done.status_code == 200 and "stonks-oracle" in done.json()["known_down"]
    log = c.get("/api/kube/status").json()["settings_log"]
    assert log[0]["actor"] == "celes" and "known_down" in log[0]["changes"]
    assert c.post("/api/kube/known-down", json={"namespace": "Bad Name"}).status_code == 422
