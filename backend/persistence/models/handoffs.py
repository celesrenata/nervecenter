"""Coding handoff queue (NS-ADR-106).

A handoff is a coding brief for opencode or Menagerie. ``runner`` says who
executes it: ``central`` (the in-cluster opencode server) or ``mac`` (the
owner's Mac, which pulls work). NerveCenter never executes code itself.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Index, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, UUIDv4


class CodingHandoff(Base):
    __tablename__ = "coding_handoffs"

    id: Mapped[UUIDv4]
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # opencode | menagerie
    target: Mapped[str] = mapped_column(String(16), nullable=False)
    # central | mac
    runner: Mapped[str] = mapped_column(String(16), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    brief: Mapped[str] = mapped_column(Text, nullable=False)
    project_slug: Mapped[str | None] = mapped_column(String(64), nullable=True)
    repo: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # owner | agent:<name>
    requested_by: Mapped[str] = mapped_column(String(64), nullable=False, default="owner")
    # queued | claimed | done | failed | cancelled
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued")
    # Only the owner can set this (SSO route). Without it no run may push.
    push_approved: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    claimed_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    result_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    session_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)

    __table_args__ = (Index("ix_coding_handoffs_queue", "runner", "status", "created_at"),)
