"""Tool-contract tests: the 18 §45 tools map onto the §46 API paths.

Every tool is exercised offline through a ``httpx.MockTransport`` injected
into :class:`mcp.api.NerveAPI`: the expected HTTP method, path, query/body
shape, and the §29 ``Nerve-Trace-Id`` header are asserted per tool. The R112
personal-operations tools (11 more, 29 total) have their full contract in
``test_personal_tools.py``.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from mcp.api import TRACE_HEADER, NerveAPI
from mcp.core import build_registry, handle_message
from mcp.tools_module import ALL_TOOLS, build_tools, resolve_base_url

# tool -> (method, path, expected query/body shape)
TOOL_ROUTES: dict[str, tuple[str, str, str]] = {
    "search_entities": ("GET", "/api/search", "query q"),
    "get_entity": ("GET", "/api/entities/{id}", "path id"),
    "get_entity_timeline": ("GET", "/api/events", "query entity"),
    "get_recent_changes": ("GET", "/api/changes", "no args"),
    "get_goal": ("GET", "/api/goals/{id}", "path id"),
    "create_goal": ("POST", "/api/goals", "body"),
    "update_goal": ("POST", "/api/goals/{id}", "path id + body"),
    "get_task": ("GET", "/api/tasks/{id}", "path id"),
    "create_task": ("POST", "/api/tasks", "body"),
    "report_claim": ("POST", "/api/claims", "body"),
    "report_artifact": ("POST", "/api/evidence", "body"),
    "compile_context": ("POST", "/api/context/compile", "body"),
    "get_compute": ("GET", "/api/compute", "no args"),
    "get_models": ("GET", "/api/models", "no args"),
    "get_route_history": ("GET", "/api/routes", "no args"),
    "get_incident": ("GET", "/api/incidents/{id}", "path id"),
    "start_investigation": ("POST", "/api/incidents/investigate", "path id + body"),
    "create_planned_change": ("POST", "/api/changes", "body"),
}

#: The 11 R112 personal tools added on top of the 18 §45 names (see
#: ``test_personal_tools.py`` for their full route contract).
PERSONAL_TOOLS: set[str] = {
    "get_attention",
    "get_daily_brief",
    "get_person",
    "get_conversation",
    "search_communications",
    "get_commitments",
    "get_waiting_on",
    "create_followup",
    "snooze_attention",
    "resolve_attention",
    "draft_reply",
}


def _call(
    registry: Any, name: str, arguments: dict[str, Any], api: NerveAPI | None
) -> dict[str, Any]:
    params = {"name": name, "arguments": arguments}
    message = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": params}
    result = handle_message(message, registry, api)
    assert result is not None and "result" in result, result
    return result["result"]


def _api(handler: Any) -> NerveAPI:
    return NerveAPI("http://unit-test", transport=httpx.MockTransport(handler))


def test_all_tools_catalogue_matches_spec() -> None:
    """ALL_TOOLS is the §45 list followed by the R112 personal tools (29)."""
    assert [t["name"] for t in ALL_TOOLS[: len(TOOL_ROUTES)]] == list(TOOL_ROUTES)
    assert len(ALL_TOOLS) == 18 + len(PERSONAL_TOOLS)
    assert {t["name"] for t in ALL_TOOLS[len(TOOL_ROUTES):]} == PERSONAL_TOOLS
    for entry in ALL_TOOLS:
        assert entry["method"] in ("GET", "POST")
        assert entry["path"].startswith("/api/")
        assert entry["input_schema"]["type"] == "object"


def test_resolve_base_url_env_override(monkeypatch: Any) -> None:
    """base_url resolution: argument > NERVE_API env > localhost default."""
    assert resolve_base_url() == "http://localhost:8000"
    monkeypatch.setenv("NERVE_API", "http://127.0.0.1:9999")
    assert resolve_base_url() == "http://127.0.0.1:9999"
    assert resolve_base_url("http://arg:1") == "http://arg:1"


def test_build_tools_returns_twenty_nine_bound_tools() -> None:
    """build_tools() yields 29 Tool instances (18 §45 + 11 R112 personal)."""
    tools = build_tools("http://unit:8000")
    assert len(tools) == 18 + len(PERSONAL_TOOLS)
    assert {t.name for t in tools} == set(TOOL_ROUTES) | PERSONAL_TOOLS
    registry = build_registry()
    assert len(registry.list_tools()) == 18 + len(PERSONAL_TOOLS)


def test_get_tools_proxy_paths() -> None:
    """GET tools hit the expected §46 paths with the right query/path args."""
    cases = {
        "search_entities": {"q": "gpu"},
        "get_entity": {"id": "x-1"},
        "get_entity_timeline": {"id": "x-1"},
        "get_recent_changes": {},
        "get_goal": {"id": "x-1"},
        "get_task": {"id": "x-1"},
        "get_compute": {},
        "get_models": {},
        "get_route_history": {},
        "get_incident": {"id": "x-1"},
    }
    registry = build_registry()
    for name, args in cases.items():
        method, path, shape = TOOL_ROUTES[name]
        expected = path.format(id="x-1") if "{id}" in path else path

        def handler(
            request: httpx.Request,
            _method: str = method,
            _expected: str = expected,
            _shape: str = shape,
        ) -> httpx.Response:
            assert request.method == _method, (request.method, _method)
            assert request.url.path == _expected, (request.url.path, _expected)
            if _shape == "query q":
                assert request.url.params.get("q") == "gpu"
            if _shape == "query entity":
                # GET /api/events?entity=<id> per the task contract
                assert request.url.params.get("entity") == "x-1"
            if _shape == "path id":
                assert request.url.path.endswith("/x-1")
            return httpx.Response(200, json={"ok": True})

        api = _api(handler)
        result = _call(registry, name, args, api)
        assert result["isError"] is False, result["content"][0]["text"]
        assert json.loads(result["content"][0]["text"]) == {"ok": True}


def test_post_tools_proxy_paths_and_bodies() -> None:
    """POST tools hit the expected §46 paths; bodies carry the payloads."""
    registry = build_registry()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        # 202 accept: the dependency-tolerant backend answers request_id+timestamp
        return httpx.Response(202, json={"request_id": "req-202", "timestamp": "t"})

    api = _api(handler)

    result = _call(registry, "create_goal", {"name": "g", "body": {"description": "d"}}, api)
    assert result["isError"] is False

    result = _call(registry, "update_goal", {"id": "goal-1", "body": {"status": "done"}}, api)
    assert result["isError"] is False

    result = _call(registry, "create_task", {"name": "t"}, api)
    assert result["isError"] is False

    result = _call(registry, "report_claim", {"body": {"statement": "s"}}, api)
    assert result["isError"] is False

    result = _call(registry, "report_artifact", {"body": {"kind": "artifact"}}, api)
    assert result["isError"] is False

    result = _call(registry, "start_investigation", {"id": "inc-1"}, api)
    assert result["isError"] is False

    result = _call(registry, "create_planned_change", {"body": {"what": "w"}}, api)
    assert result["isError"] is False

    result = _call(registry, "compile_context", {"question": "why?"}, api)
    assert result["isError"] is False
    assert "request_id" in json.loads(result["content"][0]["text"])


def test_post_tools_captured_paths_bodies() -> None:
    """Each POST tool maps to exactly its §46 path (method + path asserted per call)."""
    registry = build_registry()
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        return httpx.Response(202, json={"request_id": "r"})

    api = _api(handler)
    calls = [
        ("create_goal", {"name": "g"}),
        ("update_goal", {"id": "goal-1", "body": {"status": "done"}}),
        ("create_task", {"name": "t"}),
        ("report_claim", {"body": {"statement": "s"}}),
        ("report_artifact", {"body": {"kind": "k"}}),
        ("start_investigation", {"id": "inc-1", "body": {"focus": "f"}}),
        ("create_planned_change", {"body": {"what": "w"}}),
    ]
    for name, args in calls:
        _call(registry, name, args, api)

    assert seen == [
        ("POST", "/api/goals"),
        ("POST", "/api/goals/goal-1"),
        ("POST", "/api/tasks"),
        ("POST", "/api/claims"),
        ("POST", "/api/evidence"),
        ("POST", "/api/incidents/investigate"),
        ("POST", "/api/changes"),
    ]


def test_compiled_context_falls_back_to_get_on_405() -> None:
    """compile_context: POST /api/context/compile 405 → GET /api/context."""
    registry = build_registry()
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.method == "POST":
            return httpx.Response(405, json={"detail": "method not allowed"})
        return httpx.Response(200, json={"context": "compiled"})

    api = _api(handler)
    result = _call(registry, "compile_context", {"question": "why?"}, api)
    assert result["isError"] is False
    assert calls == [("POST", "/api/context/compile"), ("GET", "/api/context")]
    assert json.loads(result["content"][0]["text"]) == {"context": "compiled"}


def test_compile_context_post_succeeds_directly() -> None:
    """compile_context: a 2xx POST answer is returned without fallback."""
    registry = build_registry()
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        return httpx.Response(200, json={"compiled": True})

    api = _api(handler)
    result = _call(registry, "compile_context", {"question": "why?"}, api)
    assert result["isError"] is False
    assert calls == ["POST"]


def test_trace_header_sent_on_proxied_calls() -> None:
    """Every proxied call carries a fresh Nerve-Trace-Id (spec §29)."""
    registry = build_registry()
    traces: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        traces.append(request.headers.get(TRACE_HEADER))
        return httpx.Response(200, json={"ok": True})

    api = _api(handler)
    _call(registry, "get_compute", {}, api)
    _call(registry, "get_models", {}, api)
    assert all(t for t in traces)
    assert len(set(traces)) == 2  # fresh correlation id per call


async def test_unreachable_api_surfaces_as_tool_error() -> None:
    """Network failure while proxying → isError result, not a JSON-RPC error."""
    registry = build_registry()
    api = NerveAPI("http://127.0.0.1:1", timeout=0.2)
    result = _call(registry, "get_compute", {}, api)
    assert result["isError"] is True
    assert "UnreachableError" in result["content"][0]["text"]
    await api.close()
