"""Allowlisted agent tool layer (NS-ADR-113)."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from backend.agents.tools import ToolGateway, policy
from backend.agents.tools.fetch_guard import FetchGuard
from backend.agents.tools.protocol import MAX_ROUNDS, parse_calls, run_with_tools

HUB_TOKEN = "hub-token-value"


async def _public(host: str) -> list[str]:
    return ["93.184.215.14"]


def _guard() -> FetchGuard:
    return FetchGuard(
        resolver=_public,
        transport=httpx.MockTransport(lambda r: httpx.Response(200)),
        sensitive_allowlist=frozenset(),
    )


OMNI_KEY = "omni-key-value"


class Mcp:
    """A fake hub and OmniRoute MCP: records (url, auth, method, tool)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None, str, str | None]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        method = body["method"]
        tool = body.get("params", {}).get("name")
        self.calls.append((str(request.url), request.headers.get("authorization"), method, tool))
        if method == "initialize":
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": 1, "result": {}},
                headers={"mcp-session-id": "sid"},
            )
        if method.startswith("notifications/"):
            return httpx.Response(202)
        text = json.dumps({"healthy": True, "tool": tool})
        payload = json.dumps(
            {"jsonrpc": "2.0", "id": 2, "result": {"content": [{"type": "text", "text": text}]}}
        )
        return httpx.Response(200, text=f"event: message\ndata: {payload}\n\n")

    def gateway(self, **kw: Any) -> ToolGateway:
        return ToolGateway(
            hub_url="http://hub.test",
            hub_token=HUB_TOKEN,
            omniroute_url="https://omni.test/mcp",
            omniroute_key=OMNI_KEY,
            transport=httpx.MockTransport(self.handler),
            fetch_guard=_guard(),
            **kw,
        )

    def tool_calls(self) -> list[tuple[str, str | None, str | None]]:
        return [(u, a, t) for u, a, m, t in self.calls if m == "tools/call"]


AGENT = SimpleNamespace(id="a1", type="planner", name="planner")


async def test_fetch_and_omniroute_succeed_with_their_own_credentials() -> None:
    mcp = Mcp()
    gw = mcp.gateway()
    fetched = await gw.call(AGENT, "fetch", "fetch", {"url": "https://example.org"})
    health = await gw.call(AGENT, "omniroute", "omniroute_get_health")
    assert fetched["ok"] and health["ok"] and health["result"]["healthy"] is True
    calls = mcp.tool_calls()
    assert calls[0] == ("http://hub.test/fetch", f"Bearer {HUB_TOKEN}", "fetch")
    assert calls[1] == ("https://omni.test/mcp", f"Bearer {OMNI_KEY}", "omniroute_get_health")
    sequential = await gw.call(AGENT, "sequential-thinking", "sequentialthinking", {"thought": "x"})
    assert sequential["ok"]


@pytest.mark.parametrize(
    ("server", "tool", "reason"),
    [
        ("fetch", "delete_everything", "tool_not_allowed"),
        ("memory", "format_disk", "tool_not_allowed"),
        ("omniroute", "omniroute_skills_list", "tool_not_allowed"),
        ("filesystem", "read_file", "unknown_server"),
    ],
)
async def test_unlisted_tools_and_servers_are_refused_without_a_request(
    server: str, tool: str, reason: str
) -> None:
    mcp = Mcp()
    out = await mcp.gateway().call(AGENT, server, tool, {})
    assert out == {"ok": False, "denied_reason": reason}
    assert mcp.calls == []


async def test_memory_is_refused_by_default_and_for_sensitive_agents_and_personal_data() -> None:
    mcp = Mcp()
    gw = mcp.gateway()
    args = {"entities": [{"name": "kubernetes", "entityType": "topic", "observations": ["ok"]}]}
    assert (await gw.call(AGENT, "memory", "create_entities", args))["denied_reason"] == (
        "memory_requires_non_sensitive"
    )
    finance = SimpleNamespace(id="f", type="finance", name="Finance helper")
    assert (await gw.call(finance, "memory", "create_entities", args, non_sensitive=True))[
        "denied_reason"
    ] == "memory_sensitive_agent"
    health = SimpleNamespace(id="h", type="custom", name="mental-health check-in")
    assert policy.sensitive_class(health)
    for leak in (
        {"note": "mail me at someone@example.org"},
        {"note": "call +1 555 123 4567"},
        {"note": "account 123456789012"},
    ):
        out = await gw.call(AGENT, "memory", "add_observations", leak, non_sensitive=True)
        assert out["denied_reason"] == "memory_personal_data"
    assert mcp.calls == []
    ok = await gw.call(AGENT, "memory", "create_entities", args, non_sensitive=True)
    assert ok["ok"] and mcp.tool_calls()[0][0] == "http://hub.test/memory"


async def test_unconfigured_servers_are_reported_not_called() -> None:
    gw = ToolGateway(hub_token="", omniroute_key="")
    assert not gw.configured
    assert (await gw.call(AGENT, "fetch", "fetch", {}))["denied_reason"] == "unconfigured"


async def test_http_failure_is_a_reason_code_without_the_body() -> None:
    gw = ToolGateway(
        hub_token="t",
        omniroute_key="",
        transport=httpx.MockTransport(lambda r: httpx.Response(503, text="secret body")),
    )
    out = await gw.call(AGENT, "sequential-thinking", "sequentialthinking", {})
    assert out == {"ok": False, "denied_reason": "http_503"}


