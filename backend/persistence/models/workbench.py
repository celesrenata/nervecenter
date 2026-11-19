"""Workbench persistence (migration 049_workbench).

Projects, mirrored opencode sessions, token-count usage (no prompt or completion
text is stored, Req 8.8), IDE pod state, and the audit log (Req 6.11).
"""
from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    Text,
    UniqueConstraint,
    Uuid,
    false,
)
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class WorkbenchProject(Base):
    __tablename__ = "workbench_projects"
    __table_args__ = (
        # The title-length and slug-regex CHECKs are PostgreSQL-only (char_length, ~)
        # and live in migration 049_workbench; the ORM keeps only portable ones.
        CheckConstraint("kind IN ('opencode','menagerie')", name="ck_workbench_projects_kind"),
        UniqueConstraint("slug", "kind", name="uq_workbench_projects_slug_kind"),
    )

    id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False), primary_key=True, default=lambda: str(uuid4())
    )
    slug: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_activity_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    size_bytes: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    archived: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    push_approved: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )


class WorkbenchSession(Base):
    __tablename__ = "workbench_sessions"

    id: Mapped[str] = mapped_column(Text, primary_key=True)  # opencode session id
    project_id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False), ForeignKey("workbench_projects.id"), nullable=False, index=True
    )
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    model: Mapped[str | None] = mapped_column(Text, nullable=True)
    small_model: Mapped[str | None] = mapped_column(Text, nullable=True)


class WorkbenchModelUsage(Base):
    """Token counts only. No prompt or completion text columns exist."""

    __tablename__ = "workbench_model_usage"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    model: Mapped[str] = mapped_column(Text, nullable=False)
    input_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    output_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class WorkbenchIde(Base):
    __tablename__ = "workbench_ide"

    project_id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False), ForeignKey("workbench_projects.id"), primary_key=True
    )
    pod_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    state: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_traffic_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class WorkbenchAudit(Base):
    __tablename__ = "workbench_audit"
    __table_args__ = (
        CheckConstraint("outcome IN ('success','failure')", name="ck_workbench_audit_outcome"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    project_slug: Mapped[str | None] = mapped_column(Text, nullable=True)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    outcome: Mapped[str] = mapped_column(Text, nullable=False)
