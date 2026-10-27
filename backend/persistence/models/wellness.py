"""ORM model for the private Wellness Journal (Personal-Ops Recovery §10).

Creates :class:`WellnessEntryRow` — one durable row per day of user
check-ins (mood, sleep duration/consistency, energy, stress, free-text
journal, custom key-value observations). The table is created by the
``010_wellness`` alembic revision (``down_revision`` = the current head
``007_calendar`` in this worktree; the integrator rewires the linear
chain afterward).

Privacy (spec §10 — non-negotiable)
-----------------------------------
- **Every row is ``personal``-sensitive (highest tier) at rest.** The
  platform's personal-sensitivity label is applied in the service layer
  (:mod:`backend.wellness.service`) via the module constant
  :data:`WELLNESS_SENSITIVITY` — deliberately a constant, not a per-row
  column: the §6 freeze defines the wellness table as "time, nullable
  mood/sleep, consistency, energy/stress, journal, observations" and the
  table is 100% personal-scope by construction, so a per-row column
  would be dead weight.
- **No external-model path exists or is permitted.** Nothing in this
  package (or in :mod:`backend.wellness`) sends wellness rows to
  inference / OmniRoute / cloud transports. Trends are computed
  deterministically (means, least-squares slope, Pearson r) —
  statistics only, never diagnosis language.

Conventions match :mod:`backend.persistence.models.personal` and
:mod:`backend.persistence.models.base`: string UUID ids (no database
UUID columns), ``Jsonb`` for JSONB, ``DateTime(timezone=True)``, and
``ix_<table>_<column>`` index names.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, Jsonb, UUIDv4

#: §10 — all wellness rows carry the personal-plane sensitivity label
#: (the highest-sensitivity §36 class). Applied in the service layer;
#: see the module docstring for why it is a constant rather than a column.
WELLNESS_SENSITIVITY = "personal"


def _created_at() -> Any:
    """Return a ``created_at`` column spec (timestamptz, server ``now()``)."""
    return mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


def _updated_at() -> Any:
    """Return an ``updated_at`` column spec (timestamptz, server ``now()``)."""
    return mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class WellnessEntryRow(Base):
    """A single wellness check-in (one row per day — upsert by day).

    Table ``wellness_entries`` columns
    ----------------------------------
    - ``id`` (UUID) — record row id.
    - ``created_at`` / ``updated_at`` (timestamptz) — provenance.
    - ``schema_version`` (text) — contract version, ``"v0"``.
    - ``at`` (timestamptz) — the check-in moment; its *date* is the upsert
      key (one check-in per day: re-saving the same day updates in place,
      enforced by the repository's upsert, not a DB constraint).
    - ``mood`` (int, nullable) — user self-rating 1..5 (5 = best).
    - ``sleep_minutes`` (int, nullable) — self-reported sleep duration.
    - ``sleep_consistency`` (JSONB, nullable) — consistency detail
      (e.g. ``{"stdev_minutes": 34, "regularity": "stable"}``).
    - ``energy`` (int, nullable) — 1..10.
    - ``stress`` (int, nullable) — 1..10.
    - ``journal_entry`` (text, nullable) — free-text journal.
    - ``observations`` (JSONB, nullable) — custom key-value observations.
    """

    __tablename__ = "wellness_entries"

    id: Mapped[UUIDv4]
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()
    schema_version: Mapped[str] = mapped_column(String(16), default="v0")

    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    mood: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sleep_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sleep_consistency: Mapped[dict[str, Any] | None] = mapped_column(
        Jsonb, nullable=True
    )
    energy: Mapped[int | None] = mapped_column(Integer, nullable=True)
    stress: Mapped[int | None] = mapped_column(Integer, nullable=True)
    journal_entry: Mapped[str | None] = mapped_column(Text, nullable=True)
    observations: Mapped[dict[str, Any] | None] = mapped_column(Jsonb, nullable=True)

    __table_args__ = (Index("ix_wellness_entries_at", "at"),)
