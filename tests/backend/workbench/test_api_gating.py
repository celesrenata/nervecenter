"""Workbench API authentication and gating (Req 6.1, 6.4, 6.9, 6.10, 11.3, 11.6)."""
from __future__ import annotations

import importlib
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from backend.api.auth import current_access
from backend.api.endpoints import workbench
from backend.workbench.audit import AuditLog
from backend.workbench.projects import ProjectService

app_module = importlib.import_module("backend.api.app")

PERSON = {"sub": "test-person", "typ": "access", "roles": ["user"]}
HEADER = {"X-NC-Workbench": "1"}
BODIES: dict[str, dict[str, Any]] = {
    "/api/workbench/projects": {"slug": "demo", "kind": "work"},
    "/api/workbench/projects/import": {"kind": "work", "name": "demo"},
}


@pytest.fixture(autouse=True)
def _restore_app_module(monkeypatch: pytest.MonkeyPatch):
    """ALL_ROUTERS is built at import, so each phase needs a module reload."""
    yield
    monkeypatch.delenv("WORKBENCH_PHASE", raising=False)
    monkeypatch.delenv("WORKBENCH_IDE_ENABLED", raising=False)
    importlib.reload(app_module)


def _app(monkeypatch: pytest.MonkeyPatch, phase: str = "1") -> FastAPI:
    monkeypatch.setenv("WORKBENCH_PHASE", phase)
    importlib.reload(app_module)
    return app_module.create_app()


_METHODS = {"get", "post", "put", "patch", "delete"}


def _all_paths(app: FastAPI) -> dict[str, Any]:
    return app.openapi()["paths"]


def _workbench_routes(app: FastAPI) -> list[tuple[str, str]]:
    return [
        (m.upper(), path)
        for path, ops in _all_paths(app).items()
        if path.startswith("/api/workbench")
        for m in ops
        if m in _METHODS
    ]


