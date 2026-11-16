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


def build_runner(engine: Any, app: Any = None) -> AgentRunner:
    """Wire a runner to the DB engine and the OmniRoute transport.

    NS-ADR-107/108: agents run on local secure combos (``secure_transport``). An agent
    reaches the OpenAI API only with an owner grant: ``owner_requested`` goes
    straight to OpenAI, ``high_stakes`` retries locally ``max_local_failures``
    times first. Every OpenAI call goes through the shared limiter.
    """
    from backend.inference.local_first import background_call
    from backend.inference.omniroute import OmniRouteTransport
    from backend.inference.providers import default_inference
    from backend.inference.secure_client import secure_transport
    from backend.persistence.connection import make_session_factory
    from backend.services.incident_agent import AgentService

    factory = make_session_factory(engine)
    service = AgentService(engine, session_factory=factory)
    raw_transport = OmniRouteTransport.from_env()
    # NS-ADR-108: every agent run is sensitive by default (secure combo,
    # no-memory headers, route guard). No agent is marked non-sensitive today.
    transport = secure_transport("agent_runner", raw_transport)

    async def grant_for(agent_id: str) -> str | None:
        if app is None:
            return None
        from backend.inference.escalation import get_grants

        grants = get_grants(app)
        return await grants.get("agent", agent_id) if grants else None

    async def openai(prompt: str, reason: Any) -> dict[str, Any]:
        from backend.inference.openai_engine import get_engine

        if app is None:
            raise RuntimeError("OpenAI escalation is not available")
        return await get_engine(app).generate(prompt, caller="agent_runner", reason=str(reason))

    async def infer(agent: Any, prompt: str) -> dict[str, Any]:
        binding = await service.get_inference(str(agent.id)) or {}
        requested = binding.get("model") or default_inference()["model"]
        return await background_call(
            requested,
            await grant_for(str(agent.id)),
            caller="agent_runner",
            local=lambda model: asyncio.to_thread(transport.generate, model, prompt),
            openai=lambda reason: openai(prompt, reason),
        )

    transport_for_health = raw_transport

    async def health() -> bool | None:
        if not transport_for_health.base_url and not transport_for_health.fixture_mode:
            return None
        try:
            routes = await asyncio.to_thread(transport_for_health.route_candidates)
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
    runner = build_runner(engine, app)
    app.state.agent_runner = runner
    return runner
