"""Coding handoffs (NS-ADR-106): routing, claims, Mac pull API, central opencode executor."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from backend.api.endpoints import handoffs as handoffs_ep
from backend.handoffs.central import (
    CentralExecutor,
    OpenCodeClient,
    OpenCodeError,
    build_executor,
    last_assistant_text,
    workspace_dir,
)
from backend.handoffs.service import HandoffService, HandoffStateError, route
from backend.persistence.connection import make_session_factory
from backend.persistence.models import CodingHandoff

TOKEN = "device-token-" + "x" * 30


@pytest.fixture
async def service():
    eng = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    async with eng.begin() as conn:
        await conn.run_sync(CodingHandoff.__table__.create)
    yield HandoffService(make_session_factory(eng))
    await eng.dispose()


class FakePlane:
    registry_configured = True

    async def relay_verify_token(self, *, device_id: str, token: str) -> bool:
        return device_id == "mac-1" and token == TOKEN


def make_app(service: HandoffService) -> FastAPI:
    app = FastAPI()
    app.include_router(handoffs_ep.router)
    app.state.handoff_service = service
    app.state.connection_control_plane = FakePlane()
    return app


def client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


DEV = {"X-Nerve-Device": "mac-1", "Authorization": f"Bearer {TOKEN}"}


# ---- routing ----------------------------------------------------------------


def test_routing_by_target_repo_and_capability():
    assert route("menagerie", None) == "mac"
    assert route("opencode", "apple") == "mac"
    assert route("opencode", "celesrenata/menagerie.git") == "mac"
    assert route("opencode", "nervcenter", ["swift"]) == "mac"
    assert route("opencode", "nervcenter") == "central"
    assert route("opencode", None) == "central"
    assert route("opencode", "tools", env={"NERVE_HANDOFF_MAC_REPOS": "tools"}) == "mac"


# ---- service ----------------------------------------------------------------


async def test_enqueue_claim_finish_and_single_claim(service):
    a = await service.enqueue(target="opencode", title="A", brief="do a", repo="nervcenter")
    b = await service.enqueue(target="menagerie", title="B", brief="do b")
    assert (a["runner"], a["status"], a["push_approved"]) == ("central", "queued", False)
    assert b["runner"] == "mac"
    first, second = await asyncio.gather(
        service.claim_next("central", "w1"), service.claim_next("central", "w2")
    )
    claimed = [x for x in (first, second) if x]
    assert len(claimed) == 1 and claimed[0]["id"] == a["id"]
    assert await service.claim_next("central", "w3") is None
    done = await service.finish(a["id"], ok=True, summary="ok", claimer=claimed[0]["claimed_by"])
    assert done["status"] == "done" and done["result_summary"] == "ok"
    with pytest.raises(HandoffStateError):
        await service.finish(a["id"], ok=True)


async def test_menagerie_cannot_be_forced_central_and_bad_repo(service):
    from backend.handoffs.service import HandoffInputError

    with pytest.raises(HandoffInputError):
        await service.enqueue(target="menagerie", title="x", brief="y", runner="central")
    with pytest.raises(HandoffInputError):
        await service.enqueue(target="opencode", title="x", brief="y", repo="../etc")


# ---- owner API --------------------------------------------------------------


async def test_owner_api_queue_approve_cancel_retry(service):
    async with client(make_app(service)) as c:
        r = await c.post(
            "/api/handoffs",
            json={"target": "opencode", "title": "T", "brief": "B", "repo": "nervcenter"},
        )
        assert r.status_code == 201
        hid = r.json()["id"]
        # agents cannot grant push through the enqueue body
        r = await c.post(
            "/api/handoffs",
            json={"target": "opencode", "title": "T", "brief": "B", "push_approved": True},
        )
        assert r.status_code == 422
        assert (await c.post(f"/api/handoffs/{hid}/approve-push")).json()["push_approved"] is True
        assert (await c.post(f"/api/handoffs/{hid}/cancel")).json()["status"] == "cancelled"
        assert (await c.post(f"/api/handoffs/{hid}/retry")).json()["status"] == "queued"
        assert len((await c.get("/api/handoffs")).json()["handoffs"]) == 1
        status = (await c.get("/api/handoffs/status")).json()
        assert status["mac"]["pull_path_enabled"] is True
        assert (await c.get("/api/handoffs/nope")).status_code == 404


# ---- Mac pull API -----------------------------------------------------------


async def test_device_routes_require_the_relay_token_with_one_generic_401(service):
    await service.enqueue(target="menagerie", title="M", brief="b")
    async with client(make_app(service)) as c:
        bodies = set()
        for headers in (
            {},
            {"X-Nerve-Device": "mac-1"},
            {"X-Nerve-Device": "mac-1", "Authorization": "Bearer wrong"},
            {"X-Nerve-Device": "other", "Authorization": f"Bearer {TOKEN}"},
        ):
            r = await c.post("/api/handoffs/device/claim", headers=headers)
            assert r.status_code == 401
            bodies.add(r.text)
        assert len(bodies) == 1
    assert (await service.list())[0]["status"] == "queued"  # nothing claimed


async def test_device_claim_and_result_roundtrip(service):
    await service.enqueue(target="opencode", title="central", brief="b", repo="nervcenter")
    m = await service.enqueue(target="menagerie", title="M", brief="build the app")
    async with client(make_app(service)) as c:
        job = (await c.post("/api/handoffs/device/claim", headers=DEV)).json()["handoff"]
        assert (
            job["id"] == m["id"]
            and job["brief"] == "build the app"
            and job["push_approved"] is False
        )
        assert (await c.post("/api/handoffs/device/claim", headers=DEV)).json()["handoff"] is None
        r = await c.post(
            "/api/handoffs/device/result",
            headers=DEV,
            content=json.dumps({"handoff_id": m["id"], "ok": True, "summary": "built"}),
        )
        assert r.json() == {"id": m["id"], "status": "done"}
        big = json.dumps({"handoff_id": m["id"], "ok": True, "summary": "x" * 70_000})
        assert (
            await c.post("/api/handoffs/device/result", headers=DEV, content=big)
        ).status_code == 413
    row = await service.get(m["id"])
    assert row["claimed_by"] == "mac:mac-1" and row["result_summary"] == "built"


async def test_device_cannot_finish_a_central_job(service):
    c_job = await service.enqueue(target="opencode", title="central", brief="b")
    await service.claim_next("central", "central-opencode")
    async with client(make_app(service)) as c:
        r = await c.post(
            "/api/handoffs/device/result",
            headers=DEV,
            content=json.dumps({"handoff_id": c_job["id"], "ok": True, "summary": "spoof"}),
        )
        assert r.status_code == 404
    assert (await service.get(c_job["id"]))["status"] == "claimed"


# ---- central executor -------------------------------------------------------


class FakeOpenCode:
    def __init__(self, fail_on: str | None = None) -> None:
        self.calls: list[tuple[str, str, Any]] = []
        self.fail_on = fail_on

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.method, request.url.path, body))
        self.auth = request.headers.get("authorization")
        if self.fail_on and self.fail_on in request.url.path:
            return httpx.Response(500)
        if request.url.path == "/api/session":
            return httpx.Response(200, json={"data": {"id": "ses_1"}})
        if request.url.path.endswith("/prompt"):
            return httpx.Response(200, json={"data": {}})
        if request.url.path.endswith("/wait"):
            return httpx.Response(204)
        if request.url.path.endswith("/message"):
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "type": "assistant",
                            "content": [{"type": "text", "text": "Changed X; tests pass."}],
                        },
                        {"type": "user", "content": []},
                    ],
                    "cursor": {},
                },
            )
        return httpx.Response(404)


async def _run_central(service, fake, approve=False, project=None):
    job = await service.enqueue(
        target="opencode", title="Fix", brief="fix it", repo="nervcenter", project_slug=project
    )
    if approve:
        await service.approve_push(job["id"])

    class Ctx:
        async def context_text(self, slug):
            return f"context for {slug}"

    ex = CentralExecutor(
        service,
        OpenCodeClient("http://oc:4096", "pw", transport=httpx.MockTransport(fake)),
        workspaces="/workspaces",
        context=Ctx(),
    )
    await ex.tick()
    await ex._task
    return await service.get(job["id"])


async def test_central_runs_session_denies_push_and_records_summary(service):
    fake = FakeOpenCode()
    row = await _run_central(service, fake, project="home-lab")
    assert row["status"] == "done" and row["result_summary"] == "Changed X; tests pass."
    assert row["session_ref"] == "ses_1" and row["claimed_by"] == "central-opencode"
    create = fake.calls[0][2]
    assert create["location"] == {"directory": "/workspaces/nervcenter"}
    assert {"action": "shell", "resource": "git push *", "effect": "deny"} in create["permissions"]
    prompt = fake.calls[1][2]["text"]
    assert "Do not push" in prompt and "context for home-lab" in prompt
    assert fake.auth.startswith("Basic ")


async def test_central_allows_push_only_when_owner_approved(service):
    fake = FakeOpenCode()
    await _run_central(service, fake, approve=True)
    perms = fake.calls[0][2]["permissions"]
    assert not any(p["resource"] == "git push *" for p in perms)


async def test_central_failure_is_recorded_without_the_password(service):
    row = await _run_central(service, FakeOpenCode(fail_on="/prompt"))
    assert row["status"] == "failed" and "HTTP 500" in row["error"] and "pw" not in row["error"]


def test_helpers():
    assert workspace_dir("/workspaces", "a/b") == "/workspaces/a/b"
    with pytest.raises(OpenCodeError):
        workspace_dir("/workspaces", "../x")
    assert last_assistant_text([{"type": "assistant", "content": [{"type": "reasoning"}]}]) == ""
    assert build_executor(object(), env={}) is None  # not configured -> off


async def test_mcp_tool_posts_to_handoffs():
    from mcp.api import NerveAPI
    from mcp.tools_module import build_tools

    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"], seen["body"] = request.url.path, json.loads(request.content)
        return httpx.Response(201, json={"id": "h1"})

    tool = next(t for t in build_tools("http://unit") if t.name == "enqueue_coding_handoff")
    api = NerveAPI("http://unit", transport=httpx.MockTransport(handler))
    await tool.handler({"target": "opencode", "title": "t", "brief": "b"}, api)
    assert seen["path"] == "/api/handoffs" and seen["body"]["target"] == "opencode"


def test_handoff_ingress_is_exact_paths_with_limits():
    from pathlib import Path

    import yaml

    path = Path(__file__).resolve().parents[2] / "deployments/kubernetes/base/relay-ingress.yaml"
    docs = [d for d in yaml.safe_load_all(path.read_text()) if d]
    ing = next(d for d in docs if d["kind"] == "Ingress" and d["metadata"]["name"] == "nervcenter-handoff")  # noqa: E501
    paths = ing["spec"]["rules"][0]["http"]["paths"]
    assert {(p["path"], p["pathType"]) for p in paths} == {
        ("/api/handoffs/device/claim", "Exact"),
        ("/api/handoffs/device/result", "Exact"),
    }
    mw = ing["metadata"]["annotations"]["traefik.ingress.kubernetes.io/router.middlewares"]
    assert "handoff-body-limit" in mw and "handoff-rate-limit" in mw
    by = {d["metadata"]["name"]: d for d in docs if d["kind"] == "Middleware"}
    assert by["handoff-body-limit"]["spec"]["buffering"]["maxRequestBodyBytes"] == 65536
    assert by["handoff-rate-limit"]["spec"]["rateLimit"] == {"average": 2, "burst": 10}
