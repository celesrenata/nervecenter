"""Property 18: request gates (WebSocket Origin, session, method, CSRF).

**Validates: Requirements 6.7, 6.8, 6.9, 6.10**
"""
from __future__ import annotations

import string
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from starlette.testclient import WebSocketDenialResponse
from starlette.websockets import WebSocketDisconnect

from backend.api.auth import current_access
from backend.api.endpoints import workbench
from backend.workbench import ide_proxy
from backend.workbench.audit import AuditLog
from backend.workbench.ide_proxy import check_ws_gate, create_ide_router
from backend.workbench.projects import ProjectService

PERSON = {"sub": "test-person", "typ": "access", "roles": ["user"]}
WRITE = ("POST", "PUT", "PATCH", "DELETE")
SETTINGS = settings(
    max_examples=40,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)

_label = st.text(alphabet=string.ascii_lowercase + string.digits, min_size=1, max_size=10)
_hosts = st.builds(
    lambda a, b, port: f"{a}.{b}" + (f":{port}" if port else ""),
    _label,
    _label,
    st.one_of(st.none(), st.integers(1, 65535)),
)
_schemes = st.sampled_from(["http", "https"])


# -- pure gate ---------------------------------------------------------------


@SETTINGS
@given(
    host=_hosts,
    scheme=_schemes,
    other=_hosts,
    origin_kind=st.sampled_from(["match", "other", "missing", "empty", "garbage"]),
    session_ok=st.booleans(),
)
def test_check_ws_gate(
    host: str, scheme: str, other: str, origin_kind: str, session_ok: bool
) -> None:
    origin = {
        "match": f"{scheme}://{host}",
        "other": f"{scheme}://{other}",
        "missing": None,
        "empty": "",
        "garbage": "not an origin",
    }[origin_kind]
    got = check_ws_gate(session_ok, origin, [host])
    if not session_ok:
        assert got == 401
    elif origin_kind == "match" or (origin_kind == "other" and other.lower() == host.lower()):
        assert got is None
    else:
        assert got == 403


# -- router with a fake WebSocket client and counting upstream connector -----


class _Manager:
    async def get(self, slug: str) -> Any:
        return SimpleNamespace(pod_ip="10.0.0.9")

    async def record_traffic(self, slug: str) -> None:
        return None


class _Upstream:
    subprotocol = None

    def __aiter__(self) -> "_Upstream":
        return self

    async def __anext__(self) -> Any:
        raise StopAsyncIteration

    async def send(self, data: Any) -> None:
        return None

    async def close(self) -> None:
        return None


def _ws_client(
    monkeypatch: pytest.MonkeyPatch, session_ok: bool, allowed: str
) -> tuple[TestClient, list[str]]:
    connects: list[str] = []

    async def fake_connect(url: str, **_kw: Any) -> _Upstream:
        connects.append(url)
        return _Upstream()

    async def validate(_headers: Any) -> bool:
        return session_ok

    monkeypatch.setattr(ide_proxy.websockets, "connect", fake_connect)
    app = FastAPI()
    app.include_router(create_ide_router(_Manager(), validate, allowed_origins=[allowed]))  # type: ignore[arg-type]
    return TestClient(app, raise_server_exceptions=False), connects


@SETTINGS
@given(
    host=_hosts,
    other=_hosts,
    scheme=_schemes,
    origin_kind=st.sampled_from(["match", "other", "missing"]),
    session_ok=st.booleans(),
)
def test_ws_upgrade_gates_and_no_upstream_on_refusal(
    host: str, other: str, scheme: str, origin_kind: str, session_ok: bool
) -> None:
    with pytest.MonkeyPatch.context() as mp:
        # Pin the request Host so the router's implicit own-host allowance never
        # equals the generated origin by accident.
        client, connects = _ws_client(mp, session_ok, f"{scheme}://{host}")
        headers = {"host": "workbench.invalid", "x-forwarded-host": "workbench.invalid"}
        if origin_kind == "match":
            headers["origin"] = f"{scheme}://{host}"
        elif origin_kind == "other":
            headers["origin"] = f"{scheme}://{other}"
        matches = origin_kind == "match" or (
            origin_kind == "other" and other.lower() == host.lower()
        )
        expected = 401 if not session_ok else (None if matches else 403)
        if "origin" in headers and headers["origin"].endswith("workbench.invalid"):
            return
        with client:
            if expected is None:
                with client.websocket_connect("/ide/demo/", headers=headers):
                    pass
                assert len(connects) == 1
            else:
                with pytest.raises((WebSocketDenialResponse, WebSocketDisconnect)) as info:
                    with client.websocket_connect("/ide/demo/", headers=headers):
                        pass
                exc = info.value
                if isinstance(exc, WebSocketDenialResponse):
                    assert exc.status_code == expected
                assert connects == []


# -- Workbench router: methods and CSRF header -------------------------------


@pytest.fixture
def wb(monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, list[str]]:
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
    app = FastAPI()
    app.include_router(workbench.router)
    app.dependency_overrides[current_access] = lambda: PERSON
    return TestClient(app, raise_server_exceptions=False), seen


def _state_changing(client: TestClient) -> list[tuple[str, str]]:
    out = []
    for path, ops in client.app.openapi()["paths"].items():  # type: ignore[attr-defined]
        for m in ops:
            if m.upper() in WRITE:
                out.append((m.upper(), path))
    return out


def _url(path: str) -> str:
    return path.replace("{kind}", "work").replace("{slug}", "demo")


_bad_header = st.one_of(
    st.none(), st.just(""), st.just("0"), st.text(alphabet=string.ascii_letters + string.digits, max_size=8).filter(
        lambda s: s != "1"
    )
)
_cross_origin = st.one_of(st.none(), _hosts.map(lambda h: f"https://{h}"))


@SETTINGS
@given(data=st.data(), header=_bad_header, origin=_cross_origin)
def test_state_changing_without_csrf_header_is_403_and_does_nothing(
    wb: tuple[TestClient, list[str]], data: st.DataObject, header: str | None, origin: str | None
) -> None:
    client, seen = wb
    method, path = data.draw(st.sampled_from(_state_changing(client)))
    headers: dict[str, str] = {}
    if header is not None:
        headers["X-NC-Workbench"] = header
    if origin:
        headers["Origin"] = origin
    seen.clear()
    resp = client.request(method, _url(path), headers=headers, json={})
    assert resp.status_code == 403
    assert seen == []


@SETTINGS
@given(data=st.data(), method=st.sampled_from(["GET", "HEAD", *WRITE]))
def test_wrong_method_on_state_changing_path_is_405(
    wb: tuple[TestClient, list[str]], data: st.DataObject, method: str
) -> None:
    client, seen = wb
    routes = _state_changing(client)
    _, path = data.draw(st.sampled_from(routes))
    allowed = {m for m, p in routes if p == path}
    allowed_all = allowed | {
        m.upper()
        for m in client.app.openapi()["paths"][path]  # type: ignore[attr-defined]
    }
    if method in allowed_all or (method == "HEAD" and "GET" in allowed_all):
        return
    seen.clear()
    resp = client.request(method, _url(path), headers={"X-NC-Workbench": "1"}, json={})
    assert resp.status_code == 405
    assert seen == []
