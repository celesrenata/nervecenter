"""MCP server core: registry, message dispatch, and stdio/HTTP transports.

Implements the wave-2 JSON-RPC 2.0 wire subset (spec §45) against the
``mcp.protocol`` constants — ``initialize``, ``ping``, ``tools/list``,
``tools/call`` — reusing the protocol's error codes and envelope builders
(none are redefined here). The same dispatch drives both transports:

- stdio: one JSON-RPC message per stdin line, one response line to stdout
  (``python -m mcp``).
- HTTP: ``POST /mcp`` and ``POST /mcp/`` (both registered explicitly, so
  neither spelling triggers a trailing-slash redirect; JSON body = JSON-RPC
  message, response = JSON-RPC response) plus ``GET /mcp/health`` →
  ``{"status": "healthy"}``
  (``uvicorn mcp.core:app``, used by the k8s deployment).

Unknown methods answer with the ``protocol.METHOD_NOT_FOUND`` envelope via
``protocol.make_error``; notifications (no ``id``) produce no response.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from mcp.api import DEFAULT_BASE_URL, NerveAPI
from mcp.protocol import (
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    PROTOCOL_VERSION,
    SERVER_INFO,
    is_notification,
    make_error,
    make_result,
)
from mcp.tools import ToolError, ToolRegistry
from mcp.tools_module import build_tools

TRANSPORTS = ("stdio", "http")


async def dispatch_message(
    msg: dict[str, Any], registry: ToolRegistry, api: NerveAPI | None
) -> dict[str, Any] | None:
    """Dispatch one JSON-RPC message; ``None`` means no response (notification)."""
    if not isinstance(msg, dict):
        return make_error(None, INVALID_REQUEST, "message must be a JSON object")
    if is_notification(msg):
        return None
    request_id = msg.get("id")
    method = msg.get("method")
    if not isinstance(method, str) or not method:
        return make_error(request_id, INVALID_REQUEST, "invalid request: 'method' is required")
    params = msg.get("params")
    if params is None:
        params = {}
    if not isinstance(params, dict):
        return make_error(request_id, INVALID_PARAMS, "params must be an object")

    if method == "initialize":
        return make_result(
            request_id,
            {
                "protocolVersion": PROTOCOL_VERSION,
                "serverInfo": dict(SERVER_INFO),
                "capabilities": {"tools": {"listChanged": False}},
            },
        )
    if method == "ping":
        return make_result(request_id, {})
    if method == "tools/list":
        return make_result(request_id, {"tools": registry.list_tools()})
    if method == "tools/call":
        name = params.get("name")
        if not isinstance(name, str) or not name:
            return make_error(request_id, INVALID_PARAMS, "tools/call requires a 'name' string")
        args = params.get("arguments")
        if args is None:
            args = {}
        if not isinstance(args, dict):
            return make_error(
                request_id, INVALID_PARAMS, "tools/call 'arguments' must be an object"
            )
        # ``api=None`` is valid: tools fall back to the base URL they were
        # bound to at build time (NERVE_API / default).
        try:
            result = await registry.call(name, args, api)
        except ToolError as exc:
            return make_result(
                request_id,
                {"content": [{"type": "text", "text": str(exc)}], "isError": True},
            )
        return make_result(request_id, result)
    return make_error(request_id, METHOD_NOT_FOUND, f"method not found: {method}")


def _run_in_fresh_loop(coro: Any) -> Any:
    """Run a coroutine on a throwaway loop in a worker thread and block.

    Used when :func:`handle_message` is called from inside an already-running
    event loop (e.g. from a tool executor), where ``asyncio.run`` cannot nest.
    """
    outcome: dict[str, Any] = {}

    def _worker() -> None:
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            outcome["result"] = loop.run_until_complete(coro)
        except BaseException as exc:  # noqa: BLE001 - re-raised on the caller side
            outcome["error"] = exc
        finally:
            loop.close()
            asyncio.set_event_loop(None)

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()
    thread.join()
    if "error" in outcome:
        raise outcome["error"]
    return outcome["result"]


def handle_message(
    msg: dict[str, Any], registry: ToolRegistry, api: NerveAPI | None = None
) -> dict[str, Any] | None:
    """Dispatch one JSON-RPC message and return the response (or ``None``).

    Sync facade over :func:`dispatch_message`: without a running event loop the
    dispatch runs on a fresh loop; with one running (e.g. called from inside an
    async context) it is run on a helper thread so the call still completes
    synchronously. ``None`` means no response is due (JSON-RPC notification).
    """
    coro = dispatch_message(msg, registry, api)
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    return _run_in_fresh_loop(coro)


def register_all_tools(registry: ToolRegistry, base_url: str | None = None) -> ToolRegistry:
    """Register every §45 tool (via ``mcp.tools_module.ALL_TOOLS``) on the registry."""
    for tool in build_tools(base_url):
        registry.register_tool(tool)
    return registry


def build_registry(base_url: str | None = None) -> ToolRegistry:
    """A :class:`ToolRegistry` pre-loaded with all 29 tools (18 §45 + 11 R112 personal)."""
    return register_all_tools(ToolRegistry(), base_url)


class MCPServer:
    """MCP server over the §45 wire subset, usable for stdio and HTTP.

    ``api`` may be ``None``: discovery (``initialize`` / ``tools/list``) works
    without it, and ``tools/call`` falls back to the base URL the tools were
    bound to at build time.
    """

    def __init__(
        self,
        registry: ToolRegistry | None = None,
        api: NerveAPI | None = None,
        *,
        base_url: str | None = None,
        transport: str = "stdio",
    ) -> None:
        if transport not in TRANSPORTS:
            raise ValueError(f"unsupported transport: {transport!r} (expected one of {TRANSPORTS})")
        self.transport = transport
        self.registry = registry if registry is not None else build_registry(base_url)
        self.api = api
        self.base_url = base_url
        self.app: FastAPI | None = None
        if transport == "http":
            self.app = create_app(base_url=base_url, registry=self.registry, api=self.api)

    @property
    def name(self) -> str:
        """Server identity (matches ``protocol.SERVER_INFO['name']``)."""
        return SERVER_INFO["name"]

    async def handle(self, msg: dict[str, Any]) -> dict[str, Any] | None:
        """Handle one JSON-RPC message (async form of :func:`handle_message`)."""
        return await dispatch_message(msg, self.registry, self.api)

    def close(self) -> None:
        """Best-effort synchronous teardown; the HTTP app owns its API lifecycle."""


def build_server(transport: str = "stdio", **kwargs: Any) -> MCPServer:
    """Build a server for ``"stdio"`` or ``"http"``.

    Keyword args: ``registry``, ``api``, ``base_url`` (see :class:`MCPServer`).
    """
    return MCPServer(transport=transport, **kwargs)


# ---------------------------------------------------------------------------
# HTTP transport (k8s deployment: POST /mcp and POST /mcp/ + GET /mcp/health)
# ---------------------------------------------------------------------------


def create_app(
    base_url: str | None = None,
    registry: ToolRegistry | None = None,
    api: NerveAPI | None = None,
) -> FastAPI:
    """FastAPI app exposing the MCP handler over HTTP.

    ``POST /mcp`` and ``POST /mcp/`` — JSON-RPC message in, JSON-RPC
    response out (204 for notifications, per JSON-RPC 2.0). Both spellings
    are registered explicitly so neither triggers a trailing-slash redirect
    (the ingress serves the ``/mcp/`` prefix behind TLS, so a 307 would
    bounce callers off the secure endpoint). ``GET /mcp/health`` →
    ``{"status": "healthy"}``.

    When no API is supplied, one is created against the base URL
    (``NERVE_MCP_API`` env — in-cluster: ``http://api:8000`` — else
    ``http://localhost:8000``) and managed for the app lifespan.
    """
    server_registry = registry if registry is not None else build_registry(base_url)
    resolved_base = base_url or os.environ.get("NERVE_MCP_API") or DEFAULT_BASE_URL

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        managed: NerveAPI | None = None
        if api is not None:
            app.state.api = api
        else:
            managed = NerveAPI(resolved_base)
            app.state.api = managed
        yield
        if managed is not None:
            await managed.close()

    app = FastAPI(title=SERVER_INFO["name"], version=SERVER_INFO["version"], lifespan=lifespan)

    async def mcp_endpoint(request: Request) -> JSONResponse:
        # JSON body = one JSON-RPC message (FastAPI: read the raw body — a
        # typed `body: Any` parameter would be interpreted as a query param).
        raw = await request.body()
        try:
            message = json.loads(raw) if raw else None
        except ValueError:
            return JSONResponse(
                content=make_error(None, PARSE_ERROR, "parse error"), status_code=400
            )
        if message is None:
            return JSONResponse(
                content=make_error(None, INVALID_REQUEST, "invalid request"), status_code=400
            )
        response = await dispatch_message(message, server_registry, app.state.api)
        if response is None:  # notification: no response body
            return JSONResponse(content=None, status_code=204)
        return JSONResponse(content=response)

    # Register the JSON-RPC endpoint on BOTH spellings; the handler logic is
    # identical for each — a redirect must never be needed to reach it.
    app.add_api_route("/mcp", mcp_endpoint, methods=["POST"])
    app.add_api_route("/mcp/", mcp_endpoint, methods=["POST"])

    @app.get("/mcp/health")
    async def health() -> dict[str, str]:
        return {"status": "healthy"}

    return app


# Module-level app so ``uvicorn mcp.core:app`` works with zero configuration.
app = create_app()
