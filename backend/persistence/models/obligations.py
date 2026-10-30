"""ORM models for the unified obligation plane (P2-A — Personal Reliability).

This module maps the frozen obligation contract
(``docs/contracts/obligation.md``) onto Postgres. The obligation plane is the
durable backbone for P2's "remember what matters" layer: it unifies the
actionable subset of the existing bills / calendar / routines / wellness /
communications planes behind ONE identity, provenance, and status contract —
without replacing those planes.

Conventions match :mod:`backend.persistence.models.bills` / ``.personal``:
plural snake_case table names, string UUID ids (``UUIDv4`` — no database UUID
columns), :class:`~sqlalchemy.dialects.postgresql.JSONB` for JSONB,
``DateTime(timezone=True)`` timestamps, explicit ``ix_<table>_<column>``
indexes. Enum-like values (type, origin, status) are stored as
:class:`~sqlalchemy.String` columns — no Postgres ``ENUM``/``CHECK`` types,
which would break the SQLite scratch databases the test suite relies on.

Tables
------
- :class:`ObligationRow` — ``obligations``: a single actionable item the user
  is responsible for.

Hard rules implemented at the shape level (contract:
``docs/contracts/obligation.md``):

- **Stable identity** — ``id`` + ``source_account`` + ``owner_id`` +
  ``dedup_key`` (unique). Two records with the same ``dedup_key`` are the
  same obligation; the repository upserts (merges evidence) rather than
  creating a second row.
- **Provenance** — ``evidence`` is a JSONB array of
  ``{source_kind, source_id, snippet, confidence}`` (same shape as bills), so
  every field stays explainable.
- **Origin semantics** — ``origin`` is ``explicit`` (user-asserted or
  first-party source) or ``inferred`` (AI-derived). Inferred obligations never
  auto-execute; they require explicit acceptance to become actionable.
- **Freshness** — ``last_verified_at`` tracks when the underlying source was
  last verified; the service flips ``status`` to ``stale`` when it is old
  (distinct from "no items" in Today).
- **Terminal states** — ``done`` / ``dismissed`` are terminal; only ``stale``
  re-enters ``active`` on re-verification. Transitions are written by the
  repository only.
- **No execution** — the plane records *what* and *why*; performing any
  action tied to an obligation goes through the P3 ActionProposal + PDP
  boundary. The obligation plane itself performs no side effects.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Index, String, func
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


#: Obligation type values (frozen by the P2-A contract).
OBLIGATION_TYPES = (
    "obligation",
    "commitment",
    "task",
    "deadline",
    "appointment",
    "bill",
    "routine",
    "follow_up",
    "waiting_on",
)

#: Origin values. ``explicit`` = user-asserted / first-party; ``inferred`` =
#: AI-derived (requires acceptance before it is actionable).
OBLIGATION_ORIGINS = ("explicit", "inferred")

#: Status values. ``done`` / ``dismissed`` are terminal; only ``stale``
#: re-enters ``active`` on re-verification. ``snoozed`` is not terminal — when
#: the snooze window passes, the item returns to ``active`` (or ``stale`` if
#: the source is unverified).
OBLIGATION_STATUSES = (
    "active",
    "scheduled",
    "in_progress",
    "waiting",
    "done",
    "dismissed",
    "snoozed",
    "stale",
)


class ObligationRow(Base):
    """A single actionable personal obligation (frozen P2-A shape).

    The obligation *references* plane records via ``source_record_ref``
    (``{plane, id}``); it does not replace bills / calendar / routines /
    wellness. Payment confirmation, for example, still happens only in the
    bills plane and is mirrored back here.
    """

    __tablename__ = "obligations"
    __table_args__ = (
        Index("ix_obligations_status", "status"),
        Index("ix_obligations_due_at", "due_at"),
    )

    id: Mapped[UUIDv4]
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()
    schema_version: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="v0"
    )

    obligation_type: Mapped[str] = mapped_column(String(32), nullable=False)
    origin: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="inferred"
    )
    source_account: Mapped[str] = mapped_column(
        String(128), nullable=False, server_default=""
    )
    owner_id: Mapped[str] = mapped_column(
        String(64), nullable=False, server_default="default"
    )
    dedup_key: Mapped[str] = mapped_column(
        String(255), nullable=False, unique=True, index=True
    )
    title: Mapped[str] = mapped_column(String, nullable=False)

    due_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    trigger: Mapped[dict] = mapped_column(
        Jsonb, default=dict, nullable=False
    )
    confidence: Mapped[float] = mapped_column(nullable=False, server_default="1.0")
    uncertainty: Mapped[dict] = mapped_column(
        Jsonb, default=dict, nullable=False
    )
    evidence: Mapped[list] = mapped_column(
        Jsonb, default=list, nullable=False
    )

    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="active"
    )
    next_action: Mapped[str | None] = mapped_column(String, nullable=True)
    suggested_action: Mapped[dict | None] = mapped_column(Jsonb, nullable=True)
    priority: Mapped[int] = mapped_column(nullable=False, server_default="1")
    consequence: Mapped[str | None] = mapped_column(String, nullable=True)

    last_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    snooze_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    dismissed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    relationships: Mapped[list] = mapped_column(
        Jsonb, default=list, nullable=False
    )
    source_record_ref: Mapped[dict] = mapped_column(
        Jsonb, default=dict, nullable=False
    )
