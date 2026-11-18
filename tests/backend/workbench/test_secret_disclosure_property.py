"""Property 20: the opencode password is never disclosed (Req 6.5).

For any value of NERVE_OPENCODE_PASSWORD (the ``nervecenter-opencode`` secret is
mounted into that variable), no Workbench endpoint response body or header, and
no captured log line, contains it. Every Workbench route is exercised on its
error paths: opencode unreachable, opencode rejecting the credentials, opencode
not configured, and store failures.

**Validates: Requirements 6.5**
"""
from __future__ import annotations

import base64
import contextlib
import importlib
import io
import logging
import os
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from backend.api.auth import current_access
from backend.api.endpoints import workbench
from backend.handoffs.central import PASSWORD_ENV, URL_ENV, OpenCodeClient
from backend.workbench.audit import AuditLog
from backend.workbench.sessions import SessionService
from backend.workbench.store import StoreUnavailable

app_module = importlib.import_module("backend.api.app")

PERSON = {"sub": "test-person", "typ": "access", "roles": ["user"]}
HEADER = {"X-NC-Workbench": "1"}
FIXED_PASSWORD = "Sup3r-S3cret-opencode-pw!42"
OPENCODE_URL = "http://opencode.invalid:4096"

_PW_ALPHABET = st.characters(
    whitelist_categories=("Lu", "Ll", "Nd"), whitelist_characters="-_.!#%+=~", max_codepoint=0x7E
)
# The prefix keeps a random value from colliding with ordinary response text.
passwords = st.text(_PW_ALPHABET, min_size=8, max_size=40).map(lambda s: "Zq9~" + s)

# (method, openapi path, json body); path params are filled by ``_url``.
REQUESTS: list[tuple[str, str, dict[str, Any] | None]] = [
    ("GET", "/api/workbench/config", None),
    ("GET", "/api/workbench/health", None),
    ("GET", "/api/workbench/projects", None),
    ("POST", "/api/workbench/projects", {"slug": "demo", "kind": "opencode"}),
    ("GET", "/api/workbench/unregistered", None),
    ("POST", "/api/workbench/projects/import", {"kind": "opencode", "name": "demo"}),
    ("GET", "/api/workbench/projects/{kind}/{slug}", None),
    ("POST", "/api/workbench/projects/{kind}/{slug}/archive", {}),
    ("DELETE", "/api/workbench/projects/{kind}/{slug}", {"confirm": "demo"}),
    ("POST", "/api/workbench/projects/{kind}/{slug}/push-approval", {"approved": True}),
    ("GET", "/api/workbench/projects/{slug}/files", None),
    ("GET", "/api/workbench/projects/{slug}/file?path=a.txt", None),
    ("PUT", "/api/workbench/projects/{slug}/file", {"path": "a.txt", "content": "x"}),
    ("POST", "/api/workbench/projects/{slug}/fs", {"op": "mkdir", "path": "d", "is_dir": True}),
    ("GET", "/api/workbench/projects/{slug}/changes", None),
    ("POST", "/api/workbench/projects/{slug}/sessions", {}),
    ("GET", "/api/workbench/projects/{slug}/sessions", None),
    ("GET", "/api/workbench/sessions/{session_id}/events", None),
    ("POST", "/api/workbench/sessions/{session_id}/messages", {"text": "hello"}),
    ("POST", "/api/workbench/sessions/{session_id}/abort", {}),
    (
        "POST",
        "/api/workbench/sessions/{session_id}/selection-action",
        {"path": "a.txt", "selection": "x", "instruction": "explain"},
    ),
    ("GET", "/api/workbench/usage", None),
]


def _url(path: str) -> str:
    return (
        path.replace("{kind}", "opencode")
        .replace("{slug}", "demo")
        .replace("{session_id}", "ses_1")
    )


class _FailingStore:
    """Project and file service stand-in: every operation reports a store outage."""

    def __getattr__(self, name: str) -> Any:
        async def _fail(*_a: Any, **_k: Any) -> Any:
            raise StoreUnavailable("opencode", "http 502")

        return _fail


class _FakeUsage:
    def __init__(self, *_a: Any, **_k: Any) -> None:
        pass

    async def list_usage(self, *_a: Any, **_k: Any) -> list[Any]:
        return []


class _FakeSessions(SessionService):
    """Real SessionService logic over a fake mirror, so only opencode calls can fail."""

    def __init__(self, client: OpenCodeClient) -> None:
        super().__init__(lambda: None, client, usage=_FakeUsage())  # type: ignore[arg-type]

    async def _project(self, slug: str) -> Any:
        return SimpleNamespace(
            id=1, slug=slug, title=slug, kind="opencode", push_approved=False
        )

    async def _mirror(self, session_id: str) -> Any:
        return SimpleNamespace(
            id=session_id, status="running", model="omniroute/local/code", project_id=1
        )

    async def list(self, slug: str) -> list[dict[str, Any]]:
        await self._client.list_sessions()
        return []


