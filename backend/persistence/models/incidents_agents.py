"""Incident and Agent ORM models for the round-3 write routes (IN-1 / AG-3).

Before this module existed, the web audit flagged two missing write paths:

- ``IN-1`` — the backend exposed no incident create/resolve/close routes, so
  the web "Report Incident" form had no honest write target.
- ``AG-3`` — the backend exposed no agent create route (``POST /api/agents``
  was absent; only ``GET /agents`` and a ``501`` ``POST /agents/pause`` stub
  existed), so the web "+ Add Agent" button had to stay disabled.

Both collections are backed by real tables now.

Incidents table columns
-----------------------
- ``id`` (UUID) — immutable record identifier (generated v4).
- ``created_at`` / ``updated_at`` (timestamp) — record provenance.
- ``schema_version`` (text) — always "v0" for this version.
- ``title`` (text) — short statement of the incident.
- ``severity`` (text) — ``critical | high | medium | low``.
- ``status`` (text) — lifecycle (``active | in-progress | resolved | closed``).
- ``description`` (text, nullable) — what happened.
- ``impact`` (text, nullable) — business/operational impact.
- ``tags`` (JSONB array of text) — free-form tags.

Lifecycle::

    active -> in-progress  (investigation started)
    active|in-progress -> resolved  (remediation verified)
    active|in-progress|resolved -> closed  (terminal)
    closed -> (terminal)

Agents table columns
--------------------
- ``id`` (UUID) — immutable record identifier (generated v4).
- ``created_at`` / ``updated_at`` (timestamp) — record provenance.
- ``schema_version`` (text) — always "v0" for this version.
- ``name`` (text) — human-readable agent name (unique).
- ``type`` (text) — agent type/category.
- ``status`` (text) — lifecycle (``active | inactive | paused | error``).
- ``description`` (text, nullable) — what the agent does.
- ``prompt_template`` (text, nullable) — system prompt template.
- ``tools`` (JSONB array of text) — tool names the agent may use.

The 14 native agent *types* (spec §26) remain owned by the in-memory
:class:`~backend.agents.registry.AgentRegistry`; this table stores *agent
instances* created through the API so that ``POST /api/agents`` is a durable
write and created agents show up in ``GET /agents``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Index, String, func
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, Jsonb, UUIDv4


class Incident(Base):
    """ORM mapping of an incident record (web ``Incident`` row)."""

    __tablename__ = "incidents"

    id: Mapped[UUIDv4]
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    schema_version: Mapped[str] = mapped_column(String(16), default="v0")

    title: Mapped[str] = mapped_column(String, nullable=False)
    severity: Mapped[str] = mapped_column(String(16), default="medium", nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="active", nullable=False, index=True)
    description: Mapped[str | None] = mapped_column(String, nullable=True)
    impact: Mapped[str | None] = mapped_column(String, nullable=True)
    tags: Mapped[list[str]] = mapped_column(Jsonb, default=list, nullable=False)

    __table_args__ = (
        Index("ix_incidents_status_severity", "status", "severity"),
    )

    def __repr__(self) -> str:
        return f"<Incident id={self.id!r} title={self.title!r} status={self.status!r}>"


class Agent(Base):
    """ORM mapping of a persisted agent instance (web ``Agent`` row)."""

    __tablename__ = "agents"

    id: Mapped[UUIDv4]
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    schema_version: Mapped[str] = mapped_column(String(16), default="v0")

    name: Mapped[str] = mapped_column(String, nullable=False, unique=True, index=True)
    type: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="active", nullable=False, index=True)
    description: Mapped[str | None] = mapped_column(String, nullable=True)
    prompt_template: Mapped[str | None] = mapped_column(String, nullable=True)
    tools: Mapped[list[str]] = mapped_column(Jsonb, default=list, nullable=False)
    #: Optional per-agent inference binding (contract §3): ``{"provider": str,
    #: "model": str}`` (either key optional) or ``None`` (use the default).
    #: NULL-allowed jsonb — a NULL column means "unbound → default".
    inference: Mapped[dict[str, Any] | None] = mapped_column(Jsonb, default=None, nullable=True)
    #: Optional durable combo binding (providers feature): the ``id`` of an
    #: ``inference_combos`` row the agent binds to. ``None`` means "use the
    #: per-agent ``inference`` binding or the service default." Resolved in
    #: the ask pipeline by mirroring the combo's ``(provider, model)`` into the
    #: ``inference`` column before the standard resolution order.
    combo_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)

    def __repr__(self) -> str:
        return f"<Agent id={self.id!r} name={self.name!r} status={self.status!r}>"
