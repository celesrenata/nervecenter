"""Local-first routing, escalation reasons and the OpenAI limiter (NS-ADR-107)."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import structlog
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from backend.api.endpoints import model_policy as policy_ep
from backend.inference.escalation import GrantStore
from backend.inference.local_first import (
    LocalFirstTransport,
    Reason,
    decide,
    is_local,
    local_model,
)
from backend.inference.openai_engine import OpenAIEngine, _retry_after
from backend.inference.openai_limiter import (
    DbLedger,
    OpenAILimiter,
    OpenAILimits,
    OpenAIQueueTimeout,
    OpenAIThrottled,
)
from backend.persistence.connection import make_session_factory
from backend.persistence.models import EscalationGrant, OpenAICall

T0 = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.now = T0
        self.slept: list[float] = []

    def __call__(self) -> datetime:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += timedelta(seconds=seconds)


# ---- routing ----------------------------------------------------------------


def test_local_models_and_cloud_names_are_swapped():
    for m in (
        "local/any",
        "secure/planner",
        "pool/tier1/code",
        "vllm/qwen3.8-27b-nvfp4",
        "llama-cpp/x",
    ):
        assert is_local(m)
    for m in (
        "auto/best-chat",
        "cloud/openai",
        "hybrid/code",
        "openai/gpt-5.6-sol",
        "nerve-ask",
        None,
    ):
        assert not is_local(m)
    assert local_model("auto/best-chat", caller="t") == "local/any"
    assert local_model("secure/planner", caller="t") == "secure/planner"
    assert local_model("x", caller="t", env={"NERVE_LOCAL_MODEL": "local/esnixi"}) == "local/esnixi"
    assert local_model("x", caller="t", env={"NERVE_LOCAL_MODEL": "cloud/openai"}) == "local/any"


def test_transport_wrapper_forces_local():
    seen = []

    class Inner:
        base_url = "u"

        def generate(self, model, prompt, *a, **k):
            seen.append(model)
            return {}

    t = LocalFirstTransport(Inner(), "t")
    t.generate("auto/best-chat", "p")
    t.generate("secure/planner", "p")
    assert seen == ["local/any", "secure/planner"] and t.base_url == "u"


def test_escalation_reasons():
    # background default: never
    for kw in ({}, {"local_failures": 9}, {"capability": "openai_mcp_tunnel"}):
        assert not decide("auto/x", caller="a", background=True, **kw).escalated
    # (a)
    d = decide(None, caller="a", background=True, owner_requested=True)
    assert d.escalated and d.reason is Reason.OWNER_REQUESTED
    # (b) high stakes still local first, escalates after N failures
    assert not decide(
        None, caller="a", background=True, high_stakes=True, local_failures=1
    ).escalated
    d = decide(None, caller="a", background=True, high_stakes=True, local_failures=2)
    assert d.reason is Reason.HIGH_STAKES
    # (c) foreground after N failures, N configurable
    assert (
        decide(None, caller="f", background=False, local_failures=2).reason is Reason.LOCAL_FAILED
    )
    env = {"NERVE_LOCAL_MAX_FAILURES": "3"}
    assert not decide(None, caller="f", background=False, local_failures=2, env=env).escalated
    # (d)
    d = decide(None, caller="f", background=False, capability="openai_mcp_tunnel")
    assert d.reason is Reason.CAPABILITY_REQUIRED
    assert decide("auto/x", caller="f", background=False).model == "local/any"


def test_escalation_is_logged_with_reason_and_no_content():
    with structlog.testing.capture_logs() as logs:
        decide("secret prompt?", caller="f", background=False, owner_requested=True)
    ev = [e for e in logs if e["event"] == "inference.local_first.escalate"]
    assert ev and ev[0]["reason"] == "owner_requested" and "secret" not in json.dumps(ev)


# ---- limiter ----------------------------------------------------------------


def limiter(clock: Clock, **limits: Any) -> OpenAILimiter:
    return OpenAILimiter(limits=OpenAILimits(**limits), clock=clock, sleep=clock.sleep)


async def ok_call() -> dict[str, Any]:
    return {"content": "x", "input_tokens": 10, "output_tokens": 5}


async def test_min_spacing_between_requests():
    clock = Clock()
    lim = limiter(clock, min_spacing_s=20)
    await lim.run(ok_call, caller="t", reason="owner_requested")
    await lim.run(ok_call, caller="t", reason="owner_requested")
    assert clock.slept == [20.0]


async def test_concurrency_one_serializes_callers():
    clock = Clock()
    lim = limiter(clock, min_spacing_s=0)
    active, peak = 0, 0

    async def slow() -> dict[str, Any]:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return {"input_tokens": 1, "output_tokens": 1}

    await asyncio.gather(*(lim.run(slow, caller="t", reason="r") for _ in range(4)))
    assert peak == 1


async def test_429_honors_retry_after_then_exponential_backoff():
    clock = Clock()
    lim = limiter(clock, min_spacing_s=0, base_backoff_s=5)
    attempts = []

    async def flaky() -> dict[str, Any]:
        attempts.append(clock.now)
        if len(attempts) == 1:
            raise OpenAIThrottled(retry_after=30)
        if len(attempts) == 2:
            raise OpenAIThrottled()
        return {"input_tokens": 1, "output_tokens": 1}

    await lim.run(flaky, caller="t", reason="r")
    gaps = [(b - a).total_seconds() for a, b in zip(attempts, attempts[1:], strict=False)]
    assert gaps == [30.0, 10.0]  # Retry-After, then base * 2 ** (attempt-1) for attempt 2
    usage = await lim.usage()
    assert usage["today"]["throttled"] == 2 and usage["today"]["requests"] == 1


async def test_429_gives_up_after_max_retries():
    clock = Clock()
    lim = limiter(clock, min_spacing_s=0, max_retries=1, base_backoff_s=1)

    async def always() -> dict[str, Any]:
        raise OpenAIThrottled()

    with pytest.raises(OpenAIThrottled):
        await lim.run(always, caller="t", reason="r")


async def test_hourly_cap_waits_for_the_window_and_times_out_past_max_wait():
    clock = Clock()
    lim = limiter(clock, min_spacing_s=0, max_requests_hour=2, max_wait_s=7200)
    await lim.run(ok_call, caller="t", reason="r")
    await lim.run(ok_call, caller="t", reason="r")
    await lim.run(ok_call, caller="t", reason="r")  # waits ~1 h instead of failing
    assert clock.slept and abs(sum(clock.slept) - 3600) < 1
    short = limiter(Clock(), min_spacing_s=0, max_requests_hour=1, max_wait_s=60)
    await short.run(ok_call, caller="t", reason="r")
    with pytest.raises(OpenAIQueueTimeout):
        await short.run(ok_call, caller="t", reason="r")


async def test_token_cap():
    clock = Clock()
    lim = limiter(clock, min_spacing_s=0, max_tokens_hour=20, max_wait_s=7200)
    await lim.run(ok_call, caller="t", reason="r")  # 15 tokens
    await lim.run(ok_call, caller="t", reason="r")  # 30 >= 20 afterwards
    await lim.run(ok_call, caller="t", reason="r")
    assert clock.slept and clock.slept[-1] > 3000


def test_limits_from_env():
    lim = OpenAILimits.from_env(
        {
            "NERVE_OPENAI_MIN_SPACING_S": "5",
            "NERVE_OPENAI_CONCURRENCY": "0",
            "NERVE_OPENAI_MAX_REQUESTS_DAY": "bad",
        }
    )
    assert lim.min_spacing_s == 5.0 and lim.concurrency == 1 and lim.max_requests_day == 200
    assert _retry_after("12") == 12.0 and _retry_after(None) is None


@pytest.fixture
async def factory():
    eng = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    async with eng.begin() as conn:
        for m in (OpenAICall, EscalationGrant):
            await conn.run_sync(m.__table__.create)
    yield make_session_factory(eng)
    await eng.dispose()


async def test_durable_ledger_survives_a_new_limiter(factory):
    clock = Clock()
    first = OpenAILimiter(
        DbLedger(factory, clock), OpenAILimits(min_spacing_s=60), clock=clock, sleep=clock.sleep
    )
    await first.run(ok_call, caller="t", reason="owner_requested")
    second = OpenAILimiter(
        DbLedger(factory, clock), OpenAILimits(min_spacing_s=60), clock=clock, sleep=clock.sleep
    )
    await second.run(ok_call, caller="t", reason="owner_requested")
    assert clock.slept == [60.0]
    usage = await second.usage()
    assert usage["today"]["requests"] == 2 and usage["today"]["input_tokens"] == 20


async def test_engine_uses_limiter_and_maps_429(factory):
    clock = Clock()
    lim = OpenAILimiter(
        DbLedger(factory, clock),
        OpenAILimits(min_spacing_s=0, base_backoff_s=1),
        clock=clock,
        sleep=clock.sleep,
    )
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "7"})
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 4},
            },
        )

    eng = OpenAIEngine(
        None,
        env={"NERVE_OPENAI_KEY": "k" * 20},
        transport=httpx.MockTransport(handler),
        limiter=lim,
    )
    out = await eng.generate("hi", caller="test", reason="owner_requested")
    assert out["content"] == "ok" and clock.slept == [7.0]


# ---- grants + API ------------------------------------------------------------


async def test_grants_and_policy_endpoint(factory):
    app = FastAPI()
    app.include_router(policy_ep.router)
    app.state.escalation_grants = GrantStore(factory)
    app.state.openai_limiter = OpenAILimiter()
    app.state.openai_engine = OpenAIEngine(None, env={})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.put(
            "/api/model-policy/grants",
            json={"subject_kind": "agent", "subject_id": "a1", "mode": "high_stakes"},
        )
        assert r.status_code == 200
        assert (
            await c.put(
                "/api/model-policy/grants",
                json={"subject_kind": "agent", "subject_id": "a1", "mode": "yes"},
            )
        ).status_code == 422
        body = (await c.get("/api/model-policy")).json()
        assert body["local_model"] == "local/any"
        assert body["grants"] == [
            {"subject_kind": "agent", "subject_id": "a1", "mode": "high_stakes"}
        ]
        assert body["openai"]["key_configured"] is False and body["openai"]["queue_depth"] == 0
        assert "today" in body["openai"] and body["openai"]["limits"]["concurrency"] == 1
        assert (await c.delete("/api/model-policy/grants/agent/a1")).status_code == 204
        assert (await c.get("/api/model-policy")).json()["grants"] == []


async def _bg(grant: str | None, fail_local: int):
    from backend.inference.local_first import background_call

    log: list[Any] = []

    async def local(model: str) -> dict[str, Any]:
        log.append(("local", model))
        if sum(1 for x in log if x[0] == "local") <= fail_local:
            raise RuntimeError("down")
        return {"content": "local"}

    async def openai(reason: Any) -> dict[str, Any]:
        log.append(("openai", str(reason)))
        return {"content": "openai"}

    try:
        out = await background_call(
            "auto/best-chat", grant, caller="agent_runner", local=local, openai=openai
        )
    except RuntimeError:
        out = None
    return out, log


async def test_background_agents_local_only_without_grant():
    out, log = await _bg(None, 0)
    assert out == {"content": "local"} and log == [("local", "local/any")]
    out, log = await _bg(None, 5)
    assert out is None and log == [("local", "local/any")]  # one try, no escalation


async def test_background_grants():
    out, log = await _bg("owner_requested", 0)
    assert log == [("openai", "owner_requested")]
    out, log = await _bg("high_stakes", 5)
    assert log == [("local", "local/any"), ("local", "local/any"), ("openai", "high_stakes")]
    out, log = await _bg("high_stakes", 1)
    assert out == {"content": "local"} and len(log) == 2
