"""Project context store, restricted MCP endpoint, auth verifiers, OpenAI engine (NS-ADR-104)."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import structlog
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from backend.api.endpoints import projects as projects_ep
from backend.inference.openai_engine import (
    OpenAIEngine,
    OpenAIError,
    OpenAINotConfigured,
)
from backend.inference.providers import openai_engine_info
from backend.persistence.connection import make_session_factory
from backend.persistence.models import ProjectContext, ProjectContextNote
from backend.projects import mcp_server
from backend.projects.auth import BearerTokenVerifier, OidcVerifier, build_verifiers
from backend.projects.service import ProjectInputError, ProjectNotFoundError, ProjectService

TOKEN = "t" * 40
OTHER = "o" * 40


@pytest.fixture
async def env():
    eng = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    async with eng.begin() as conn:
        for model in (ProjectContext, ProjectContextNote):
            await conn.run_sync(model.__table__.create)
    factory = make_session_factory(eng)
    yield factory
    await eng.dispose()


@pytest.fixture
async def service(env) -> ProjectService:
    return ProjectService(env)


def make_app(service: ProjectService, verifiers=None, limiter=None) -> FastAPI:
    app = FastAPI()
    app.include_router(projects_ep.router)
    app.include_router(mcp_server.router)
    app.state.project_service = service
    app.state.mcp_project_verifiers = (
        verifiers
        if verifiers is not None
        else [BearerTokenVerifier({"chatgpt": TOKEN, "openai": OTHER})]
    )
    if limiter is not None:
        app.state.mcp_project_limiter = limiter
    return app


def client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


def rpc(method: str, params: dict[str, Any] | None = None, rid: int = 1) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}}


def auth(token: str = TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ---- service ----------------------------------------------------------------


async def test_service_notes_are_append_only_and_ordered(service):
    await service.create("home-lab", "Home lab", "Rack plans")
    await service.add_note("home-lab", "owner", "first")
    await service.add_note("home-lab", "agent", "second")
    data = await service.get("home-lab")
    assert [(n["author"], n["body"]) for n in data["notes"]] == [
        ("owner", "first"),
        ("agent", "second"),
    ]
    assert not hasattr(service, "delete_note") and not hasattr(service, "update_note")
    assert (await service.list_projects())[0]["note_count"] == 2
    text = await service.context_text("home-lab")
    assert "Rack plans" in text and "[agent] second" in text


async def test_service_validation(service):
    with pytest.raises(ProjectInputError):
        await service.create("Bad Slug", "x")
    await service.create("p", "P")
    with pytest.raises(ProjectInputError):
        await service.add_note("p", "Bad Author!", "x")
    with pytest.raises(ProjectInputError):
        await service.add_note("p", "owner", "  ")
    with pytest.raises(ProjectNotFoundError):
        await service.get("missing")


# ---- REST -------------------------------------------------------------------


async def test_rest_create_note_detail_and_conflicts(service):
    async with client(make_app(service)) as c:
        assert (await c.post("/api/projects", json={"slug": "a", "title": "A"})).status_code == 201
        assert (await c.post("/api/projects", json={"slug": "a", "title": "A"})).status_code == 409
        assert (await c.post("/api/projects/a/notes", json={"body": "hi"})).status_code == 201
        d = (await c.get("/api/projects/a")).json()
        assert d["notes"][0]["author"] == "owner"
        assert (await c.get("/api/projects/nope")).status_code == 404
        assert (await c.patch("/api/projects/a", json={"summary": "S"})).json()["summary"] == "S"
        assert (
            await c.post("/api/projects", json={"slug": "BAD", "title": "A"})
        ).status_code == 422


# ---- MCP: auth --------------------------------------------------------------


async def test_mcp_generic_401_for_missing_wrong_and_unconfigured(service):
    cases = [
        (make_app(service), {}),
        (make_app(service), auth("w" * 40)),
        (make_app(service), {"Authorization": "Basic abc"}),
        (make_app(service, verifiers=[]), auth()),  # nothing configured: fail closed
    ]
    bodies = set()
    for app, headers in cases:
        async with client(app) as c:
            r = await c.post("/api/mcp/projects", json=rpc("ping"), headers=headers)
        assert r.status_code == 401
        bodies.add(r.text)
    assert len(bodies) == 1  # same body whatever failed
    assert (await service.list_projects()) == []


async def test_401_before_any_body_is_read_or_stored(service):
    async with client(make_app(service)) as c:
        r = await c.post(
            "/api/mcp/projects",
            content=b"{not json",
            headers={"Authorization": "Bearer nope"},
        )
    assert r.status_code == 401


def test_bearer_config_parsing_and_short_tokens_ignored():
    v = BearerTokenVerifier.from_env(
        {"NERVE_MCP_PROJECTS_TOKENS": f"chatgpt:{TOKEN},bad:short,Bad Name:{OTHER}"}
    )
    assert [c for c, _ in v._entries] == ["chatgpt"]
    assert build_verifiers({}) == []


# ---- MCP: tools -------------------------------------------------------------


async def test_mcp_lists_exactly_three_tools_and_nothing_else(service):
    async with client(make_app(service)) as c:
        r = await c.post("/api/mcp/projects", json=rpc("tools/list"), headers=auth())
        tools = {t["name"] for t in r.json()["result"]["tools"]}
        assert tools == {"list_projects", "get_project_context", "add_project_note"}
        for name in ("delete_project", "projects.add_note", "shell"):
            r = await c.post(
                "/api/mcp/projects",
                json=rpc("tools/call", {"name": name, "arguments": {}}),
                headers=auth(),
            )
            assert r.json()["result"]["isError"] is True
        r = await c.post("/api/mcp/projects", json=rpc("resources/list"), headers=auth())
        assert r.json()["error"]["code"] == -32601


async def test_note_written_via_mcp_is_tagged_with_the_client_not_the_caller(service):
    await service.create("p", "P")
    async with client(make_app(service)) as c:
        r = await c.post(
            "/api/mcp/projects",
            json=rpc(
                "tools/call",
                {"name": "add_project_note", "arguments": {"slug": "p", "body": "from chatgpt"}},
            ),
            headers=auth(),
        )
        assert r.json()["result"]["isError"] is False
        # a spoofed author argument is rejected
        r = await c.post(
            "/api/mcp/projects",
            json=rpc(
                "tools/call",
                {
                    "name": "add_project_note",
                    "arguments": {"slug": "p", "body": "x", "author": "owner"},
                },
            ),
            headers=auth(),
        )
        assert r.json()["result"]["isError"] is True
        await c.post(
            "/api/mcp/projects",
            json=rpc(
                "tools/call",
                {"name": "add_project_note", "arguments": {"slug": "p", "body": "from api"}},
            ),
            headers=auth(OTHER),
        )
        got = (
            await c.post(
                "/api/mcp/projects",
                json=rpc("tools/call", {"name": "get_project_context", "arguments": {"slug": "p"}}),
                headers=auth(),
            )
        ).json()["result"]["structuredContent"]
    assert [(n["author"], n["body"]) for n in got["notes"]] == [
        ("chatgpt", "from chatgpt"),
        ("openai", "from api"),
    ]
    data = await service.get("p")
    assert [n["author"] for n in data["notes"]] == ["chatgpt", "openai"]


async def test_mcp_list_and_missing_project(service):
    await service.create("p", "P")
    async with client(make_app(service)) as c:
        r = await c.post(
            "/api/mcp/projects",
            json=rpc("tools/call", {"name": "list_projects", "arguments": {}}),
            headers=auth(),
        )
        assert r.json()["result"]["structuredContent"]["projects"][0]["slug"] == "p"
        r = await c.post(
            "/api/mcp/projects",
            json=rpc("tools/call", {"name": "get_project_context", "arguments": {"slug": "zzz"}}),
            headers=auth(),
        )
        assert r.json()["result"]["isError"] is True
        r = await c.post(
            "/api/mcp/projects",
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            headers=auth(),
        )
        assert r.status_code == 204


async def test_body_cap_and_rate_limit(service):
    limiter = mcp_server.RateLimiter(rate=0.0001, burst=2)
    async with client(make_app(service, limiter=limiter)) as c:
        big = json.dumps({"x": "a" * (mcp_server.MAX_BODY_BYTES + 10)}).encode()
        r = await c.post("/api/mcp/projects", content=big, headers=auth())
        assert r.status_code == 413
        assert (
            await c.post("/api/mcp/projects", json=rpc("ping"), headers=auth())
        ).status_code == 200
        assert (
            await c.post("/api/mcp/projects", json=rpc("ping"), headers=auth())
        ).status_code == 200
        assert (
            await c.post("/api/mcp/projects", json=rpc("ping"), headers=auth())
        ).status_code == 429
        # limit is per client: the other token still passes
        assert (
            await c.post("/api/mcp/projects", json=rpc("ping"), headers=auth(OTHER))
        ).status_code == 200


async def test_audit_log_has_tool_project_outcome_but_no_content(service):
    await service.create("p", "P")
    secret = "SECRET-NOTE-TEXT-123"
    with structlog.testing.capture_logs() as logs:
        async with client(make_app(service)) as c:
            await c.post(
                "/api/mcp/projects",
                json=rpc(
                    "tools/call",
                    {"name": "add_project_note", "arguments": {"slug": "p", "body": secret}},
                ),
                headers=auth(),
            )
    calls = [e for e in logs if e["event"] == "mcp.projects.call"]
    assert calls and calls[0]["tool"] == "add_project_note" and calls[0]["project"] == "p"
    assert calls[0]["outcome"] == "ok" and calls[0]["client"] == "chatgpt"
    assert secret not in json.dumps(logs, default=str) and TOKEN not in json.dumps(
        logs, default=str
    )


# ---- OIDC verifier ----------------------------------------------------------


def _oidc_fixture():
    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    jwk.update({"kid": "k1", "alg": "RS256", "use": "sig"})
    return jwt, key, {"keys": [jwk]}


def _token(jwt, key, **claims):
    import time

    base = {
        "iss": "https://sso.example/realms/r",
        "aud": "nerve-projects",
        "exp": int(time.time()) + 300,
    }
    base.update(claims)
    return jwt.encode(base, key, algorithm="RS256", headers={"kid": "k1"})


async def test_oidc_accepts_valid_and_rejects_bad_issuer_audience_expiry_signature():
    jwt, key, jwks = _oidc_fixture()
    fetches: list[str] = []

    def fetch(uri: str):
        fetches.append(uri)
        return jwks

    v = OidcVerifier(
        "https://sso.example/realms/r",
        "nerve-projects",
        "https://sso.example/jwks",
        fetch_jwks=fetch,
    )
    ok = await v.verify(_token(jwt, key))
    assert ok is not None and ok.client == "chatgpt" and ok.verifier == "oidc"
    assert await v.verify(_token(jwt, key, iss="https://evil")) is None
    assert await v.verify(_token(jwt, key, aud="other")) is None
    assert await v.verify(_token(jwt, key, exp=1)) is None
    assert await v.verify("garbage") is None
    from cryptography.hazmat.primitives.asymmetric import rsa

    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    assert await v.verify(_token(jwt, other)) is None
    assert len(fetches) <= 3  # cached; only refetched for unknown key ids


def test_oidc_is_off_by_default_and_needs_issuer_and_audience():
    assert OidcVerifier.from_env({}) is None
    assert OidcVerifier.from_env({"NERVE_MCP_OIDC_ENABLED": "1"}) is None
    v = OidcVerifier.from_env(
        {
            "NERVE_MCP_OIDC_ENABLED": "1",
            "NERVE_MCP_OIDC_ISSUER": "https://s/r/",
            "NERVE_MCP_OIDC_AUDIENCE": "a",
        }
    )
    assert v is not None and v.jwks_uri.endswith("/protocol/openid-connect/certs")
    assert [x.name for x in build_verifiers({})] == []


async def test_endpoint_accepts_oidc_token_through_the_same_interface(service):
    jwt, key, jwks = _oidc_fixture()
    v = OidcVerifier(
        "https://sso.example/realms/r",
        "nerve-projects",
        "u",
        client="chatgpt",
        fetch_jwks=lambda _u: jwks,
    )
    await service.create("p", "P")
    async with client(make_app(service, verifiers=[v])) as c:
        r = await c.post(
            "/api/mcp/projects",
            json=rpc(
                "tools/call", {"name": "add_project_note", "arguments": {"slug": "p", "body": "n"}}
            ),
            headers=auth(_token(jwt, key)),
        )
        assert r.json()["result"]["isError"] is False
        assert (
            await c.post("/api/mcp/projects", json=rpc("ping"), headers=auth("x.y.z"))
        ).status_code == 401


# ---- OpenAI engine ----------------------------------------------------------


def fake_openai(captured: list[dict[str, Any]], status: int = 200):
    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(
            {"auth": request.headers.get("authorization"), "body": json.loads(request.content)}
        )
        if status != 200:
            return httpx.Response(status, json={"error": {"message": "nope"}})
        return httpx.Response(
            200,
            json={
                "model": "gpt-test",
                "choices": [{"message": {"content": "answer"}}],
                "usage": {"prompt_tokens": 11, "completion_tokens": 3},
            },
        )

    return httpx.MockTransport(handler)


async def test_openai_injects_project_context_into_system_message(service):
    await service.create("p", "Home lab", "Rack plans")
    await service.add_note("p", "chatgpt", "use 10GbE")
    seen: list[dict[str, Any]] = []
    eng = OpenAIEngine(
        service,
        env={"NERVE_OPENAI_KEY": "sk-secret-key", "NERVE_OPENAI_MODEL": "gpt-test"},
        transport=fake_openai(seen),
    )
    out = await eng.generate("what next?", "p")
    assert out["content"] == "answer" and out["input_tokens"] == 11
    msgs = seen[0]["body"]["messages"]
    assert (
        msgs[0]["role"] == "system"
        and "Rack plans" in msgs[0]["content"]
        and "use 10GbE" in msgs[0]["content"]
    )
    assert msgs[1] == {"role": "user", "content": "what next?"}
    assert seen[0]["auth"] == "Bearer sk-secret-key"
    assert "sk-secret-key" not in json.dumps(eng.status())


async def test_openai_without_key_is_honest_and_makes_no_call(service):
    seen: list[dict[str, Any]] = []
    eng = OpenAIEngine(service, env={}, transport=fake_openai(seen))
    assert eng.status()["key_configured"] is False
    with pytest.raises(OpenAINotConfigured):
        await eng.generate("hi")
    assert seen == []


async def test_openai_upstream_error_does_not_leak_the_key(service):
    seen: list[dict[str, Any]] = []
    eng = OpenAIEngine(
        service, env={"NERVE_OPENAI_KEY": "sk-secret-key"}, transport=fake_openai(seen, status=401)
    )
    with pytest.raises(OpenAIError) as info:
        await eng.generate("hi")
    assert "sk-secret-key" not in str(info.value) and info.value.status == 401


class FakeLocal:
    def __init__(self, fail: int = 0) -> None:
        self.calls: list[tuple[str, str]] = []
        self.fail = fail

    def generate(self, model: str, prompt: str, *a, **k):
        self.calls.append((model, prompt))
        if len(self.calls) <= self.fail:
            raise RuntimeError("local down")
        return {"content": "local answer", "input_tokens": 1, "output_tokens": 2}


async def test_ask_goes_local_first_with_project_context(service, monkeypatch):
    await service.create("p", "P", "Rack plans")
    monkeypatch.delenv("NERVE_OPENAI_KEY", raising=False)
    app = make_app(service)
    app.state.projects_local_transport = local = FakeLocal()
    seen: list[dict[str, Any]] = []
    app.state.openai_engine = OpenAIEngine(
        service, env={"NERVE_OPENAI_KEY": "k" * 20}, transport=fake_openai(seen)
    )
    async with client(app) as c:
        r = await c.post("/api/projects/p/ask", json={"prompt": "hi"})
    assert r.status_code == 200
    assert r.json()["engine"] == "local" and r.json()["content"] == "local answer"
    assert local.calls[0][0] == "local/any" and "Rack plans" in local.calls[0][1]
    assert seen == []  # OpenAI untouched


async def test_ask_escalates_after_local_failures_and_only_when_asked(service, monkeypatch):
    await service.create("p", "P")
    app = make_app(service)
    seen: list[dict[str, Any]] = []
    app.state.openai_engine = OpenAIEngine(
        service, env={"NERVE_OPENAI_KEY": "k" * 20}, transport=fake_openai(seen)
    )
    app.state.projects_local_transport = local = FakeLocal(fail=5)
    async with client(app) as c:
        r = await c.post("/api/projects/p/ask", json={"prompt": "hi"})
        assert r.json()["engine"] == "openai" and r.json()["reason"] == "local_failed"
        assert len(local.calls) == 2  # default N = 2
        local.calls.clear()
        r = await c.post("/api/projects/p/ask", json={"prompt": "hi", "use_openai": True})
        assert r.json()["reason"] == "owner_requested" and local.calls == []
    assert len(seen) == 2


async def test_ask_openai_503_without_key(service, monkeypatch):
    await service.create("p", "P")
    monkeypatch.delenv("NERVE_OPENAI_KEY", raising=False)
    async with client(make_app(service)) as c:
        r = await c.post("/api/projects/p/ask", json={"prompt": "hi", "use_openai": True})
        assert r.status_code == 503 and "not configured" in r.json()["detail"]
        assert (await c.get("/api/projects/openai/status")).json()["key_configured"] is False


def test_provider_registry_entry_never_contains_a_key(monkeypatch):
    monkeypatch.setenv("NERVE_OPENAI_KEY", "sk-should-not-appear")
    info = openai_engine_info()
    assert info["key_configured"] is True and "sk-should-not-appear" not in json.dumps(info)


def test_routers_are_in_the_app_router_list():
    from backend.api.app import ALL_ROUTERS

    paths = {getattr(r, "path", "") for router in ALL_ROUTERS for r in router.routes}
    assert "/api/mcp/projects" in paths and "/api/projects/{slug}" in paths


def test_tunnel_token_is_accepted_as_the_openai_client():
    v = BearerTokenVerifier.from_env({"NERVE_MCP_TUNNEL_TOKEN": OTHER})
    assert [c for c, _ in v._entries] == ["openai"]
    assert BearerTokenVerifier.from_env({"NERVE_MCP_TUNNEL_TOKEN": "short"}).configured is False