def _url(path: str) -> str:
    return path.replace("{kind}", "work").replace("{slug}", "demo")


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record any service or audit operation; none may run for refused requests."""
    seen: list[str] = []

    def spy(name: str):
        async def _f(*_a: Any, **_k: Any) -> Any:
            seen.append(name)
            return {}

        return _f

    for name in ("list", "list_unregistered", "create", "import_dir", "get",
                 "archive", "delete", "set_push_approved"):
        monkeypatch.setattr(ProjectService, name, spy(name))
    monkeypatch.setattr(AuditLog, "record", spy("audit"))
    return seen


@pytest.mark.route_auth
def test_every_workbench_route_returns_401_without_session(
    monkeypatch: pytest.MonkeyPatch, calls: list[str]
) -> None:
    app = _app(monkeypatch)
    routes = _workbench_routes(app)
    assert len(routes) >= 9
    with TestClient(app, raise_server_exceptions=False) as client:
        for method, path in routes:
            for headers in ({}, HEADER):
                resp = client.request(
                    method, _url(path), headers=headers, json=BODIES.get(path, {})
                )
                assert resp.status_code == 401, (method, path, headers)
    assert calls == []


@pytest.mark.route_auth
def test_unauthenticated_request_returns_no_project_data_and_changes_nothing(
    monkeypatch: pytest.MonkeyPatch, calls: list[str]
) -> None:
    """Req 11.6."""
    app = _app(monkeypatch)
    with TestClient(app, raise_server_exceptions=False) as client:
        got = client.get("/api/workbench/projects")
        made = client.post("/api/workbench/projects", headers=HEADER, json=BODIES["/api/workbench/projects"])
        gone = client.request(
            "DELETE", "/api/workbench/projects/work/demo", headers=HEADER, json={"confirm": "demo"}
        )
    for resp in (got, made, gone):
        assert resp.status_code == 401
        assert "projects" not in resp.json() and "slug" not in resp.text
    assert calls == []


@pytest.mark.route_auth
def test_workbench_routes_never_public(monkeypatch: pytest.MonkeyPatch) -> None:
    from backend.api.public_routes import PUBLIC_ROUTES

    for method, path in _workbench_routes(_app(monkeypatch)):
        assert (method, path) not in PUBLIC_ROUTES


@pytest.mark.parametrize("phase", ["0", "1", "2", "3"])
def test_only_phase_one_workbench_router_is_mounted(
    monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    app = _app(monkeypatch, phase)
    wb = {p for p in _all_paths(app) if p.startswith("/api/workbench")}
    if phase == "0":
        assert wb == set()
        assert workbench.mounted_routers() == []
    else:
        assert wb
        if phase == "1":
            assert workbench.mounted_routers() == [workbench.router]
            allowed = ("config", "health", "projects", "unregistered")
        else:
            assert workbench.mounted_routers() == [
                workbench.phase2_router,
                workbench.router,
            ]
            allowed = ("config", "health", "projects", "unregistered", "sessions", "usage")
        for p in wb:
            assert p.split("/")[3] in allowed, p


def test_phase_three_ide_routes_need_ide_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WORKBENCH_IDE_ENABLED", raising=False)
    off = _app(monkeypatch, "3")
    assert not any(p.startswith("/api/workbench/ide") for p in _all_paths(off))
    assert workbench.phase3_router not in workbench.mounted_routers()

    monkeypatch.setenv("WORKBENCH_IDE_ENABLED", "1")
    on = _app(monkeypatch, "3")
    paths = _all_paths(on)
    assert "/api/workbench/ide" in paths
    assert "/api/workbench/ide/{kind}/{slug}/start" in paths
    assert "/api/workbench/ide/{kind}/{slug}/stop" in paths
    assert "/api/workbench/ide/{kind}/{slug}/status" in paths
    assert workbench.mounted_routers() == [
        workbench.phase3_router,
        workbench.phase2_router,
        workbench.router,
    ]
    # IDE routes are never mounted below phase 3 even when enabled.
    two = _app(monkeypatch, "2")
    assert not any(p.startswith("/api/workbench/ide") for p in _all_paths(two))


def test_ide_routes_require_session_and_header(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WORKBENCH_IDE_ENABLED", "1")
    app = _app(monkeypatch, "3")
    with TestClient(app, raise_server_exceptions=False) as client:
        assert client.get("/api/workbench/ide").status_code == 401
        assert (
            client.post("/api/workbench/ide/opencode/demo/start", headers=HEADER).status_code
            == 401
        )
    with _signed_in(app) as client:
        assert client.post("/api/workbench/ide/opencode/demo/start").status_code == 403
        assert client.post("/api/workbench/ide/opencode/demo/stop").status_code == 403


def test_config_reports_ide_enabled_truthfully(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WORKBENCH_IDE_ENABLED", "1")
    monkeypatch.delenv("WORKBENCH_IDE_IMAGE", raising=False)
    app = _app(monkeypatch, "3")
    with _signed_in(app) as client:
        assert client.get("/api/workbench/config").json()["ide_enabled"] is False
    monkeypatch.setenv("WORKBENCH_IDE_IMAGE", "img@sha256:" + "a" * 64)
    with _signed_in(app) as client:
        assert client.get("/api/workbench/config").json()["ide_enabled"] is True


def test_phase_one_does_not_mount_phase_two_or_three_routes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _app(monkeypatch)
    for p in ("sessions", "ide", "agents", "chat", "terminal", "git"):
        assert not any(x.startswith(f"/api/workbench/{p}") for x in _all_paths(app)), p
    with TestClient(app, raise_server_exceptions=False) as client:
        assert client.get("/api/workbench/sessions").status_code in (404, 405)


def _signed_in(app: FastAPI) -> TestClient:
    app.dependency_overrides[current_access] = lambda: PERSON
    return TestClient(app, raise_server_exceptions=False)


def test_state_changing_requests_without_header_return_403(
    monkeypatch: pytest.MonkeyPatch, calls: list[str]
) -> None:
    app = _app(monkeypatch)
    state_changing = [(m, p) for m, p in _workbench_routes(app) if m != "GET"]
    assert state_changing
    with _signed_in(app) as client:
        for method, path in state_changing:
            for headers in ({}, {"X-NC-Workbench": "0"}):
                resp = client.request(
                    method, _url(path), headers=headers, json=BODIES.get(path, {})
                )
                assert resp.status_code == 403, (method, path, headers)
    assert calls == []


def test_read_requests_do_not_need_header(monkeypatch: pytest.MonkeyPatch, calls: list[str]) -> None:
    app = _app(monkeypatch)
    with _signed_in(app) as client:
        assert client.get("/api/workbench/config").status_code == 200
        assert client.get("/api/workbench/projects").status_code not in (401, 403)


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("PUT", "/api/workbench/projects"),
        ("DELETE", "/api/workbench/projects"),
        ("GET", "/api/workbench/projects/import"),
        ("PUT", "/api/workbench/projects/work/demo/archive"),
        ("GET", "/api/workbench/projects/work/demo/archive"),
        ("PATCH", "/api/workbench/projects/work/demo/push-approval"),
        ("POST", "/api/workbench/projects/work/demo"),
    ],
)
def test_wrong_method_on_state_changing_path_returns_405(
    monkeypatch: pytest.MonkeyPatch, calls: list[str], method: str, path: str
) -> None:
    app = _app(monkeypatch)
    with _signed_in(app) as client:
        resp = client.request(method, path, headers=HEADER, json={})
    assert resp.status_code == 405
    assert calls == []