def _transport(mode: str, password: str) -> httpx.MockTransport:
    """An opencode that is down or rejects credentials, and echoes secrets back."""
    basic = base64.b64encode(f"opencode:{password}".encode()).decode()

    def handler(request: httpx.Request) -> httpx.Response:
        if mode == "down":
            raise httpx.ConnectError(f"cannot connect with {password} / {basic}")
        return httpx.Response(
            401,
            text=f"bad credentials {password} {request.headers.get('authorization', '')}",
            headers={"WWW-Authenticate": f'Basic realm="{password}"', "X-Echo": password},
        )

    return httpx.MockTransport(handler)


@pytest.fixture(scope="module")
def app():
    mp = pytest.MonkeyPatch()
    mp.setenv("WORKBENCH_PHASE", "2")
    importlib.reload(app_module)
    application = app_module.create_app()
    application.dependency_overrides[current_access] = lambda: PERSON

    async def _noop(*_a: Any, **_k: Any) -> None:
        return None

    async def _health(*_a: Any, **_k: Any) -> dict[str, Any]:
        raise StoreUnavailable("opencode", "http 504")

    mp.setattr(AuditLog, "record", _noop)
    mp.setattr(workbench, "store_health", _health)
    mp.setattr(workbench, "UsageRecorder", _FakeUsage)
    try:
        yield application
    finally:
        mp.undo()
        os.environ.pop("WORKBENCH_PHASE", None)
        importlib.reload(app_module)


def _needles(password: str) -> list[str]:
    basic = base64.b64encode(f"opencode:{password}".encode()).decode()
    return [password, basic, base64.b64encode(password.encode()).decode()]


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.lines: list[str] = []
        self.setFormatter(logging.Formatter("%(name)s %(levelname)s %(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(self.format(record))


def _exercise(app: FastAPI, password: str, mode: str, configured: bool) -> list[str]:
    """Hit every route; return all response text, headers and log output."""
    saved = {k: os.environ.get(k) for k in (PASSWORD_ENV, URL_ENV)}
    os.environ[PASSWORD_ENV] = password
    if configured:
        os.environ[URL_ENV] = OPENCODE_URL
        app.state.workbench_sessions = _FakeSessions(
            OpenCodeClient(OPENCODE_URL, password, transport=_transport(mode, password))
        )
    else:
        os.environ.pop(URL_ENV, None)  # _sessions() answers opencode_unavailable
        app.state.workbench_sessions = None
    app.state.workbench_session_factory = object()
    app.state.workbench_projects = _FailingStore()
    app.state.workbench_files = _FailingStore()

    handler = _Capture()
    root = logging.getLogger()
    old_level = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    out, err = io.StringIO(), io.StringIO()
    seen: list[str] = []
    statuses: set[int] = set()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            with TestClient(app, raise_server_exceptions=False) as client:
                for method, path, body in REQUESTS:
                    kwargs: dict[str, Any] = {"headers": HEADER}
                    if body is not None:
                        kwargs["json"] = body
                    resp = client.request(method, _url(path), **kwargs)
                    statuses.add(resp.status_code)
                    seen.append(resp.text)
                    seen.append(str(resp.url))
                    seen.extend(f"{k}: {v}" for k, v in resp.headers.items())
    finally:
        root.removeHandler(handler)
        root.setLevel(old_level)
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        app.state.workbench_sessions = None
    assert statuses & {503, 504, 502}, statuses  # the error paths really ran
    return [*seen, *handler.lines, out.getvalue(), err.getvalue()]


def test_request_table_covers_every_workbench_route(app: FastAPI) -> None:
    methods = {"get", "post", "put", "patch", "delete"}
    routes = {
        (m.upper(), p)
        for p, ops in app.openapi()["paths"].items()
        if p.startswith("/api/workbench")
        for m in ops
        if m in methods
    }
    covered = {(m, p.split("?")[0]) for m, p, _ in REQUESTS}
    assert routes == covered


@settings(
    max_examples=15,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    password=passwords,
    mode=st.sampled_from(["down", "rejects"]),
    configured=st.booleans(),
)
def test_random_password_never_disclosed(
    app: FastAPI, password: str, mode: str, configured: bool
) -> None:
    """**Validates: Requirements 6.5**"""
    haystack = "\n".join(_exercise(app, password, mode, configured))
    for needle in _needles(password):
        assert needle not in haystack


@pytest.mark.parametrize("mode", ["down", "rejects"])
@pytest.mark.parametrize("configured", [True, False])
def test_fixed_password_absent_from_every_endpoint_response_and_log(
    app: FastAPI, mode: str, configured: bool
) -> None:
    """Regression with a canned password across all endpoints. Req 6.5."""
    parts = _exercise(app, FIXED_PASSWORD, mode, configured)
    assert any("opencode_unavailable" in p for p in parts) or not configured
    for needle in _needles(FIXED_PASSWORD):
        for part in parts:
            assert needle not in part
