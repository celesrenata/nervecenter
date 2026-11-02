"""Incident + agent write-path services (round 3, IN-1 / AG-3).

These are the application services that back the new write routes the web audit
flagged as missing:

- ``POST /api/incidents``                    (IN-1: incident create)
- ``POST /api/incidents/{id}/resolve``       (IN-1: resolve transition)
- ``POST /api/incidents/{id}/close``         (IN-1: close transition)
- ``POST /api/agents``                        (AG-3: agent create)

Design mirrors :class:`~backend.goals.service.GoalService`:

- Every mutating method runs inside a single transaction (commit on success,
  rollback on error).
- Records are returned as plain ``dict`` rows in the *web row shape*
  (camelCase ``createdAt``/``updatedAt`` / ``lastHeartbeat`` etc.), the same
  shape the ``/agents`` registry rows already use (see
  :func:`backend.api.endpoints.agents._manifest_to_row`).
- Illegal lifecycle transitions are rejected with
  :class:`IllegalTransitionError` rather than silently accepted.

Incident lifecycle::

    active -> in-progress   (investigation started)
    active|in-progress -> resolved   (remediation verified)
    active|in-progress|resolved -> closed   (terminal)
    closed -> (terminal)

Agents
------
The 14 native agent *types* (spec §26) remain owned by the in-memory
:class:`~backend.agents.registry.AgentRegistry`. :class:`AgentService` persists
*agent instances* created through the API to the ``agents`` table, so
``POST /api/agents`` is a durable write and created agents surface in
``GET /agents``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from backend.agents.registry import AgentRegistry
from backend.persistence.models import Agent as AgentORM
from backend.persistence.models import InferenceCombo as InferenceComboORM
from backend.persistence.models import InferenceProvider as InferenceProviderORM
from backend.persistence.models import Incident as IncidentORM

log = structlog.get_logger(__name__)

__all__ = [
    "AgentService",
    "AgentNotFoundError",
    "AgentNameConflictError",
    "IncidentService",
    "IncidentNotFoundError",
    "IllegalTransitionError",
]


def _iso(dt: datetime | None) -> str | None:
    """Normalize a datetime to an ISO-8601 string (``None`` passthrough)."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.isoformat()


def _utcnow() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# Incidents
# ---------------------------------------------------------------------------


class IncidentNotFoundError(Exception):
    """Raised when an incident id does not resolve to a row in the database."""

    def __init__(self, incident_id: str) -> None:
        super().__init__(f"Incident {incident_id!r} not found")
        self.incident_id = incident_id


class IllegalTransitionError(Exception):
    """Raised when an incident is asked to move to a disallowed status."""

    def __init__(self, incident_id: str, current: str, target: str) -> None:
        super().__init__(
            f"Incident {incident_id!r} cannot transition from {current!r} to {target!r}"
        )
        self.incident_id = incident_id
        self.current = current
        self.target = target


#: Allowed incident status transitions (from-status -> allowed target statuses).
_INCIDENT_TRANSITIONS: dict[str, frozenset[str]] = {
    "active": frozenset({"in-progress", "resolved", "closed"}),
    "in-progress": frozenset({"resolved", "closed"}),
    "resolved": frozenset({"closed"}),
    "closed": frozenset(),
}

#: The statuses an incident can be created in (the rest are terminal/reached).
_INCIDENT_SEVERITIES = {"critical", "high", "medium", "low"}


def _incident_to_row(orm: IncidentORM) -> dict[str, Any]:
    """Project an ORM incident onto the web ``Incident`` row shape."""
    return {
        "id": str(orm.id),
        "title": orm.title,
        "severity": orm.severity,
        "status": orm.status,
        "createdAt": _iso(orm.created_at),
        "updatedAt": _iso(orm.updated_at),
        "description": orm.description,
        "impact": orm.impact,
        "tags": list(orm.tags or []),
    }


