"""Infrastructure connectors and change plans (NS-ADR-120).

``infra_connectors``: a Kubernetes cluster (server + bearer token) or an SSH
host (host/port/user + private key). Credentials live in the encrypted vault
(``secrets``, owner_kind ``infra_connector``); this row holds only non-secret
config and the pinned SSH host keys.

``infra_plans``: a change an agent proposes (plan mode). Nothing runs until the
owner approves it; then each approved step runs exactly as written (act mode).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, Jsonb, UUIDv4


class InfraConnector(Base):
    __tablename__ = "infra_connectors"

    id: Mapped[UUIDv4]
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)  # kubernetes | ssh
    name: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    config: Mapped[dict[str, Any]] = mapped_column(Jsonb, nullable=False, default=dict)
    #: ssh only: unpinned | pending | pinned
    host_key_status: Mapped[str] = mapped_column(String(16), nullable=False, default="unpinned")


class InfraPlan(Base):
    __tablename__ = "infra_plans"

    id: Mapped[UUIDv4]
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    agent_id: Mapped[str | None] = mapped_column(String, nullable=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    summary: Mapped[str] = mapped_column(String, nullable=False, default="")
    steps: Mapped[list[dict[str, Any]]] = mapped_column(Jsonb, nullable=False, default=list)
    verify: Mapped[list[dict[str, Any]]] = mapped_column(Jsonb, nullable=False, default=list)
    rollback: Mapped[str] = mapped_column(String, nullable=False, default="")
    #: proposed | approved | rejected | running | done | failed
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="proposed")
    act_mode: Mapped[str] = mapped_column(String(16), nullable=False, default="opencode")
    decided_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: per-step outcome: [{"index", "ok", "exit", "output"}] (redacted, capped)
    results: Mapped[list[dict[str, Any]]] = mapped_column(Jsonb, nullable=False, default=list)
    handoff_id: Mapped[str | None] = mapped_column(String, nullable=True)


__all__ = ["InfraConnector", "InfraPlan"]
