"""Shared project context store (NS-ADR-104).

NerveCenter is the source of truth. ``project_context_notes`` is append-only:
no code path updates or deletes a note (a note is only removed with its project).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, UUIDv4


class ProjectContext(Base):
    __tablename__ = "project_contexts"

    id: Mapped[UUIDv4]
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    slug: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")

    __table_args__ = (UniqueConstraint("slug", name="uq_project_contexts_slug"),)


class ProjectContextNote(Base):
    __tablename__ = "project_context_notes"

    id: Mapped[UUIDv4]
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    project_id: Mapped[str] = mapped_column(
        String, ForeignKey("project_contexts.id", ondelete="CASCADE"), nullable=False
    )
    # agent | owner | chatgpt | other client name (lowercase, max 32)
    author: Mapped[str] = mapped_column(String(32), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (Index("ix_project_context_notes_project", "project_id", "created_at"),)
