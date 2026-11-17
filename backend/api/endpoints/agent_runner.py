"""Agent runner endpoints (NS-ADR-099): run now, history, schedule, status.

Only persisted agent instances can be scheduled or run; built-in types are
read-only manifests. Without a DB engine every route answers ``503``.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from backend.agents.runner import AgentBusyError, AgentMissingError, get_runner
from backend.agents.tools import ToolGateway

router = APIRouter(tags=["agent-runner"])


class ScheduleRequest(BaseModel):
    model_config = {"extra": "forbid"}

    mode: str = Field(default="on_demand", description="interval | daily | on_demand")
    interval_seconds: int | None = Field(default=None, description="Seconds between runs (min 60)")
    daily_time: str | None = Field(default=None, description="HH:MM for daily mode")
    timezone: str | None = Field(default=None, description="IANA timezone for daily mode")
    priority: str = Field(default="background", description="background | time_critical")
    enabled: bool = True


def _runner(request: Request):
    runner = get_runner(request.app)
    if runner is None:
        raise HTTPException(
            status_code=503,
            detail="Agent runner is not configured; set NERVE_DB_URL.",
        )
    return runner


@router.get("/api/agents/runner/status")
async def runner_status(request: Request) -> dict[str, Any]:
    """Back-off state, stretch factors and whether the loop is running.

    Without a DB engine there is no runner; this still answers 200 with an
    honest "not running" payload so the Agents screen can render.
    """
    runner = get_runner(request.app)
    if runner is None:
        return {
            "state": "normal",
            "stretch": {"background": 1.0, "time_critical": 1.0},
            "throttles_in_window": 0,
            "running": False,
            "skillhub_configured": False,
            "configured": False,
        }
    return runner.status()


@router.post("/api/agents/{agent_id}/run", status_code=201)
async def run_agent_now(request: Request, agent_id: str) -> dict[str, Any]:
    """Run one agent immediately and return the recorded run."""
    try:
        return await _runner(request).run_agent(agent_id, "on_demand")
    except AgentMissingError:
        raise HTTPException(status_code=404, detail="Agent not found") from None
    except AgentBusyError:
        raise HTTPException(status_code=409, detail="This agent is already running.") from None


@router.get("/api/agents/{agent_id}/runs")
async def list_runs(request: Request, agent_id: str, limit: int = 25) -> dict[str, Any]:
    runner = _runner(request)
    if await runner.store.get_agent(agent_id) is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    return {"data": await runner.store.runs(agent_id, limit)}


@router.get("/api/agents/{agent_id}/schedule")
async def get_schedule(request: Request, agent_id: str) -> dict[str, Any]:
    runner = _runner(request)
    if await runner.store.get_agent(agent_id) is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    return await runner.get_schedule(agent_id)


@router.put("/api/agents/{agent_id}/schedule")
async def put_schedule(request: Request, agent_id: str, payload: ScheduleRequest) -> dict[str, Any]:
    try:
        return await _runner(request).set_schedule(agent_id, payload.model_dump())
    except AgentMissingError:
        raise HTTPException(status_code=404, detail="Agent not found") from None
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


class ToolCallRequest(BaseModel):
    model_config = {"extra": "forbid"}
    server: str = Field(
        ..., min_length=1, description="fetch | sequential-thinking | memory | omniroute"
    )
    tool: str = Field(..., min_length=1)
    args: dict[str, Any] = Field(default_factory=dict)
    non_sensitive: bool = Field(
        default=False,
        description="Owner marker required for memory tools (NS-ADR-113); never set by agents.",
    )


def _gateway(request: Request) -> ToolGateway:
    runner = get_runner(request.app)
    gateway = getattr(runner, "tools", None) if runner is not None else None
    return gateway if gateway is not None else ToolGateway()


_COMBO_CACHE: dict[str, Any] = {"at": 0.0, "combos": None}
COMBO_CACHE_S = 60.0


def _salvage_combos(text: str) -> list[dict[str, Any]]:
    """Recover the complete combo objects from a list OmniRoute's MCP filter truncated (~50 KB)."""
    import json as _json

    start = text.find("[", text.find('"combos"'))
    if start < 0:
        return []
    decoder, pos, out = _json.JSONDecoder(), start + 1, []
    while True:
        while pos < len(text) and text[pos] in " \t\r\n,":
            pos += 1
        if pos >= len(text) or text[pos] != "{":
            return out
        try:
            obj, pos = decoder.raw_decode(text, pos)
        except ValueError:
            return out
        out.append(obj)


def _parse_combos(result: Any) -> list[dict[str, Any]]:
    import json as _json

    data = result
    if isinstance(result, str):
        try:
            data = _json.loads(result)
        except ValueError:
            data = _salvage_combos(result)
    items = data.get("combos", []) if isinstance(data, dict) else data
    out: list[dict[str, Any]] = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            continue
        models = [m.get("model") for m in item.get("models") or [] if isinstance(m, dict)]
        out.append({
            "name": item["name"],
            "enabled": bool(item.get("enabled", True)),
            "secure": item["name"].startswith("secure/"),
            "models": [m for m in models if isinstance(m, str)][:8],
        })
    return sorted(out, key=lambda c: (not c["secure"], c["name"]))


@router.get("/api/agents/omniroute-combos")
async def omniroute_combos(request: Request) -> dict[str, Any]:
    """OmniRoute combos an agent can run on (names and their models only; cached 60 s).

    Choosing one sets the agent's model to the combo name (e.g. ``secure/coder``);
    the runner's local-first routing and secure/no-memory rules still apply.
    """
    import time

    now = time.monotonic()
    if _COMBO_CACHE["combos"] is None or now - _COMBO_CACHE["at"] > COMBO_CACHE_S:
        # Full result: the agent-facing 20 000-char cap cut the list into broken JSON.
        combos = _parse_combos(await _gateway(request).omniroute_full("omniroute_list_combos"))
        # OmniRoute's MCP filter truncates the list; /v1/models names every combo.
        from backend.inference.catalog import get_catalog
        seen = {c["name"] for c in combos}
        for item in await get_catalog().items():
            if item["kind"] == "combo" and item["id"] not in seen:
                seen.add(item["id"])
                combos.append({"name": item["id"], "enabled": True,
                               "secure": item["id"].startswith("secure/"), "models": []})
        combos.sort(key=lambda c: (not c["secure"], c["name"]))
        if combos or _COMBO_CACHE["combos"] is None:
            _COMBO_CACHE.update(at=now, combos=combos)
    return {"combos": _COMBO_CACHE["combos"] or []}


@router.get("/api/agents/tools")
async def list_tools(request: Request) -> dict[str, Any]:
    """The frozen tool allowlist and whether each server is reachable-configured."""
    return _gateway(request).describe()


@router.post("/api/agents/{agent_id}/tools/call")
async def call_tool(request: Request, agent_id: str, payload: ToolCallRequest) -> dict[str, Any]:
    """Call one allowlisted tool as this agent, through the same gateway the runner uses."""
    runner = _runner(request)
    agent = await runner.store.get_agent(agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    return await _gateway(request).call(
        agent, payload.server, payload.tool, payload.args, non_sensitive=payload.non_sensitive
    )
