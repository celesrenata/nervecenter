"""ORM models for the routines and sleep plane (Personal-Ops Recovery §9 — workstream H).

This module maps the frozen ``routines`` / ``routine_check_ins`` / sleep tables
onto Postgres. The durable backbone is read by the Routine Engine
(:mod:`backend.routines.service`) and the ``/api/routines`` + ``/api/health``
endpoints, so the columns here are the contract.

Freeze rules encoded by the column set (spec §9 HARD RULES):

- A **missed routine is a one-time gentle note only** — there are *no* streak
  counters and no guilt fields anywhere. The one-time "already flagged" marker
  lives in the ``routines.missed_flags`` JSONB (per-schedule-slot window keys),
  so re-evaluating the same missed window never re-flags it.
- **Missing sleep data never proves asleep or awake.** Sleep records are
  explicit user/HealthKit-supplied rows; a query window with too few records
  reports ``regularity: unknown`` — never an assertion of state.
- **Wake-up requires a deterministic clock-based fallback alarm.** The
  ``routines.deterministic_fallback_alarm`` JSONB holds a server-computed
  clock-based alarm (``{at, timezone, trigger_level, …}``) that fires via
  NerveCenter's own durable notification path — with *no* dependence on Apple
  Health, device wake, or any device push channel.

Conventions (see :mod:`backend.persistence.models.base`):
plural snake_case table names, string UUID ids (``UUIDv4`` — no database UUID
columns), ``Jsonb`` for JSONB, ``DateTime(timezone=True)`` timestamps, and
explicit ``ix_<table>_<column>`` indexes. Enum-like values (kind, source,
state) are stored as :class:`~sqlalchemy.String` columns, matching the rest of
the platform — no Postgres ``ENUM``/``CHECK`` types, which would break the
SQLite scratch databases the test suite relies on.

Tables
------
- :class:`RoutineRow`        — ``routines``.
- :class:`RoutineCheckInRow` — ``routine_check_ins``.
- :class:`SleepRecordRow`    — ``sleep_records``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, Jsonb, UUIDv4


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


#: Routine kinds (frozen by spec §9).
ROUTINE_KINDS = (
    "wake",
    "workday_prep",
    "bedtime",
    "morning_briefing",
    "appointment_prep",
    "medication",
    "check_in",
)

#: Sleep record sources (frozen by spec §9).
SLEEP_SOURCES = ("apple_health", "manual")


class RoutineRow(Base):
    """A durable user routine (wake, workday prep, bedtime, briefing, …).

    Table ``routines`` columns
    --------------------------
    - ``id`` (UUID) — record row id.
    - ``kind`` (text) — one of :data:`ROUTINE_KINDS`.
    - ``title`` (text) — human label shown in Today / the routines view.
    - ``schedule`` (JSONB) — ``{"times": ["07:30"], "weekdays": [0,1,2,3,4,5,6],
      "timezone": "America/Los_Angeles", …}``. ``times`` are ``HH:MM`` wall
      clock strings; ``weekdays`` are ISO-8601 day numbers (0=Monday … 6=Sunday);
      an empty ``weekdays`` list means "every day". ``timezone`` is the IANA
      zone the wall-clock times are authored in.
    - ``enabled`` (bool) — the routine participates in evaluation only when on.
    - ``last_completed_at`` (timestamptz, nullable) — the most recent
      completion (check-in or explicit complete). Drives missed-window dedup
      and the per-day completion view.
    - ``deterministic_fallback_alarm`` (JSONB) — a server-computed, clock-based
      fallback alarm: ``{"at": iso, "timezone": IANA, "trigger_level": str,
      "basis": "clock"}``. For a ``wake`` routine this is the deterministic
      clock-based alarm that fires through NerveCenter's own notification path
      (a durable attention item + a ``notification_log`` entry) — it never
      depends on Apple Health or device wake. Empty dict when not applicable.
    - ``missed_flags`` (JSONB) — one-time gentle "missed" markers keyed by
      schedule window (e.g. ``"2026-10-27T07:30"``). Re-evaluating the same
      window finds the marker and does not re-flag. Never a streak counter.
    - ``created_at`` / ``updated_at`` (timestamptz) — provenance.
    - ``schema_version`` (text) — contract version, ``"v0"``.
    """

    __tablename__ = "routines"

    id: Mapped[UUIDv4]
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()
    schema_version: Mapped[str] = mapped_column(String(16), default="v0")

    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    schedule: Mapped[dict[str, Any]] = mapped_column(
        Jsonb, default=dict, nullable=False
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    deterministic_fallback_alarm: Mapped[dict[str, Any]] = mapped_column(
        Jsonb, default=dict, nullable=False
    )
    missed_flags: Mapped[dict[str, Any]] = mapped_column(
        Jsonb, default=dict, nullable=False
    )

    __table_args__ = (
        Index("ix_routines_kind", "kind"),
        Index("ix_routines_enabled", "enabled"),
    )


class RoutineCheckInRow(Base):
    """A completion record for a routine (check-in / explicit complete).

    Table ``routine_check_ins`` columns
    -----------------------------------
    - ``id`` (UUID) — record row id.
    - ``routine_id`` (FK ``routines.id``, CASCADE) — the routine completed.
    - ``at`` (timestamptz) — when the routine was completed (the completion
      timestamp; not necessarily the schedule slot time).
    - ``completed`` (bool) — ``true`` for a completion, ``false`` when the user
      explicitly skipped/declined (the routine is still "handled" either way —
      no guilt either direction).
    - ``note`` (text, nullable) — free-form note captured at check-in time.
    - ``created_at`` (timestamptz) — provenance.
    """

    __tablename__ = "routine_check_ins"

    id: Mapped[UUIDv4]
    created_at: Mapped[datetime] = _created_at()
    schema_version: Mapped[str] = mapped_column(String(16), default="v0")

    routine_id: Mapped[str] = mapped_column(
        String, ForeignKey("routines.id", ondelete="CASCADE"), nullable=False
    )
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("ix_routine_check_ins_routine_id", "routine_id"),
        Index("ix_routine_check_ins_at", "at"),
    )


class SleepRecordRow(Base):
    """An explicit sleep record (Apple Health relay or manual entry).

    Table ``sleep_records`` columns
    --------------------------------
    - ``id`` (UUID) — record row id.
    - ``bed_at`` (timestamptz) — when the user went to bed.
    - ``wake_at`` (timestamptz, nullable) — when the user woke up. A record may
      be partial (bedtime-only) until a wake time is supplied.
    - ``source`` (text) — ``apple_health`` / ``manual`` (frozen §9).
    - ``duration_minutes`` (int, nullable) — sleep duration **computed on
      write** from ``wake_at - bed_at``; ``None`` while ``wake_at`` is absent.
    - ``consent_granted`` (bool) — the user explicitly consented to this
      record being stored. Records ingested without consent are rejected (422);
      the column exists so the stored fact is inspectable and the consent state
      is durable, not just request-scoped.
    - ``created_at`` (timestamptz) — provenance.

    Missing data is *unknown*, never an assertion: a window with fewer than
    three records reports ``regularity: unknown`` (see
    :mod:`backend.routines.service`).
    """

    __tablename__ = "sleep_records"

    id: Mapped[UUIDv4]
    created_at: Mapped[datetime] = _created_at()
    schema_version: Mapped[str] = mapped_column(String(16), default="v0")

    bed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    wake_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    duration_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    consent_granted: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    __table_args__ = (
        Index("ix_sleep_records_bed_at", "bed_at"),
        Index("ix_sleep_records_source", "source"),
    )
