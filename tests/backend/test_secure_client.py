"""Secure model calls (NS-ADR-108): secure combo, no-memory headers, route guard, every path."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import httpx
import pytest
import structlog
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from backend.inference import secure_client
from backend.inference.omniroute import OmniRouteTransport
from backend.inference.openai_engine import OpenAIEngine, OpenAIError
from backend.inference.openai_limiter import OpenAILimiter, OpenAILimits
from backend.inference.secure_client import (
    SECURE_HEADERS,
    SecureRouteDenied,
    SecureTransport,
    secure_model,
)
from backend.mail_security.route_guard import LocalOnlyRouteResolver

ROOT = Path(__file__).resolve().parents[2]
BASE = "http://omniroute.omniroute.svc:20128"


# ---- fakes --------------------------------------------------------------------


class Registry:
    """Registry: secure/planner local; secure/leaky has a cloud leaf; local/any unflagged."""

    def __init__(self) -> None:
        combos = [
            {
                "name": "secure/planner",
                "strategy": "priority",
                "config": {"secure": True},
                "models": [{"kind": "combo-ref", "comboName": "pool/tier1/planner"}],
            },
            {
                "name": "pool/tier1/planner",
                "strategy": "priority",
                "models": ["llama-cpp/deepseek-v41"],
            },
            {
                "name": "secure/leaky",
                "strategy": "priority",
                "config": {"secure": True},
                "models": ["llama-cpp/deepseek-v41", "openai/gpt-5.6-sol"],
            },
            {"name": "local/any", "strategy": "priority", "models": ["llama-cpp/deepseek-v41"]},
        ]
        self.docs = {
            "/v1/models": {"data": [{"id": c["name"]} for c in combos]},
            "/api/combos": {"combos": combos},
            "/api/providers": {
                "connections": [
                    {"provider": "llama-cpp", "baseUrl": "http://192.168.133.2:7777"},
                    {"provider": "openai", "baseUrl": "https://api.openai.com/v1"},
                ]
            },
        }
        self.calls = 0

    async def get_json(self, path: str) -> Any:
        self.calls += 1
        return self.docs[path]


def real_guard(registry: Registry | None = None):
    reg = registry or Registry()
    return LocalOnlyRouteResolver(
        reg, omniroute_base_url=BASE, require_secure_combo=True
    ).resolve, reg


class Inner:
    base_url = BASE
    api_key = "k"
    fixture_mode = False

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def generate(self, model, prompt, parameters=None, timeout=None, headers=None, **kw):
        self.calls.append({"model": model, "prompt": prompt, "headers": headers})
        return {"content": "ok", "input_tokens": 1, "output_tokens": 1, "e2e_latency_ms": 5.0}


def wire_client(seen: list[httpx.Request]) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "hi"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )

    return httpx.Client(transport=httpx.MockTransport(handler))


# ---- the helper -----------------------------------------------------------------


def test_secure_model_mapping():
    assert secure_model("secure/reader") == "secure/reader"
    for m in ("auto/best-chat", "local/any", None, "nerve-ask"):
        assert secure_model(m) == "secure/planner"
    assert secure_model("x", env={"NERVE_SECURE_MODEL": "secure/coder"}) == "secure/coder"
    assert secure_model("x", env={"NERVE_SECURE_MODEL": "local/any"}) == "secure/planner"


def test_headers_on_the_wire_and_guard_runs_every_call():
    seen: list[httpx.Request] = []
    guard, reg = real_guard()
    inner = OmniRouteTransport(base_url=BASE, api_key="k", _client=wire_client(seen))
    t = SecureTransport(inner, "test", guard)
    t.generate("auto/best-chat", "personal text")
    t.generate("secure/planner", "personal text")
    assert len(seen) == 2 and reg.calls == 6  # three registry reads per call, no cache
    for req in seen:
        assert req.headers["x-omniroute-no-memory"] == "true"
        assert req.headers["x-omniroute-compression"] == "off"
        assert json.loads(req.content)["model"] == "secure/planner"


@pytest.mark.parametrize("model,code", [("secure/leaky", "LEAF_NON_LOCAL"), ("local/any", None)])
def test_guard_denies_cloud_leaf_and_unflagged_combo_and_sends_nothing(model, code, monkeypatch):
    monkeypatch.setenv("NERVE_SECURE_MODEL", "secure/leaky")
    guard, _ = real_guard()
    inner = Inner()
    with pytest.raises(SecureRouteDenied) as info:
        SecureTransport(inner, "test", guard).generate(model, "personal text")
    assert inner.calls == []
    if code:
        assert code in info.value.reason


def test_guard_errors_fail_closed_and_logs_have_no_content():
    async def broken(_alias: str):
        raise RuntimeError("secret registry text")

    inner = Inner()
    with structlog.testing.capture_logs() as logs:
        with pytest.raises(SecureRouteDenied) as info:
            SecureTransport(inner, "test", broken).generate("secure/planner", "PERSONAL-SENTINEL")
    assert info.value.reason == "GUARD_ERROR" and inner.calls == []
    assert "PERSONAL-SENTINEL" not in json.dumps(logs) and "secret registry" not in json.dumps(logs)


def test_unconfigured_omniroute_is_denied_by_the_default_guard():
    inner = Inner()
    inner.base_url = None
    with pytest.raises(SecureRouteDenied):
        SecureTransport(inner, "test").generate("secure/planner", "p")
    assert inner.calls == []


def test_only_explicit_non_sensitive_skips_secure():
    guard, reg = real_guard()
    inner = Inner()
    SecureTransport(inner, "test", guard).generate("auto/best-chat", "public", sensitive=False)
    assert (
        inner.calls[0]["model"] == "local/any"
        and inner.calls[0]["headers"] is None
        and reg.calls == 0
    )


def test_guard_works_from_inside_a_running_loop():
    guard, _ = real_guard()
    inner = Inner()

    async def main():
        return SecureTransport(inner, "test", guard).generate("secure/planner", "p")

    import asyncio

    assert asyncio.run(main())["content"] == "ok"


# ---- every path ------------------------------------------------------------------


@pytest.fixture
def patched(monkeypatch):
    """Route every secure_transport() to a fake inner + the real guard on a fake registry."""
    inner = Inner()
    guard, reg = real_guard()
    monkeypatch.setattr(OmniRouteTransport, "from_env", classmethod(lambda cls: inner))
    monkeypatch.setattr(secure_client, "build_guard", lambda *_a: guard)
    return inner, reg


def _assert_secure(call: dict[str, Any], model: str = "secure/planner") -> None:
    assert call["model"] == model and call["headers"] == SECURE_HEADERS


async def test_researcher_path(patched):
    from backend.research.engine import COMBO, build_engine

    inner, reg = patched
    app = FastAPI()
    app.state.db_engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    engine = build_engine(app)
    engine._transport.generate(COMBO, "research prompt")
    _assert_secure(inner.calls[0])
    assert reg.calls == 3
    await app.state.db_engine.dispose()


async def test_agent_runner_path_default_and_high_stakes_escalation(patched, monkeypatch):
    from backend.agents.runner import build_runner
    from backend.persistence.models import EscalationGrant, OpenAICall

    inner, reg = patched
    eng = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    from backend.persistence.models.incidents_agents import Agent as AgentORM

    async with eng.begin() as conn:
        for m in (AgentORM, EscalationGrant, OpenAICall):
            await conn.run_sync(m.__table__.create)

    class A:
        id = "a1"

    runner = build_runner(eng)
    await runner.infer(A(), "agent prompt")
    _assert_secure(inner.calls[0])

    # high stakes: local secure attempts fail, then OpenAI through the shared limiter
    app = FastAPI()
    from backend.inference.escalation import GrantStore
    from backend.persistence.connection import make_session_factory

    app.state.escalation_grants = GrantStore(make_session_factory(eng))
    await app.state.escalation_grants.set("agent", "a1", "high_stakes")
    seen: list[Any] = []

    def oa(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "o"}}], "usage": {}})

    limiter = OpenAILimiter(limits=OpenAILimits(min_spacing_s=0))
    app.state.openai_limiter = limiter
    app.state.openai_engine = OpenAIEngine(
        None, env={"NERVE_OPENAI_KEY": "k" * 20}, transport=httpx.MockTransport(oa), limiter=limiter
    )
    inner.calls.clear()

    def fail(*a, **k):
        inner.calls.append({"model": a[0], "headers": k.get("headers")})
        raise RuntimeError("local down")

    monkeypatch.setattr(inner, "generate", fail)
    runner2 = build_runner(eng, app)
    with structlog.testing.capture_logs() as logs:
        out = await runner2.infer(A(), "AGENT-PROMPT-SENTINEL")
    assert out["content"] == "o" and len(inner.calls) == 2
    for c in inner.calls:
        _assert_secure(c)
    assert seen and (await limiter.usage())["today"]["requests"] == 1
    esc = [
        e
        for e in logs
        if e["event"] in ("inference.local_first.escalate", "openai.limiter.call_ok")
    ]
    assert esc and all(e.get("reason") == "high_stakes" for e in esc)
    assert "AGENT-PROMPT-SENTINEL" not in json.dumps(logs, default=str)
    await eng.dispose()


async def test_project_ask_path(patched):
    from fastapi.testclient import TestClient

    from backend.api.endpoints import projects as projects_ep
    from backend.persistence.connection import make_session_factory
    from backend.persistence.models import ProjectContext, ProjectContextNote
    from backend.projects.service import ProjectService

    inner, _ = patched
    eng = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    async with eng.begin() as conn:
        for m in (ProjectContext, ProjectContextNote):
            await conn.run_sync(m.__table__.create)
    svc = ProjectService(make_session_factory(eng))
    await svc.create("p", "P", "private summary")
    app = FastAPI()
    app.include_router(projects_ep.router)
    app.state.project_service = svc
    app.state.openai_engine = OpenAIEngine(svc, env={})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/api/projects/p/ask", json={"prompt": "q"})
    assert r.json()["model"] == "secure/planner"
    _assert_secure(inner.calls[0])
    assert "private summary" in inner.calls[0]["prompt"]
    await eng.dispose()
    del TestClient


def test_context_ask_wiring_uses_the_secure_client(patched):
    from backend.api.app import create_app

    app = create_app()
    ask = app.state.ask_service
    assert isinstance(ask._transport, SecureTransport)


# ---- OpenAI: minimum context, never mental health -----------------------------------


async def test_openai_refuses_mental_health_data_and_logs_reason_only():
    seen: list[Any] = []
    eng = OpenAIEngine(
        None,
        env={"NERVE_OPENAI_KEY": "k" * 20},
        transport=httpx.MockTransport(lambda r: seen.append(r) or httpx.Response(200)),
    )
    with structlog.testing.capture_logs() as logs:
        with pytest.raises(OpenAIError):
            await eng.generate(
                "PHQ9-SENTINEL",
                caller="x",
                reason="owner_requested",
                data_classes=frozenset({"mental_health"}),
            )
    assert seen == [] and "PHQ9-SENTINEL" not in json.dumps(logs)


def test_mental_health_finance_and_health_never_reach_a_model():
    pattern = re.compile(r"^\s*(from|import)\s+(backend\.inference|httpx)\b.*$", re.M)
    for pkg in ("mental_health", "finance", "bills", "wellness"):
        for f in (ROOT / "backend" / pkg).rglob("*.py"):
            assert not pattern.search(f.read_text()), f"{f} imports a model client"


def test_every_omniroute_generate_call_site_is_wrapped():
    """Each backend file that builds an OmniRoute transport for generation wraps it securely."""
    allowed = {
        "backend/inference/omniroute.py",
        "backend/inference/__init__.py",
        "backend/inference/secure_client.py",
        "backend/context/ask.py",
    }
    for f in (ROOT / "backend").rglob("*.py"):
        rel = f.relative_to(ROOT).as_posix()
        if rel in allowed or "__pycache__" in rel:
            continue
        src = f.read_text()
        if re.search(r"OmniRouteTransport(\.from_env)?\(", src):
            assert "secure_transport(" in src, rel
        assert "LocalFirstTransport(" not in src or rel == "backend/inference/local_first.py", rel