def test_allowlist_is_frozen() -> None:
    with pytest.raises(TypeError):
        policy.HUB_SERVERS["shell"] = frozenset({"run"})  # type: ignore[index]
    with pytest.raises(AttributeError):
        policy.OMNIROUTE_TOOLS.add("omniroute_delete")  # type: ignore[attr-defined]
    assert len(policy.OMNIROUTE_TOOLS) == 11
    assert policy.is_allowed("fetch", "fetch") and not policy.is_allowed("fetch", "other")


def test_parse_calls_ignores_malformed_lines() -> None:
    text = (
        'hi\nTOOL_CALL {"server":"fetch","tool":"fetch","args":{"url":"u"}}\nTOOL_CALL {bad\n'
        'TOOL_CALL {"server":1}\n'
    )
    calls = parse_calls(text)
    assert [(c.server, c.tool, c.args) for c in calls] == [("fetch", "fetch", {"url": "u"})]


async def test_model_written_tool_results_are_discarded() -> None:
    from backend.agents.tools.protocol import strip_invented_results

    text = 'TOOL_CALL {"server":"fetch","tool":"fetch","args":{}}\nTOOL_RESULT fetch/fetch: fake\nmore'
    assert strip_invented_results(text) == 'TOOL_CALL {"server":"fetch","tool":"fetch","args":{}}'
    assert strip_invented_results("plain report") == "plain report"


async def test_model_cannot_mark_memory_calls_non_sensitive() -> None:
    mcp = Mcp()
    replies = iter(
        [
            'TOOL_CALL {"server":"memory","tool":"create_entities","args":{},"non_sensitive":true}',
            "done",
        ]
    )

    async def infer(prompt: str) -> dict[str, Any]:
        return {"content": next(replies), "input_tokens": 1, "output_tokens": 1}

    result = await run_with_tools(infer, "p", mcp.gateway(), AGENT)
    assert result["content"] == "done" and mcp.tool_calls() == []


async def test_tool_loop_is_bounded_and_sums_tokens() -> None:
    mcp = Mcp()
    prompts: list[str] = []

    async def infer(prompt: str) -> dict[str, Any]:
        prompts.append(prompt)
        return {
            "content": 'TOOL_CALL {"server":"fetch","tool":"fetch",'
            '"args":{"url":"https://example.org/a"}}',
            "input_tokens": 2,
            "output_tokens": 3,
        }

    result = await run_with_tools(infer, "p", mcp.gateway(), AGENT)
    assert len(prompts) == MAX_ROUNDS + 1 and len(mcp.tool_calls()) == MAX_ROUNDS
    assert result["input_tokens"] == 2 * (MAX_ROUNDS + 1)
    assert "TOOL_RESULT fetch/fetch" in prompts[-1]


async def test_without_a_gateway_the_prompt_is_unchanged() -> None:
    seen: list[str] = []

    async def infer(prompt: str) -> dict[str, Any]:
        seen.append(prompt)
        return {"content": "x"}

    await run_with_tools(infer, "plain", None, AGENT)
    assert seen == ["plain"]


async def test_health_signal_reads_omniroute_health_and_never_says_down_on_failure() -> None:
    assert await Mcp().gateway().health_signal() is True
    broken = ToolGateway(
        omniroute_key="k", transport=httpx.MockTransport(lambda r: httpx.Response(500))
    )
    assert await broken.health_signal() is None
    assert await ToolGateway(hub_token="", omniroute_key="").health_signal() is None


def test_tools_endpoints() -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from backend.api.endpoints import agent_runner as runner_ep

    app = FastAPI()
    app.include_router(runner_ep.router)
    with TestClient(app) as client:
        body = client.get("/api/agents/tools").json()
        assert set(body["servers"]) == {
            "sequential-thinking", "fetch", "memory", "omniroute", "infra", "nerve"}
        assert body["servers"]["fetch"]["tools"] == ["fetch"]
        assert (
            client.post(
                "/api/agents/x/tools/call", json={"server": "fetch", "tool": "fetch"}
            ).status_code
            == 503
        )


async def test_tool_loop_reports_progress_events_in_order() -> None:
    mcp = Mcp()
    replies = iter(
        [
            'TOOL_CALL {"server":"fetch","tool":"fetch","args":{"url":"https://example.org/a"}}',
            "done",
        ]
    )

    async def infer(prompt: str) -> dict[str, Any]:
        return {"content": next(replies), "input_tokens": 1, "output_tokens": 1}

    events: list[tuple[str, str]] = []

    def on_event(kind: str, text: str, data: dict[str, Any] | None) -> None:
        events.append((kind, text))
        if kind == "tool_result":
            raise RuntimeError("a broken listener must not break the run")

    result = await run_with_tools(infer, "p", mcp.gateway(), AGENT, on_event)
    assert result["content"] == "done"
    kinds = [k for k, _ in events]
    assert kinds == [
        "model_call", "model_reply", "tool_call", "tool_result", "model_call", "model_reply"
    ]
    assert events[2][1] == "fetch.fetch" and events[-1][1] == "done"
