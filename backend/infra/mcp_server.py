"""MCP endpoint the central opencode uses to ACT on approved infra plans (NS-ADR-120).

``POST /api/mcp/infra`` (in-cluster only; no public route). Same bearer-token
verifiers, 64 KiB body cap and rate limiter as ``/api/mcp/projects``, but only
the client named ``opencode`` may call it. Three tools:

- ``infra_get_plan(plan_id)``: the plan, if it is approved/running/done/failed;
- ``infra_run_step(plan_id, index)``: run the NEXT step exactly as approved;
- ``infra_run_verify(plan_id)``: run the plan's read-only verification steps.

opencode never receives a credential: steps run inside NerveCenter.
"""

from __future__ import annotations

import json
from typing import Any

import structlog
from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from backend.infra.service import InfraInputError, InfraService, get_service
from backend.projects import mcp_server as projects_mcp

log = structlog.get_logger(__name__)

router = APIRouter(tags=["mcp-infra"])

ACT_CLIENT = "opencode"
VISIBLE = ("approved", "running", "done", "failed")
_ID = {"type": "string", "description": "Plan id"}
TOOLS: dict[str, dict[str, Any]] = {
    "infra_get_plan": {
        "description": "Read an approved infrastructure plan: steps, verification, rollback, "
        "and the results of steps already run.",
        "properties": {"plan_id": _ID},
        "required": ["plan_id"],
    },
    "infra_run_step": {
        "description": "Run the next step of an approved plan exactly as approved. index must be "
        "the next selected step (the plan's selected_steps, or all steps when null; ascending). Returns ok, exit code and (redacted) output. If ok is false "
        "the plan stops: report and do not continue.",
        "properties": {"plan_id": _ID, "index": {"type": "integer", "minimum": 0}},
        "required": ["plan_id", "index"],
    },
    "infra_run_verify": {
        "description": "Run the plan's read-only verification steps after all steps ran.",
        "properties": {"plan_id": _ID},
        "required": ["plan_id"],
    },
}


async def _call(svc: InfraService, tool: str, args: dict[str, Any]) -> Any:
    plan_id = args.get("plan_id")
    if not isinstance(plan_id, str):
        raise InfraInputError("plan_id must be a string")
    plan = await svc.get_plan(plan_id)
    if plan["status"] not in VISIBLE:
        raise InfraInputError(f"plan is {plan['status']}")
    if tool == "infra_get_plan":
        return plan
    if tool == "infra_run_step":
        index = args.get("index")
        if not isinstance(index, int) or isinstance(index, bool):
            raise InfraInputError("index must be an integer")
        return await svc.run_step(plan_id, index)
    return {"verify": await svc.run_verify(plan_id)}


async def dispatch(message: Any, svc: InfraService) -> dict[str, Any] | None:
    if not isinstance(message, dict):
        return projects_mcp._error(None, projects_mcp.INVALID_REQUEST, "invalid request")
    if "id" not in message:
        return None
    rid, method = message.get("id"), message.get("method")
    params = message.get("params") or {}
    if method == "initialize":
        return projects_mcp._result(rid, {
            "protocolVersion": projects_mcp.PROTOCOL_VERSION,
            "serverInfo": {"name": "nervcenter-infra", "version": "0.1.0"},
            "capabilities": {"tools": {"listChanged": False}},
        })
    if method == "ping":
        return projects_mcp._result(rid, {})
    if method == "tools/list":
        return projects_mcp._result(rid, {"tools": [
            {"name": n, "description": s["description"],
             "inputSchema": {"type": "object", "properties": s["properties"],
                             "required": s["required"]}}
            for n, s in TOOLS.items()]})
    if method != "tools/call" or not isinstance(params, dict):
        return projects_mcp._error(rid, projects_mcp.METHOD_NOT_FOUND, "method not found")
    name, args = params.get("name"), params.get("arguments") or {}
    if name not in TOOLS or not isinstance(args, dict):
        return projects_mcp._result(
            rid, {"content": [{"type": "text", "text": "unknown tool"}], "isError": True})
    try:
        result, outcome = await _call(svc, name, args), "ok"
    except KeyError:
        result, outcome = "plan not found", "not_found"
    except InfraInputError as exc:
        result, outcome = str(exc), "invalid"
    except Exception:  # noqa: BLE001 - never leak internals
        result, outcome = "internal error", "error"
    log.info("mcp.infra.call", tool=name, outcome=outcome)
    if outcome != "ok":
        return projects_mcp._result(
            rid, {"content": [{"type": "text", "text": str(result)}], "isError": True})
    return projects_mcp._result(rid, {
        "content": [{"type": "text", "text": json.dumps(result, indent=2, default=str)}],
        "structuredContent": result, "isError": False})


@router.post("/api/mcp/infra")
async def infra_mcp(request: Request) -> Response:
    principal = await projects_mcp._authenticate(request)
    if principal is None or principal.client != ACT_CLIENT:
        return projects_mcp._unauthorized()
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > projects_mcp.MAX_BODY_BYTES:
        return JSONResponse({"detail": "Payload too large"}, status_code=413)
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > projects_mcp.MAX_BODY_BYTES:
            return JSONResponse({"detail": "Payload too large"}, status_code=413)
    if not projects_mcp._limiter(request).allow(f"infra:{principal.client}"):
        return JSONResponse({"detail": "Too many requests"}, status_code=429,
                            headers={"Retry-After": "1"})
    svc = get_service(request.app)
    if svc is None:
        return JSONResponse({"detail": "durable storage is not configured"}, status_code=503)
    try:
        message = json.loads(bytes(raw)) if raw else None
    except ValueError:
        return JSONResponse(projects_mcp._error(None, projects_mcp.PARSE_ERROR, "parse error"),
                            status_code=400)
    response = await dispatch(message, svc)
    return Response(status_code=204) if response is None else JSONResponse(response)
