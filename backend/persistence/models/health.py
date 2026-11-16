"""Apple Health samples ingested from the iPhone (NS-ADR-100).

One row per HealthKit sample batch member. ``dedupe_key`` is a server-side
hash of (device, kind, start, end, source) so a replayed batch is a no-op.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Float, Index, String, func
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, UUIDv4


class HealthSampleRow(Base):
    """One Apple Health sample (weight, sleep night, steps, heart rate, ...)."""

    __tablename__ = "health_samples"

    id: Mapped[UUIDv4]
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    device_id: Mapped[str] = mapped_column(String(128), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    value: Mapped[float] = mapped_column(Float, nullable=False)
    unit: Mapped[str] = mapped_column(String(16), nullable=False)
    source: Mapped[str] = mapped_column(String(64), nullable=False, default="apple_health")
    dedupe_key: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)

    __table_args__ = (Index("ix_health_samples_kind_start", "kind", "start_at"),)
