"""Restricted MCP endpoint for project context (NS-ADR-104).

``POST /api/mcp/projects`` speaks the JSON-RPC subset (initialize, ping,
tools/list, tools/call) and exposes exactly three tools: ``list_projects``,
``get_project_context`` and ``add_project_note``. Reads plus note-append only.

Order of work per request: bearer header -> verifier chain (generic 401, nothing
read or stored before it) -> body cap (64 KiB) -> per-client rate limit ->
dispatch. The audit log carries tool, project slug, outcome and client, never
note text. The author of a written note is the authenticated client name, not
a caller-supplied value.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from backend.projects.auth import Principal, TokenVerifier, build_verifiers
from backend.projects.service import (
    ProjectInputError,
    ProjectNotFoundError,
    ProjectService,
    get_service,
)

log = structlog.get_logger(__name__)

router = APIRouter(tags=["mcp-projects"])

MAX_BODY_BYTES = 64 * 1024
RATE_PER_SECOND = 5.0
RATE_BURST = 10.0
PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "nervcenter-projects", "version": "0.1.0"}

PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS = -32700, -32600, -32601, -32602

_SLUG = {"type": "string", "description": "Project slug (lowercase letters, digits, dashes)"}
TOOLS: dict[str, dict[str, Any]] = {
    "list_projects": {
        "description": "List shared projects (slug, title, note count).",
        "properties": {},
        "required": [],
    },
    "get_project_context": {
        "description": "Get a project's summary and its notes (oldest first).",
        "properties": {"slug": _SLUG},
        "required": ["slug"],
    },
    "add_project_note": {
        "description": "Append a note to a project (append-only, tagged with your client name).",
        "properties": {"slug": _SLUG, "body": {"type": "string", "description": "Note text"}},
        "required": ["slug", "body"],
    },
}


def _result(rid: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def _error(rid: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


def _unauthorized() -> JSONResponse:
    return JSONResponse(
        {"detail": "Unauthorized"}, status_code=401, headers={"WWW-Authenticate": "Bearer"}
    )


class RateLimiter:
    """Token bucket per client; in-process (one api replica)."""

    def __init__(
        self,
        rate: float = RATE_PER_SECOND,
        burst: float = RATE_BURST,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._rate, self._burst, self._clock = rate, burst, clock
        self._buckets: dict[str, tuple[float, float]] = {}

    def allow(self, key: str) -> bool:
        now = self._clock()
        tokens, last = self._buckets.get(key, (self._burst, now))
        tokens = min(self._burst, tokens + (now - last) * self._rate)
        if tokens < 1.0:
            self._buckets[key] = (tokens, now)
            return False
        self._buckets[key] = (tokens - 1.0, now)
        return True


def _check_args(tool: str, args: Any) -> str | None:
    if not isinstance(args, dict):
        return "arguments must be an object"
    spec = TOOLS[tool]
    for key in spec["required"]:
        if not isinstance(args.get(key), str):
            return f"argument {key!r} must be a string"
    extra = set(args) - set(spec["properties"])
    if extra:
        return "unexpected argument"
    return None


async def _call_tool(
    service: ProjectService, principal: Principal, tool: str, args: dict[str, Any]
) -> tuple[Any, str]:
    """Return (result, outcome). Raises nothing for expected failures."""
    if tool == "list_projects":
        return {"projects": await service.list_projects()}, "ok"
    if tool == "get_project_context":
        return await service.get(args["slug"]), "ok"
    note = await service.add_note(args["slug"], principal.client, args["body"])
    return note, "ok"


async def dispatch(
    message: Any, service: ProjectService, principal: Principal
) -> dict[str, Any] | None:
    if not isinstance(message, dict):
        return _error(None, INVALID_REQUEST, "message must be a JSON object")
    if "id" not in message:
        return None  # notification
    rid = message.get("id")
    method = message.get("method")
    params = message.get("params") or {}
    if not isinstance(method, str) or not isinstance(params, dict):
        return _error(rid, INVALID_REQUEST, "invalid request")
    if method == "initialize":
        return _result(
            rid,
            {
                "protocolVersion": PROTOCOL_VERSION,
                "serverInfo": dict(SERVER_INFO),
                "capabilities": {"tools": {"listChanged": False}},
            },
        )
    if method == "ping":
        return _result(rid, {})
    if method == "tools/list":
        return _result(
            rid,
            {
                "tools": [
                    {
                        "name": name,
                        "description": spec["description"],
                        "inputSchema": {
                            "type": "object",
                            "properties": spec["properties"],
                            "required": spec["required"],
                        },
                    }
                    for name, spec in TOOLS.items()
                ]
            },
        )
    if method != "tools/call":
        return _error(rid, METHOD_NOT_FOUND, "method not found")
    name = params.get("name")
    args = params.get("arguments") or {}
    if not isinstance(name, str) or name not in TOOLS:
        log.info(
            "mcp.projects.call",
            tool="unknown",
            project=None,
            outcome="unknown_tool",
            client=principal.client,
        )
        return _result(
            rid, {"content": [{"type": "text", "text": "unknown tool"}], "isError": True}
        )
    slug = (
        args.get("slug") if isinstance(args, dict) and isinstance(args.get("slug"), str) else None
    )
    problem = _check_args(name, args)
    result: Any = None
    if problem:
        text, outcome = problem, "invalid"
    else:
        try:
            result, outcome = await _call_tool(service, principal, name, args)
            text = ""
        except ProjectInputError as exc:
            text, outcome = str(exc), "invalid"
        except ProjectNotFoundError:
            text, outcome = "project not found", "not_found"
        except Exception:  # noqa: BLE001 - never leak internals to the caller
            text, outcome = "internal error", "error"
    log.info("mcp.projects.call", tool=name, project=slug, outcome=outcome, client=principal.client)
    if outcome != "ok":
        return _result(rid, {"content": [{"type": "text", "text": text}], "isError": True})
    return _result(
        rid,
        {
            "content": [{"type": "text", "text": json.dumps(result, indent=2, default=str)}],
            "structuredContent": result,
            "isError": False,
        },
    )


def _verifiers(request: Request) -> list[TokenVerifier]:
    existing = getattr(request.app.state, "mcp_project_verifiers", None)
    if existing is None:
        existing = build_verifiers()
        request.app.state.mcp_project_verifiers = existing
    return existing


def _limiter(request: Request) -> RateLimiter:
    existing = getattr(request.app.state, "mcp_project_limiter", None)
    if existing is None:
        existing = RateLimiter()
        request.app.state.mcp_project_limiter = existing
    return existing


async def _authenticate(request: Request) -> Principal | None:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    for verifier in _verifiers(request):
        principal = await verifier.verify(token.strip())
        if principal is not None:
            return principal
    return None


@router.post("/api/mcp/projects")
async def projects_mcp(request: Request) -> Response:
    principal = await _authenticate(request)
    if principal is None:
        return _unauthorized()
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        return JSONResponse({"detail": "Payload too large"}, status_code=413)
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > MAX_BODY_BYTES:
            return JSONResponse({"detail": "Payload too large"}, status_code=413)
    if not _limiter(request).allow(principal.client):
        return JSONResponse(
            {"detail": "Too many requests"}, status_code=429, headers={"Retry-After": "1"}
        )
    service = get_service(request.app)
    if service is None:
        return JSONResponse({"detail": "durable storage is not configured"}, status_code=503)
    try:
        message = json.loads(bytes(raw)) if raw else None
    except ValueError:
        return JSONResponse(_error(None, PARSE_ERROR, "parse error"), status_code=400)
    if message is None:
        return JSONResponse(_error(None, INVALID_REQUEST, "invalid request"), status_code=400)
    response = await dispatch(message, service, principal)
    if response is None:
        return Response(status_code=204)
    return JSONResponse(response)
