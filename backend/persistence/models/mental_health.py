"""Mental-health daily forms (NS-ADR-101).

``answers_enc`` and ``comment_enc`` hold base64 AES-256-GCM ciphertext; only the
total score and the date stay readable so trends can be computed.
"""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import Date, DateTime, Index, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, UUIDv4


class MhFormRow(Base):
    """One PHQ-9 or GAD-7 submission for one day."""

    __tablename__ = "mh_forms"

    id: Mapped[UUIDv4]
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    form_date: Mapped[date] = mapped_column(Date, nullable=False)
    instrument: Mapped[str] = mapped_column(String(8), nullable=False)
    total_score: Mapped[int] = mapped_column(Integer, nullable=False)
    answers_enc: Mapped[str] = mapped_column(Text, nullable=False)
    comment_enc: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint("form_date", "instrument", name="uq_mh_forms_date_instrument"),
        Index("ix_mh_forms_form_date", "form_date"),
    )
