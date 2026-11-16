"""Agent runner endpoints (NS-ADR-099): run now, history, schedule, status.

Only persisted agent instances can be scheduled or run; built-in types are
read-only manifests. Without a DB engine every route answers ``503``.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from backend.agents.runner import AgentBusyError, AgentMissingError, get_runner

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
