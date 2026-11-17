"""Owner-uploaded resources and their links to agents (NS-ADR-125).

``resources``: metadata only. The bytes live in the encrypted object store under
``object_key`` (``resources/<id>``). ``sensitivity`` is ``normal`` (can be linked to
agents and read by them) or ``secret`` (owner and backend only, never linkable).

``agent_resource_links``: many-to-many between an agent id and a resource.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, UUIDv4


class Resource(Base):
    __tablename__ = "resources"

    id: Mapped[UUIDv4]
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    content_type: Mapped[str] = mapped_column(
        String(100), nullable=False, default="application/octet-stream"
    )
    size: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    #: normal | secret
    sensitivity: Mapped[str] = mapped_column(String(16), nullable=False, default="normal")
    object_key: Mapped[str] = mapped_column(String(200), nullable=False)
    uploaded_by: Mapped[str | None] = mapped_column(String(200), nullable=True)


class AgentResourceLink(Base):
    __tablename__ = "agent_resource_links"

    agent_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    resource_id: Mapped[str] = mapped_column(String, primary_key=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


__all__ = ["AgentResourceLink", "Resource"]
