"""Agent runner tables: per-agent schedules and run history (NS-ADR-099).

``agent_schedules`` holds one cadence row per agent instance (``agent_id`` is
the primary key, no foreign key so a native type could be scheduled later).
``agent_runs`` is the append-only history the runner writes; the completed /
failed counts shown on the Agents screen are derived from it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, Boolean, DateTime, Index, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, Jsonb, UUIDv4


class AgentSchedule(Base):
    """Cadence for one agent: ``interval`` | ``daily`` | ``on_demand``."""

    __tablename__ = "agent_schedules"

    id: Mapped[UUIDv4]
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    agent_id: Mapped[str] = mapped_column(String, nullable=False, unique=True, index=True)
    mode: Mapped[str] = mapped_column(String(16), nullable=False, default="on_demand")
    interval_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    daily_time: Mapped[str | None] = mapped_column(String(5), nullable=True)  # "HH:MM"
    timezone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    priority: Mapped[str] = mapped_column(String(16), nullable=False, default="background")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    next_run_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AgentRun(Base):
    """One execution of an agent (``completed`` | ``failed`` | ``skipped``)."""

    __tablename__ = "agent_runs"

    id: Mapped[UUIDv4]
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    agent_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    trigger: Mapped[str] = mapped_column(String(16), nullable=False, default="schedule")
    output: Mapped[str | None] = mapped_column(String, nullable=True)
    error: Mapped[str | None] = mapped_column(String, nullable=True)
    input_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    #: Skill Hub skills injected into this run: ``[{id, revision, review_status, chars}]``.
    skills: Mapped[list[dict[str, Any]]] = mapped_column(Jsonb, default=list, nullable=False)
    #: ``loaded`` | ``none`` | ``unavailable`` | ``unconfigured``.
    skills_status: Mapped[str | None] = mapped_column(String(16), nullable=True)

    __table_args__ = (Index("ix_agent_runs_agent_started", "agent_id", "started_at"),)


class AgentOverride(Base):
    """Owner edits layered over a built-in agent's code manifest (NS-ADR-116).

    The manifest stays read-only; this row is the durable override. ``skills``
    and ``inference`` are ``NULL`` when not overridden. Removing the row (and the
    agent's schedule) resets the agent to its defaults.
    """

    __tablename__ = "agent_overrides"

    id: Mapped[UUIDv4]
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    agent_id: Mapped[str] = mapped_column(String, nullable=False, unique=True, index=True)
    prompt_addition: Mapped[str | None] = mapped_column(String, nullable=True)
    skills: Mapped[list[str] | None] = mapped_column(Jsonb, nullable=True)
    inference: Mapped[dict[str, Any] | None] = mapped_column(Jsonb, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
