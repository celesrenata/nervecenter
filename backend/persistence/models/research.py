"""Long-term researcher tables (NS-ADR-103).

``research_projects`` is one research question run over hours or days,
``research_steps`` are the plan nodes, ``research_checkpoints`` is the
append-only resume trail written in the same transaction as each finished
step so a restarted pod continues from the last one.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, UUIDv4


class ResearchProject(Base):
    __tablename__ = "research_projects"

    id: Mapped[UUIDv4]
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    # planned | running | paused | done | failed
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="planned", index=True)
    budget_hours: Mapped[float] = mapped_column(Float, nullable=False, default=4.0)
    cadence_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=300)
    combo: Mapped[str] = mapped_column(String(64), nullable=False, default="secure/planner")
    spent_seconds: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_step_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Owner-written notes; FEAT-006 project context links to this later.
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    report: Mapped[str | None] = mapped_column(Text, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ResearchStep(Base):
    __tablename__ = "research_steps"

    id: Mapped[UUIDv4]
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    project_id: Mapped[str] = mapped_column(
        String, ForeignKey("research_projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    goal: Mapped[str] = mapped_column(Text, nullable=False)
    # pending | running | done | skipped | failed
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    result: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    input_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (UniqueConstraint("project_id", "ordinal", name="uq_research_steps_order"),)


class ResearchCheckpoint(Base):
    __tablename__ = "research_checkpoints"

    id: Mapped[UUIDv4]
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    project_id: Mapped[str] = mapped_column(
        String, ForeignKey("research_projects.id", ondelete="CASCADE"), nullable=False
    )
    # Number of steps finished (done or skipped) when this checkpoint was taken.
    step_cursor: Mapped[int] = mapped_column(Integer, nullable=False)
    notes: Mapped[str] = mapped_column(Text, nullable=False, default="")

    __table_args__ = (Index("ix_research_checkpoints_project", "project_id", "created_at"),)
