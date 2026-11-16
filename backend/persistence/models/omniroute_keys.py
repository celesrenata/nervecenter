"""Audit trail for OmniRoute key changes (NS-ADR-118): who, when, which key, outcome.

The key values themselves live encrypted in ``secrets``; this table never holds
a value.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Index, String, func
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, UUIDv4


class OmniRouteKeyAudit(Base):
    __tablename__ = "omniroute_key_audit"

    id: Mapped[UUIDv4]
    at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    actor: Mapped[str] = mapped_column(String(200), nullable=False)
    key_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    outcome: Mapped[str] = mapped_column(String(64), nullable=False)

    __table_args__ = (Index("ix_omniroute_key_audit_at", "at"),)
