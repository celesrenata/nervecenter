"""OpenAI usage ledger and escalation grants (NS-ADR-107).

``openai_calls`` is the durable record behind the global OpenAI limiter: one row
per attempt (caller, reason code, outcome, tokens, Retry-After). No prompt or
response content is stored. ``escalation_grants`` are owner-set permissions for
a subject (agent, project, handoff) to use OpenAI: ``owner_requested`` (use it)
or ``high_stakes`` (allowed after local attempts fail).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Float, Index, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, UUIDv4


class OpenAICall(Base):
    __tablename__ = "openai_calls"

    id: Mapped[UUIDv4]
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    caller: Mapped[str] = mapped_column(String(64), nullable=False)
    reason: Mapped[str] = mapped_column(String(32), nullable=False)
    # ok | throttled | error
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    retry_after_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    wait_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (Index("ix_openai_calls_created", "created_at"),)


class EscalationGrant(Base):
    __tablename__ = "escalation_grants"

    id: Mapped[UUIDv4]
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # agent | project | handoff
    subject_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(128), nullable=False)
    # owner_requested | high_stakes
    mode: Mapped[str] = mapped_column(String(16), nullable=False)

    __table_args__ = (
        UniqueConstraint("subject_kind", "subject_id", name="uq_escalation_grants_subject"),
    )
