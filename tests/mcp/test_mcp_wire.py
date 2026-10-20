"""Wire-model tests for the NerveCenter MCP server (mcp.core / mcp.protocol).

No live server is used: dispatch runs in-process, the HTTP app is exercised
with ``fastapi.testclient`` (lifespan-driven), and tools/call is exercised
against a ``httpx.MockTransport``-backed :class:`mcp.api.NerveAPI` so the
whole path — JSON-RPC envelope → tool registry → §46 proxy → trace header —
is covered offline.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
from fastapi.testclient import TestClient

import mcp
from mcp.api import TRACE_HEADER, NerveAPI
from mcp.core import build_registry, build_server, create_app, handle_message
from mcp.protocol import METHOD_NOT_FOUND, PROTOCOL_VERSION, SERVER_INFO

SPEC_TOOLS = {
    "search_entities",
    "get_entity",
    "get_entity_timeline",
    "get_recent_changes",
    "get_goal",
    "create_goal",
    "update_goal",
    "get_task",
    "create_task",
    "report_claim",
    "report_artifact",
    "compile_context",
    "get_compute",
    "get_models",
    "get_route_history",
    "get_incident",
    "start_investigation",
    "create_planned_change",
}

#: R112 personal-operations tools (11 more on top of the 18 §45 names).
PERSONAL_TOOLS = {
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


def _mock_api(handler: Any) -> NerveAPI:
    return NerveAPI("http://unit-test", transport=httpx.MockTransport(handler))


def test_import_surface_and_all_tools_count() -> None:
    """``import mcp`` exposes the 29-tool catalogue (18 §45 + 11 R112) plus the core API."""
    assert len(mcp.ALL_TOOLS) == len(SPEC_TOOLS) + len(PERSONAL_TOOLS)
    assert {t["name"] for t in mcp.ALL_TOOLS} == SPEC_TOOLS | PERSONAL_TOOLS
    for name in ("build_registry", "build_server", "handle_message", "register_all_tools"):
        assert hasattr(mcp, name), name
    for name in ("Tool", "ToolError", "ToolRegistry", "validate_args", "NerveAPI", "MCPClient"):
        assert hasattr(mcp, name), name
    assert mcp.PROTOCOL_VERSION == PROTOCOL_VERSION == "2024-11-05"
    assert mcp.SERVER_INFO == SERVER_INFO


def test_initialize_envelope() -> None:
    """initialize returns protocolVersion, serverInfo, and capabilities."""
    reg = build_registry()
    result = handle_message(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}, reg, None
    )
    assert result is not None
    assert result["jsonrpc"] == "2.0"
    assert result["id"] == 1
    assert result["result"]["protocolVersion"] == PROTOCOL_VERSION
    assert result["result"]["serverInfo"]["name"] == "nervcenter-mcp"
    assert result["result"]["capabilities"]["tools"]


def test_ping_returns_empty_result() -> None:
    """ping answers with an empty result object."""
    reg = build_registry()
    result = handle_message({"jsonrpc": "2.0", "id": 2, "method": "ping"}, reg, None)
    assert result == {"jsonrpc": "2.0", "id": 2, "result": {}}


def test_tools_list_has_all_spec_tools() -> None:
    """tools/list advertises all 29 tools (18 §45 + 11 R112) with name/description/inputSchema."""
    reg = build_registry()
    result = handle_message({"jsonrpc": "2.0", "id": 3, "method": "tools/list"}, reg, None)
    assert result is not None
    tools = result["result"]["tools"]
    assert len(tools) >= 15
    names = {t["name"] for t in tools}
    assert SPEC_TOOLS <= names
    for tool in tools:
        assert tool["description"]
        assert tool["inputSchema"]["type"] == "object"
        assert "properties" in tool["inputSchema"]
        assert "required" in tool["inputSchema"]


def test_unknown_method_returns_method_not_found() -> None:
    """Unknown methods answer with the protocol.METHOD_NOT_FOUND envelope."""
    reg = build_registry()
    result = handle_message({"jsonrpc": "2.0", "id": 4, "method": "nope"}, reg, None)
    assert result is not None
    assert "error" in result
    assert result["error"]["code"] == METHOD_NOT_FOUND == -32601


def test_invalid_request_without_method() -> None:
    """A request without a method is an INVALID_REQUEST, not a method miss."""
    reg = build_registry()
    result = handle_message({"jsonrpc": "2.0", "id": 5, "params": {}}, reg, None)
    assert result is not None
    assert result["error"]["code"] == -32600


def test_notification_produces_no_response() -> None:
    """Notifications (no id) get no response per JSON-RPC 2.0."""
    reg = build_registry()
    assert handle_message({"jsonrpc": "2.0", "method": "ping"}, reg, None) is None
    call_notification = {"jsonrpc": "2.0", "method": "tools/call", "params": {}}
    assert handle_message(call_notification, reg, None) is None


def test_tools_call_success_with_mock_transport() -> None:
    """tools/call proxies to §46, sends Nerve-Trace-Id, wraps text content."""
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["query"] = request.url.query
        seen["trace"] = request.headers.get(TRACE_HEADER)
        return httpx.Response(200, json={"data": [{"id": "gpu-1"}], "meta": {"request_id": "r1"}})

    api = _mock_api(handler)
    reg = build_registry()
    result = handle_message(
        {
            "jsonrpc": "2.0",
            "id": 10,
            "method": "tools/call",
            "params": {"name": "search_entities", "arguments": {"q": "gpu"}},
        },
        reg,
        api,
    )
    assert result is not None
    assert "result" in result
    content = result["result"]["content"]
    assert content[0]["type"] == "text"
    expected = {"data": [{"id": "gpu-1"}], "meta": {"request_id": "r1"}}
    assert json.loads(content[0]["text"]) == expected
    assert result["result"]["isError"] is False
    assert seen["path"] == "/api/search"
    assert b"q=gpu" in seen["query"]  # httpx url.query is bytes
    assert seen["trace"]  # §29 correlation header present on the proxied call


def test_tools_call_error_returns_is_error_result() -> None:
    """Tool failures (unknown tool, bad args) surface as isError results."""
    reg = build_registry()
    unknown = handle_message(
        {"jsonrpc": "2.0", "id": 11, "method": "tools/call", "params": {"name": "does_not_exist"}},
        reg,
        None,
    )
    assert unknown is not None
    assert unknown["result"]["isError"] is True
    assert "unknown tool" in unknown["result"]["content"][0]["text"]

    missing_params = {"name": "get_entity", "arguments": {}}
    missing = handle_message(
        {"jsonrpc": "2.0", "id": 12, "method": "tools/call", "params": missing_params}, reg, None
    )
    assert missing is not None
    assert missing["result"]["isError"] is True
    assert "missing required argument: id" in missing["result"]["content"][0]["text"]


def test_tools_call_http_error_proxies_as_error_result() -> None:
    """A non-2xx §46 answer propagates as an isError tool result."""
    api = _mock_api(lambda request: httpx.Response(404, json={"detail": "Entity not found"}))
    reg = build_registry()
    result = handle_message(
        {
            "jsonrpc": "2.0",
            "id": 13,
            "method": "tools/call",
            "params": {"name": "get_entity", "arguments": {"id": "nope"}},
        },
        reg,
        api,
    )
    assert result is not None
    assert result["result"]["isError"] is True
    assert "404" in result["result"]["content"][0]["text"]


def test_handle_message_works_from_running_event_loop() -> None:
    """handle_message() is synchronous even when called inside an event loop."""
    import asyncio

    async def caller() -> dict[str, Any]:
        reg = build_registry()
        return handle_message({"jsonrpc": "2.0", "id": 20, "method": "ping"}, reg, None) or {}

    assert asyncio.run(caller()) == {"jsonrpc": "2.0", "id": 20, "result": {}}


def test_build_server_stdio_and_http() -> None:
    """build_server() returns server objects usable for both transports."""
    stdio_server = build_server("stdio")
    assert stdio_server.name == SERVER_INFO["name"]
    assert stdio_server.transport == "stdio"
    assert len(stdio_server.registry.list_tools()) == len(SPEC_TOOLS) + len(PERSONAL_TOOLS)

    http_server = build_server("http")
    assert http_server.app is not None
    assert http_server.transport == "http"


def test_http_app_serves_both_mcp_path_spellings() -> None:
    """The JSON-RPC POST handler is registered on BOTH ``/mcp`` and ``/mcp/``.

    The ingress serves the ``/mcp/`` prefix behind TLS while plain ``/mcp``
    falls through to the web SPA; if only one spelling existed, the other
    would answer with a trailing-slash redirect (307) built from the plain
    HTTP connection behind the ingress. Both spellings must therefore be
    served directly — the route table contains both and TestClient
    (``follow_redirects=False``) must never see a redirect.
    """
    app = create_app(api=_mock_api(lambda request: httpx.Response(200, json={})))
    post_paths = sorted(
        route.path for route in app.routes if "POST" in getattr(route, "methods", set())
    )
    assert "/mcp" in post_paths
    assert "/mcp/" in post_paths
    assert app.router.redirect_slashes is True  # app-wide redirects stay enabled

    with TestClient(app, follow_redirects=False) as client:
        for path in ("/mcp", "/mcp/"):
            response = client.post(
                path, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
            )
            assert response.status_code == 200, path  # no 307/301 redirect
            body = response.json()
            assert body["jsonrpc"] == "2.0"
            assert body["id"] == 1
            tools = body["result"]["tools"]
            assert {tool["name"] for tool in tools} == SPEC_TOOLS | PERSONAL_TOOLS
        # Sanity: the explicit health route is unaffected.
        health = client.get("/mcp/health")
        assert health.status_code == 200
        assert health.json() == {"status": "healthy"}
