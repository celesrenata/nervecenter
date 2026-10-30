"""Persistence models package.

This module exports all ORM models used by NerveCenter. Models are grouped
by schema contract:

- :mod:`~backend.persistence.models.base` — declarative base, vector type, mixins.
- :mod:`~backend.persistence.models.events` — :class:`~schemas.events.Event`.
- :mod:`~backend.persistence.models.entities` — :class:`~schemas.entities.Entity`.
- :mod:`~backend.persistence.models.relationships` — :class:`~schemas.relationships.Relationship`.
- :mod:`~backend.persistence.models.evidence` — :class:`~schemas.evidence.Evidence`.
- :mod:`~backend.persistence.models.claims` — Claim, Decision.
- :mod:`~backend.persistence.models.tasks` — Task, TaskRun.
- :mod:`~backend.persistence.models.goals` — Goal, AgentResult.
- :mod:`~backend.persistence.models.planned_changes` — PlannedChange.
- :mod:`~backend.persistence.models.obligations` — :class:`ObligationRow`
  (P2-A unified obligation plane).
- :mod:`~backend.persistence.models.auth` — auth infrastructure (S101):
  :class:`AuthSession`, :class:`AuthDevice`, :class:`PairingChallenge`,
  :class:`ApprovalRecord`.

Usage::

    from backend.persistence.models import Event, Entity, Goal, Task
"""

from .auth import ApprovalRecord, AuthDevice, AuthSession, PairingChallenge
from .base import Base, Jsonb, RootRecord, TimedRecord, UUIDv4, Vector
from .claims import Claim, Decision
from .entities import Entity
from .events import Event
from .evidence import Evidence
from .goals import AgentResult, Goal
from .incidents_agents import Agent, Incident
from .obligations import (
    OBLIGATION_ORIGINS,
    OBLIGATION_STATUSES,
    OBLIGATION_TYPES,
    ObligationRow,
)
from .planned_changes import PlannedChange
from .relationships import Relationship
from .tasks import Task, TaskRun

__all__ = [
    "Base",
    "Vector",
    "TimedRecord",
    "RootRecord",
    "Jsonb",
    "UUIDv4",
    "Event",
    "Entity",
    "Relationship",
    "Evidence",
    "Claim",
    "Decision",
    "Task",
    "TaskRun",
    "Goal",
    "AgentResult",
    "PlannedChange",
    "Incident",
    "Agent",
    "AuthSession",
    "AuthDevice",
    "PairingChallenge",
    "ApprovalRecord",
    "ObligationRow",
    "OBLIGATION_TYPES",
    "OBLIGATION_ORIGINS",
    "OBLIGATION_STATUSES",
]