class IncidentService:
    """Async CRUD + lifecycle service over the ``incidents`` table."""

    def __init__(
        self,
        engine: AsyncEngine,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
    ) -> None:
        self._engine = engine
        self._session_factory = session_factory

    # -- internal helpers ---------------------------------------------------

    def _factory(self) -> async_sessionmaker[AsyncSession]:
        if self._session_factory is None:
            from backend.persistence.connection import make_session_factory

            return make_session_factory(self._engine)
        return self._session_factory

    async def _load(self, session: AsyncSession, incident_id: str) -> IncidentORM:
        orm = await session.get(IncidentORM, incident_id)
        if orm is None:
            raise IncidentNotFoundError(incident_id)
        return orm

    # -- reads ----------------------------------------------------------------

    async def list_incidents(self) -> list[dict[str, Any]]:
        """Return all persisted incidents as web rows, newest first."""
        factory = self._factory()
        async with factory() as session:
            result = await session.execute(
                select(IncidentORM).order_by(IncidentORM.created_at.desc())
            )
            return [_incident_to_row(r) for r in result.scalars().all()]

    async def get(self, incident_id: str) -> dict[str, Any]:
        """Return one persisted incident as a web row (404 on the API side)."""
        factory = self._factory()
        async with factory() as session:
            orm = await self._load(session, incident_id)
            return _incident_to_row(orm)

    # -- writes ---------------------------------------------------------------

    async def create(
        self,
        *,
        title: str,
        severity: str = "medium",
        description: str | None = None,
        impact: str | None = None,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """Create a new incident and persist it. Returns the web row (201 body)."""
        if severity not in _INCIDENT_SEVERITIES:
            raise ValueError(f"severity must be one of {sorted(_INCIDENT_SEVERITIES)}")
        factory = self._factory()
        async with factory() as session:
            orm = IncidentORM(
                title=title,
                severity=severity,
                status="active",
                description=description,
                impact=impact,
                tags=list(tags or []),
                updated_at=_utcnow(),
            )
            session.add(orm)
            await session.commit()
            await session.refresh(orm)
            log.info("incidents.created", incident_id=str(orm.id), title=title)
            return _incident_to_row(orm)

    async def _transition(self, incident_id: str, target: str) -> dict[str, Any]:
        factory = self._factory()
        async with factory() as session:
            orm = await self._load(session, incident_id)
            current = orm.status
            if target not in _INCIDENT_TRANSITIONS.get(current, frozenset()):
                await session.rollback()
                raise IllegalTransitionError(incident_id, current, target)
            orm.status = target
            orm.updated_at = _utcnow()
            await session.commit()
            await session.refresh(orm)
            log.info(
                "incidents.transitioned",
                incident_id=str(orm.id),
                from_status=current,
                to_status=target,
            )
            return _incident_to_row(orm)

    async def resolve(self, incident_id: str) -> dict[str, Any]:
        """Resolve an incident (active|in-progress -> resolved)."""
        return await self._transition(incident_id, "resolved")

    async def close(self, incident_id: str) -> dict[str, Any]:
        """Close an incident (active|in-progress|resolved -> closed, terminal)."""
        return await self._transition(incident_id, "closed")


# ---------------------------------------------------------------------------
# Agents
# ---------------------------------------------------------------------------


class AgentNotFoundError(Exception):
    """Raised when an agent id does not resolve to a row in the database."""

    def __init__(self, agent_id: str) -> None:
        super().__init__(f"Agent {agent_id!r} not found")
        self.agent_id = agent_id


class AgentNameConflictError(Exception):
    """Raised when an agent name collides with an existing row."""

    def __init__(self, name: str) -> None:
        super().__init__(f"Agent {name!r} already exists")
        self.name = name


def _agent_to_row(orm: AgentORM) -> dict[str, Any]:
    """Project an ORM agent onto the web ``Agent`` row shape.

    Live-telemetry fields (``lastHeartbeat`` / ``cpuUsage`` /
    ``memoryUsage`` / ``tasksCompleted`` / ``tasksFailed``) have no backing
    store, so they are ``None`` (honest) rather than invented numbers — the
    same convention the native registry rows use.
    """
    return {
        "id": str(orm.id),
        "name": orm.name,
        "type": orm.type,
        "status": orm.status,
        "description": orm.description,
        "prompt_template": orm.prompt_template,
        "tools": list(orm.tools or []),
        "capabilities": None,
        "lastHeartbeat": None,
        "cpuUsage": None,
        "memoryUsage": None,
        "tasksCompleted": None,
        "tasksFailed": None,
        "inference": orm.inference,
        "combo_id": orm.combo_id,
    }


class AgentService:
    """Async persistence service over the ``agents`` table (agent instances)."""

    def __init__(
        self,
        engine: AsyncEngine,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
    ) -> None:
        self._engine = engine
        self._session_factory = session_factory
        # The native agent *type* manifests; reused to render registry rows in
        # :meth:`list_all`. (Process-wide, immutable.)
        self._registry = AgentRegistry()

    # -- internal helpers ---------------------------------------------------

    def _factory(self) -> async_sessionmaker[AsyncSession]:
        if self._session_factory is None:
            from backend.persistence.connection import make_session_factory

            return make_session_factory(self._engine)
        return self._session_factory

    async def _load(self, session: AsyncSession, agent_id: str) -> AgentORM:
        orm = await session.get(AgentORM, agent_id)
        if orm is None:
            raise AgentNotFoundError(agent_id)
        return orm

    # -- reads ---------------------------------------------------------------

    async def list_persisted(self) -> list[dict[str, Any]]:
        """Return all persisted agent instances (web rows), newest first."""
        factory = self._factory()
        async with factory() as session:
            result = await session.execute(
                select(AgentORM).order_by(AgentORM.created_at.desc())
            )
            rows = result.scalars().all()
            return [_agent_to_row(r) for r in rows]

    async def list_all(self) -> list[dict[str, Any]]:
        """Return native registry rows followed by persisted agent instances.

        This is what ``GET /agents`` merges when an agent service is wired: the
        14 native agent *types* first, then any agent *instances* created via
        ``POST /api/agents``.
        """
        native = [
            {
                "id": m.agent_type,
                "name": m.name,
                "type": m.agent_type,
                "status": "active",
                "description": m.description,
                "tools": list(m.tools),
                "prompt_template": m.prompt_template,
                "capabilities": None,
                "lastHeartbeat": None,
                "cpuUsage": None,
                "memoryUsage": None,
                "tasksCompleted": None,
                "tasksFailed": None,
            }
            for m in self._registry.list_all()
        ]
        return native + await self.list_persisted()

    # -- writes ---------------------------------------------------------------

    async def create(
        self,
        *,
        name: str,
        agent_type: str,
        description: str | None = None,
        prompt_template: str | None = None,
        tools: list[str] | None = None,
        inference: dict[str, Any] | None = None,
        combo_id: str | None = None,
    ) -> dict[str, Any]:
        """Create and persist a new agent instance. Returns the web row (201).

        ``inference`` is the per-agent binding (contract §3); ``None`` means
        "unbound → default". ``combo_id`` is an optional durable combo binding
        (providers feature) — when set, the agent resolves its binding
        through the combo at ask time (lazy, see :meth:`get_inference`).
        Raises :class:`AgentNameConflictError` when ``name`` collides with an
        existing row (unique constraint).
        """
        factory = self._factory()
        async with factory() as session:
            orm = AgentORM(
                name=name,
                type=agent_type,
                status="active",
                description=description,
                prompt_template=prompt_template,
                tools=list(tools or []),
                inference=inference,
                combo_id=combo_id,
                updated_at=_utcnow(),
            )
            session.add(orm)
            try:
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                log.warning("agents.create_conflict", name=name)
                raise AgentNameConflictError(name) from exc
            await session.refresh(orm)
            log.info("agents.created", agent_id=str(orm.id), name=name)
            return _agent_to_row(orm)

    async def get(self, agent_id: str) -> dict[str, Any]:
        """Return one persisted agent by id, or raise :class:`AgentNotFoundError`."""
        factory = self._factory()
        async with factory() as session:
            orm = await self._load(session, agent_id)
            return _agent_to_row(orm)

    async def get_inference(self, agent_id: str) -> dict[str, Any] | None:
        """Return a persisted agent's inference binding (or ``None``).

        Resolution order: (1) a durable :class:`InferenceCombo` bound via
        ``combo_id`` — the combo's ``(provider.provider_type, combo.model_id)``
        is resolved lazily so re-pointing a combo re-binds every bound agent
        on the next ask (no cascade write); (2) the per-agent ``inference``
        jsonb binding (contract §3); (3) ``None``.

        Returns ``None`` both when the agent has no binding and when the id is
        not a persisted instance (e.g. a native type or an unknown id), so the
        ask endpoint can silently fall through to native-type / default
        resolution (contract §2 step 2).
        """
        factory = self._factory()
        async with factory() as session:
            orm = await session.get(AgentORM, agent_id)
            if orm is None:
                return None
            # (1) Combo binding — wins over the per-agent ``inference`` jsonb.
            if orm.combo_id is not None:
                combo = await session.get(InferenceComboORM, orm.combo_id)
                if combo is not None:
                    provider = await session.get(InferenceProviderORM, combo.provider_id)
                    if provider is not None:
                        return {
                            "provider": provider.provider_type,
                            "model": combo.model_id,
                        }
                # Dangling combo/provider reference — fall through to the
                # per-agent jsonb rather than silently using a dead binding.
                log.warning(
                    "agents.dangling_combo",
                    agent_id=agent_id,
                    combo_id=orm.combo_id,
                )
            # (2) Per-agent jsonb binding.
            return orm.inference

    async def update(
        self,
        agent_id: str,
        *,
        name: str | None = None,
        description: str | None = None,
        prompt_template: str | None = None,
        tools: list[str] | None = None,
        inference: dict[str, Any] | None = None,
        combo_id: str | None = None,
    ) -> dict[str, Any]:
        """Partially update a persisted agent instance; returns the web row.

        ``None`` values leave the corresponding field unchanged (partial
        update, contract §4 "absent/null leaves unchanged"). ``status`` is
        lifecycle, not editable metadata, and is deliberately never touched
        here. ``inference`` is the nullable jsonb binding; a supplied dict
        sets/overwrites it, and ``None`` leaves the column as-is (clearing a
        binding is not a PATCH operation — see contract §4). Raises
        :class:`AgentNotFoundError` when the id is unknown and
        :class:`AgentNameConflictError` when a supplied ``name`` collides with
        another row (mirrors :meth:`create`).
        """
        factory = self._factory()
        async with factory() as session:
            orm = await self._load(session, agent_id)
            if name is not None:
                orm.name = name
            if description is not None:
                orm.description = description
            if prompt_template is not None:
                orm.prompt_template = prompt_template
            if tools is not None:
                orm.tools = list(tools)
            if inference is not None:
                orm.inference = inference
            if combo_id is not None:
                # A supplied id rebinds the agent; an empty string clears the
                # combo binding (back to the per-agent jsonb / default). Omitted
                # (``None``) leaves the existing binding unchanged.
                orm.combo_id = combo_id if combo_id else None
            orm.updated_at = _utcnow()
            # Snapshot the name ahead of the commit: after `rollback()` the ORM
            # is detached, and reading `orm.name` would trigger a lazy DB load
            # outside the greenlet (MissingGreenlet) instead of raising the
            # intended conflict error.
            conflict_name = orm.name
            try:
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                log.warning("agents.update_conflict", agent_id=agent_id)
                raise AgentNameConflictError(conflict_name) from exc
            await session.refresh(orm)
            log.info("agents.updated", agent_id=str(orm.id))
            return _agent_to_row(orm)
