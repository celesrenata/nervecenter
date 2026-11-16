"""Durable agent runner (NS-ADR-099): cadence, run history, back-off, Skill Hub."""

from __future__ import annotations

import asyncio
from typing import Any

import structlog

from backend.agents.runner.backoff import BackoffController
from backend.agents.runner.runner import (
    AgentBusyError,
    AgentMissingError,
    AgentRunner,
    runner_disabled,
)
from backend.agents.runner.skillhub import SkillHubClient
from backend.agents.runner.store import RunnerStore

log = structlog.get_logger(__name__)

__all__ = [
    "AgentBusyError",
    "AgentMissingError",
    "AgentRunner",
    "BackoffController",
    "RunnerStore",
    "SkillHubClient",
    "build_runner",
    "get_runner",
    "runner_disabled",
]


def build_runner(engine: Any) -> AgentRunner:
    """Wire a runner to the DB engine and the OmniRoute transport."""
    from backend.inference.omniroute import OmniRouteTransport
    from backend.inference.providers import default_inference
    from backend.persistence.connection import make_session_factory
    from backend.services.incident_agent import AgentService

    factory = make_session_factory(engine)
    service = AgentService(engine, session_factory=factory)
    transport = OmniRouteTransport.from_env()

    async def infer(agent: Any, prompt: str) -> dict[str, Any]:
        binding = await service.get_inference(str(agent.id)) or {}
        model = binding.get("model") or default_inference()["model"]
        return await asyncio.to_thread(transport.generate, model, prompt)

    async def health() -> bool | None:
        if not transport.base_url and not transport.fixture_mode:
            return None
        try:
            routes = await asyncio.to_thread(transport.route_candidates)
        except Exception:  # noqa: BLE001 - probe failure is "no signal", not "down"
            return None
        if not routes:
            return None
        return any(r.availability == "ready" for r in routes)

    return AgentRunner(RunnerStore(factory), infer, health_probe=health)


def get_runner(app: Any) -> AgentRunner | None:
    """The app's runner, built lazily from ``app.state.db_engine``."""
    runner = getattr(app.state, "agent_runner", None)
    if runner is not None:
        return runner
    engine = getattr(app.state, "db_engine", None)
    if engine is None:
        return None
    runner = build_runner(engine)
    app.state.agent_runner = runner
    return runner
