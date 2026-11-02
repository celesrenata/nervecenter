"""Agent service endpoints.

The read routes expose the *real* agent registry — :class:`AgentRegistry`
from :mod:`backend.agents.registry` — instead of a hardcoded fixture. The
registry holds the 14 native agent types and their capability manifests
(spec §26). Agent *instances* created through the API are now persisted to
the ``agents`` table (migration 004) via
:class:`~backend.services.incident_agent.AgentService`; ``GET /agents``
merges the native registry rows first, then the persisted instances.

Write routes (AG-3, added in round 3)
-------------------------------------
- ``POST /api/agents`` — create a durable agent instance (201).

Honest degradation
------------------
When no DB engine is configured the create route answers ``503`` (mirroring
the :mod:`backend.api.endpoints.incidents` / goals precedent) and
``GET /agents`` serves the native registry rows only. No fake acceptance.

Fields with no backing store (``lastHeartbeat``/``cpuUsage``/
``memoryUsage``/``tasksCompleted``/``tasksFailed``) are returned as
``None`` instead of being fabricated.

The ``pause`` mutation under ``/api/agents`` still has **no** backing
persistence/service contract (the agent registry is an in-memory,
non-mutable surface), so it returns an explicit, honest ``501 Not
Implemented`` rather than a fake ``202 accepted``.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from backend.agents.registry import AgentRegistry
from backend.api.models import ListResponse
from backend.api.endpoints._helpers import list_response
from backend.services.incident_agent import (
    AgentNameConflictError,
    AgentNotFoundError,
    AgentService,
)
from backend.inference.providers import _sanitize_binding

# A single process-wide registry (the native types are static and shared);
# building it per-request would just repeat the same 14 manifests.
_REGISTRY = AgentRegistry()

# The agents collection spans two prefix families:
#   - legacy read routes under ``/agents`` (``GET /agents/``, ``GET /agents/{id}``);
#   - the spec §47 mutation endpoint under the canonical ``/api/agents`` prefix.
# ``backend.api.app`` registers this single router and *separately* installs the
# ``/api/agents`` GET canonical aliases (see ``canonical_aliases``), which
# delegate to the module-level :func:`list_agents` / :func:`get_agent` below
# (the alias passes its ``Request`` through so the persisted-agent merge works
# under the canonical prefix too).
# The router is therefore prefix-less and declares full paths so both families
# resolve without a trailing-slash redirect.
router = APIRouter(tags=["agents"])


def _manifest_to_row(manifest) -> dict[str, Any]:
    """Map a real :class:`AgentManifest` to the web ``Agent`` row shape.

    Telemetry fields have no backing store, so they are ``None`` (honest)
    rather than invented numbers.
    """
    capabilities = manifest.capabilities
    return {
        "id": manifest.agent_type,
        "name": manifest.name,
        "type": manifest.agent_type,
        "status": "active",
        "description": manifest.description,
        "tools": list(manifest.tools),
        "prompt_template": manifest.prompt_template,
        "capabilities": (
            {
                "tools": list(capabilities.tools),
                "policy": capabilities.policy,
                "verification": capabilities.verification,
            }
            if capabilities is not None
            else None
        ),
        # Per-agent inference binding (contract §3); native types carry it on
        # the manifest (set via ``AgentRegistry.set_inference``), else ``None``.
        "inference": _sanitize_binding(getattr(manifest, "inference", None)),
        # No backing store yet — do not fabricate live telemetry.
        "lastHeartbeat": None,
        "cpuUsage": None,
        "memoryUsage": None,
        "tasksCompleted": None,
        "tasksFailed": None,
    }


@router.get("/agents/", response_model=ListResponse)
async def list_agents(request: Request):
    """List all agents.

    Native agent-*type* registry rows come first; when a DB engine is
    configured, persisted agent *instances* (created via
    ``POST /api/agents``) follow. Without a DB the list is the native
    registry only (honest degradation, not a fabricated merge).
    """
    agents = [_manifest_to_row(m) for m in _REGISTRY.list_all()]
    svc = _agent_service(request)
    if svc is not None:
        agents.extend(await svc.list_persisted())
    return list_response(agents)


@router.get("/agents/{agent_id}")
async def get_agent(agent_id: str, request: Request = None):
    """Get a single agent by id.

    Resolution order: first the native agent *type* registry (e.g. ``planner``);
    if that misses and a DB-backed agent service is configured, fall back to a
    persisted agent *instance*. Unknown ids return 404 (not a fabricated
    fallback); with no DB available a non-native id is an honest 404.
    """
    manifest = _REGISTRY.get(agent_id)
    if manifest is not None:
        return _manifest_to_row(manifest)
    svc = _agent_service(request) if request is not None else None
    if svc is not None:
        try:
            return await svc.get(agent_id)
        except AgentNotFoundError:
            pass
    raise HTTPException(status_code=404, detail="Agent not found")


# §47 `nerve pause-agents` → POST /api/agents/pause (canonical path).
class AgentCreateRequest(BaseModel):
    """Payload for ``POST /api/agents``.

    ``name`` is unique across the persisted agent table; a collision
    surfaces as a ``409`` rather than a duplicate row.
    """

    name: str = Field(..., min_length=1, description="Unique agent name")
    type: str = Field(..., min_length=1, description="Agent type (spec §26)")
    description: str | None = Field(default=None, description="What the agent does")
    prompt_template: str | None = Field(
        default=None, description="Optional system prompt template"
    )
    tools: list[str] = Field(default_factory=list, description="Tool names")
    inference: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Optional per-agent inference binding, e.g. "
            '{"provider": "omniroute", "model": "claude-sonnet-4"}. '
            "Both keys are optional; null means 'use the default' (contract §3)."
        ),
    )
    combo_id: str | None = Field(
        default=None,
        description=(
            "Optional durable combo binding (id of an inference combo). "
            "When set, the agent resolves its provider/model through the "
            "combo at ask time; re-pointing the combo re-binds the agent."
        ),
    )


def _agent_service(request: Request) -> AgentService | None:
    """Resolve the agents write-path service for this request.

    Prefers an explicitly wired ``services["agents"]`` (so a future
    :func:`backend.api.wiring.build_services` entry wins), then falls back to a
    per-app service built from the configured ``db_engine``. Returns ``None``
    when neither is available (no ``NERVE_DB_URL`` set).
    """
    services = getattr(request.app.state, "services", {})
    svc = services.get("agents") if isinstance(services, dict) else None
    if svc is not None:
        return svc
    engine = getattr(request.app.state, "db_engine", None)
    if engine is None:
        return None
    state = request.app.state
    memo = getattr(state, "_agents_service", None)
    if memo is None:
        memo = AgentService(engine)
        setattr(state, "_agents_service", memo)
    return memo


@router.post("/api/agents", status_code=201)
async def create_agent(
    request: Request, payload: AgentCreateRequest
) -> dict[str, Any]:
    """Create a durable agent instance (AG-3, round 3).

    Returns the web row for the new instance (201). Degrades to ``503`` when
    no DB engine is configured; duplicate names return ``409``.
    """
    svc = _agent_service(request)
    if svc is None:
        raise HTTPException(
            status_code=503,
            detail="Agent persistence is not configured; set NERVE_DB_URL.",
        )
    try:
        return await svc.create(
            name=payload.name,
            agent_type=payload.type,
            description=payload.description,
            prompt_template=payload.prompt_template,
            tools=payload.tools,
            inference=payload.inference,
            combo_id=payload.combo_id or None,
        )
    except AgentNameConflictError:
        raise HTTPException(
            status_code=409,
            detail=f"An agent named '{payload.name}' already exists.",
        ) from None


class AgentUpdateRequest(BaseModel):
    """Payload for ``PATCH /api/agents/{agent_id}``.

    Every field is optional: a ``None`` value leaves the corresponding attribute
    unchanged (partial update). ``name`` is unique across the persisted table; a
    collision with another row surfaces as a ``409``.
    """

    model_config = {"extra": "forbid"}

    name: str | None = Field(default=None, min_length=1, description="New unique name")
    description: str | None = Field(default=None, description="What the agent does")
    prompt_template: str | None = Field(
        default=None, description="Optional system prompt template"
    )
    tools: list[str] | None = Field(default=None, description="Tool names")
    inference: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Optional per-agent inference binding; a supplied dict sets it. "
            "Absent/null leaves the existing binding unchanged (contract §4)."
        ),
    )
    combo_id: str | None = Field(
        default=None,
        description=(
            "Optional durable combo binding; setting it binds the agent to "
            "the combo, setting it to a null/omitted value leaves the "
            "existing binding unchanged. (A clear is handled by the web UI "
            "by passing an empty string.)"
        ),
    )


@router.patch("/api/agents/{agent_id}")
async def update_agent(
    request: Request, agent_id: str, payload: AgentUpdateRequest
) -> dict[str, Any]:
    """Partially update a persisted agent instance.

    Returns the updated web row (200). Degrades to ``503`` when no DB engine is
    configured, ``404`` for an unknown id, and ``409`` for a duplicate name.
    """
    svc = _agent_service(request)
    if svc is None:
        raise HTTPException(
            status_code=503,
            detail="Agent persistence is not configured; set NERVE_DB_URL.",
        )
    try:
        return await svc.update(
            agent_id,
            name=payload.name,
            description=payload.description,
            prompt_template=payload.prompt_template,
            tools=payload.tools,
            inference=payload.inference,
            combo_id=payload.combo_id,
        )
    except AgentNotFoundError:
        raise HTTPException(
            status_code=404, detail=f"Agent {agent_id!r} not found"
        ) from None
    except AgentNameConflictError:
        raise HTTPException(
            status_code=409,
            detail=f"An agent named '{payload.name}' already exists.",
        ) from None


@router.post("/api/agents/pause", status_code=501)
async def pause_agents() -> dict[str, Any]:
    """Pause all agents.

    There is no durable agent-mutation persistence/service contract; the
    registry is an in-memory, non-mutable surface, so we answer an honest
    ``501`` instead of a fake ``202`` that would pretend the pause applied.
    """
    return {
        "detail": (
            "Agent pause is not implemented: there is no durable agent "
            "persistence/service contract to record the mutation."
        )
    }
