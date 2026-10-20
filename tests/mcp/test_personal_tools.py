"""R112 personal-tool contract tests.

The 11 new personal-operations tools (added on top of the 18 §45 tools →
29 total) are exercised offline through a ``httpx.MockTransport`` injected
into :class:`mcp.api.NerveAPI`: per tool the exact HTTP method, path,
query/body shape and the §29 ``Nerve-Trace-Id`` header are asserted, plus
catalogue metadata (names, total, required args) and error behavior.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from mcp.api import TRACE_HEADER, NerveAPI
from mcp.core import build_registry, handle_message
from mcp.tools_module import ALL_TOOLS, build_tools, resolve_base_url

#: The 11 R112 tools added on top of the existing 18 §45 tools.
PERSONAL_TOOLS: dict[str, tuple[str, str]] = {
    "get_attention": ("GET", "/api/attention"),
    "get_daily_brief": ("GET", "/api/briefing"),
    "get_person": ("GET", "/api/people/{id}"),
    "get_conversation": ("GET", "/api/conversations/{id}"),
    "search_communications": ("GET", "/api/communications"),
    "get_commitments": ("GET", "/api/commitments"),
    "get_waiting_on": ("GET", "/api/waiting-on"),
    "create_followup": ("POST", "/api/followups"),
    "snooze_attention": ("POST", "/api/attention/{id}/snooze"),
    "resolve_attention": ("POST", "/api/attention/{id}/resolve"),
    "draft_reply": ("POST", "/api/drafts"),
}

ORIGINAL_18 = {
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

REQUIRED_ARGS = {
    "get_person": ["id"],
    "get_conversation": ["id"],
    "snooze_attention": ["id"],
    "resolve_attention": ["id"],
    "create_followup": ["reason", "target_id", "target_kind"],
    "draft_reply": ["channel", "text"],
}

#: Tools whose description must not imply any outgoing send.
NO_SEND_TOOLS = ("create_followup", "draft_reply")


def _call(registry: Any, name: str, arguments: dict[str, Any], api: NerveAPI) -> dict[str, Any]:
    message = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}}
    result = handle_message(message, registry, api)
    assert result is not None and "result" in result, result
    return result["result"]


def _api(handler: Any) -> NerveAPI:
    return NerveAPI("http://unit-test", transport=httpx.MockTransport(handler))


def test_all_tools_exactly_nine_twenty_nine() -> None:
    """_SPEC / ALL_TOOLS / build_tools / registry all report exactly 29 tools."""
    assert len(ALL_TOOLS) == 29
    assert len(build_tools("http://unit:8000")) == 29
    registry = build_registry()
    assert len(registry.list_tools()) == 29
    names = {t["name"] for t in ALL_TOOLS}
    assert names == ORIGINAL_18 | set(PERSONAL_TOOLS)
    assert len(names) == 29


def test_new_descriptor_metadata() -> None:
    """Each R112 entry carries the canonical method/path/schema + required args."""
    by_name = {t["name"]: t for t in ALL_TOOLS}
    for name, (method, path) in PERSONAL_TOOLS.items():
        entry = by_name[name]
        assert entry["method"] == method, name
        assert entry["path"] == path, name
        assert entry["input_schema"]["type"] == "object", name
        assert entry["input_schema"]["properties"] == {
            p: s for p, s in entry["input_schema"]["properties"].items() if isinstance(s, dict)
        }, name
        for prop, spec in entry["input_schema"]["properties"].items():
            assert spec["type"] in ("string", "integer", "array"), (name, prop)
            assert spec.get("description"), (name, prop)


def test_required_args_match_contract() -> None:
    """Required-arg sets match the backend request contracts; the rest are optional."""
    by_name = {t["name"]: t for t in ALL_TOOLS}
    for name, required in REQUIRED_ARGS.items():
        assert sorted(by_name[name]["input_schema"]["required"]) == required, name
    for name in PERSONAL_TOOLS:
        if name not in REQUIRED_ARGS:
            assert by_name[name]["input_schema"]["required"] == [], name


def test_no_send_semantics_in_descriptions() -> None:
    """create_followup / draft_reply must be explicit that they create local
    records only and never send anything to any channel."""
    by_name = {t["name"]: t for t in ALL_TOOLS}
    for name in NO_SEND_TOOLS:
        desc = by_name[name]["description"].lower()
        assert "sends nothing" in desc or "never sends" in desc, name


def test_dispatch_methods_paths_and_shapes() -> None:
    """Each tool dispatches exactly its canonical method/path with the right args."""
    registry = build_registry()
    calls: list[tuple[str, str, dict[str, Any]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            payload: dict[str, Any] = dict(request.url.params)
        else:
            payload = json.loads(request.content or b"{}")
        calls.append((request.method, request.url.path, payload))
        return httpx.Response(200, json={"ok": True})

    api = _api(handler)
    invocations = [
        ("get_attention", {"state": "NOW", "limit": 5}),
        ("get_daily_brief", {"kind": "midday"}),
        ("get_person", {"id": "p-1"}),
        ("get_conversation", {"id": "cv-1"}),
        ("search_communications", {"person_id": "p-1", "channel": "email"}),
        ("get_commitments", {}),
        ("get_waiting_on", {"status": "all"}),
        ("create_followup", {"target_kind": "commitment", "target_id": "f-1", "reason": "promised a quote"}),
        ("snooze_attention", {"id": "a-1", "until": "2026-10-21T09:00:00Z"}),
        ("resolve_attention", {"id": "a-2"}),
        ("draft_reply", {"channel": "imessage", "text": "On it."}),
    ]
    for name, args in invocations:
        result = _call(registry, name, args, api)
        assert result["isError"] is False, result["content"][0]["text"]

    assert calls == [
        ("GET", "/api/attention", {"state": "NOW", "limit": "5"}),
        ("GET", "/api/briefing", {"kind": "midday"}),
        ("GET", "/api/people/p-1", {}),
        ("GET", "/api/conversations/cv-1", {}),
        ("GET", "/api/communications", {"person_id": "p-1", "channel": "email"}),
        ("GET", "/api/commitments", {}),
        ("GET", "/api/waiting-on", {"status": "all"}),
        (
            "POST",
            "/api/followups",
            {"target_kind": "commitment", "target_id": "f-1", "reason": "promised a quote"},
        ),
        ("POST", "/api/attention/a-1/snooze", {"id": "a-1", "until": "2026-10-21T09:00:00Z"}),
        ("POST", "/api/attention/a-2/resolve", {"id": "a-2"}),
        ("POST", "/api/drafts", {"channel": "imessage", "text": "On it."}),
    ]


def test_optional_args_omitted() -> None:
    """Optional args left out of the call never appear on the wire."""
    registry = build_registry()
    calls: list[tuple[str, str, dict[str, Any]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload: dict[str, Any] = dict(request.url.params) if request.method == "GET" else json.loads(request.content or b"{}")
        calls.append((request.method, request.url.path, payload))
        return httpx.Response(200, json={"ok": True})

    api = _api(handler)
    _call(registry, "get_attention", {}, api)
    _call(registry, "get_daily_brief", {}, api)
    _call(registry, "search_communications", {}, api)
    _call(registry, "draft_reply", {"channel": "sms", "text": "hi"}, api)
    assert calls == [
        ("GET", "/api/attention", {}),
        ("GET", "/api/briefing", {}),
        ("GET", "/api/communications", {}),
        ("POST", "/api/drafts", {"channel": "sms", "text": "hi"}),
    ]


def test_missing_required_arg_is_tool_error() -> None:
    """A required arg left out answers isError with 'missing required argument'."""
    registry = build_registry()
    api = _api(lambda request: httpx.Response(200, json={}))
    result = _call(registry, "create_followup", {"target_kind": "commitment"}, api)
    assert result["isError"] is True
    assert "missing required argument" in result["content"][0]["text"]
    result = _call(registry, "draft_reply", {"channel": "email"}, api)
    assert result["isError"] is True
    assert "missing required argument" in result["content"][0]["text"]


def test_unknown_tool_is_tool_error_not_exception() -> None:
    """tools/call with an unregistered name answers isError, not a JSON-RPC error."""
    registry = build_registry()
    api = _api(lambda request: httpx.Response(200, json={}))
    result = _call(registry, "send_email", {"to": "x"}, api)
    assert result["isError"] is True
    assert "unknown tool: send_email" in result["content"][0]["text"]


def test_permission_metadata_not_supported_by_existing_schema() -> None:
    """Contract blocker: the §45 Tool schema (name/description/inputSchema) has no
    permission/annotation field, so the R112 tools expose no permission metadata.

    The descriptors must stay exactly name/description/inputSchema — if a
    permission layer is added to mcp.tools.Tool.descriptor(), extend this test
    to assert per-tool scopes (e.g. personal:read / personal:write).
    """
    registry = build_registry()
    for descriptor in registry.list_tools():
        assert set(descriptor) == {"name", "description", "inputSchema"}
        assert "annotations" not in descriptor
        assert "permissions" not in descriptor


def test_trace_header_on_personal_calls() -> None:
    """Every proxied personal call carries a fresh §29 Nerve-Trace-Id."""
    registry = build_registry()
    seen_headers: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_headers.append(request.headers[TRACE_HEADER])
        return httpx.Response(200, json={"ok": True})

    api = _api(handler)
    _call(registry, "get_attention", {}, api)
    _call(registry, "draft_reply", {"channel": "email", "text": "ok"}, api)
    assert len(seen_headers) == 2
    assert all(len(h) == 32 for h in seen_headers)


def test_base_url_env_override(monkeypatch: Any) -> None:
    """base_url resolution: argument > NERVE_API env > localhost default."""
    assert resolve_base_url() == "http://localhost:8000"
    monkeypatch.setenv("NERVE_API", "http://127.0.0.1:9999")
    assert resolve_base_url() == "http://127.0.0.1:9999"
    assert resolve_base_url("http://arg:1") == "http://arg:1"
