"""ORM models for the durable bills plane (Personal-Ops Recovery §7 — workstream G).

This module maps the frozen bills table set from
``specs/personal-ops-recovery/spec.md`` §7 onto Postgres. Bills supply
extraction evidence plus extracted-field confidence, duplicate handling,
upcoming / monthly-calendar views, and escalation hooks. NerveCenter never
assumes payment, transfers money, or purchases — ``confirmed_paid`` is only
ever set by an explicit user action (hard rule).

Conventions (see :mod:`backend.persistence.models.base` /
:mod:`backend.persistence.models.personal`): plural snake_case table names,
string UUID ids (``UUIDv4`` — no database UUID columns), ``Jsonb`` for JSONB,
``DateTime(timezone=True)`` timestamps, explicit ``ix_<table>_<column>``
indexes. Enum-like values (recurrence, status) are stored as
:class:`~sqlalchemy.String` columns, matching the rest of the platform — no
Postgres ``ENUM``/``CHECK`` types, which would break the SQLite scratch
databases the test suite relies on.

Tables
------
- :class:`BillRow`            — ``bills``: an obligation the user may have to
  pay. Carries per-evidence records (source kind + id + snippet + confidence
  map) so every extracted field stays explainable.
- :class:`BillCandidateRow`   — ``bill_candidates``: an extracted *proposal*
  from a communication or uploaded document. Promotion to a bill is an
  EXPLICIT user action (accepting the candidate), never automatic.

Hard rules implemented at the shape level:

- Evidence is preserved: ``BillRow.evidence`` / ``BillCandidateRow.evidence``
  are JSONB arrays of ``{source_kind, source_id, snippet, confidence}``.
- Uncertainties are preserved: ``BillCandidateRow.extracted_fields`` carries
  a per-field confidence map (``{"amount": 0.98, "due_date": 0.62, …}``);
  fields below :data:`~backend.bills.extractor.NEEDS_REVIEW_THRESHOLD` are
  surfaced as *needs review*, never silently filled.
- No auto-confirmation: ``BillRow.status`` defaults to ``pending`` and only
  the repository's explicit ``mark_confirmed_paid`` (user action) flips it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, String, func
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


#: Bill recurrence values (frozen by workstream G's contract; the §6 text's
#: "yearly" is carried as ``other`` + an ``interval`` payload).
BILL_RECURRENCES = ("one-time", "weekly", "monthly", "other")

#: Bill status values (frozen by spec §6/§7). ``confirmed_paid`` is ONLY ever
#: written by an explicit user action — there is no code path that sets it
#: from a passing due date or a scan.
BILL_STATUSES = ("pending", "confirmed_paid", "disputed", "stale")

#: Candidate lifecycle values. Promotion to a bill happens only on the
#: explicit accept action (repository ``accept_candidate``).
CANDIDATE_STATUSES = ("proposed", "accepted", "rejected")


class BillRow(Base):
    """A bill / payment obligation (frozen §7 shape).

    Table ``bills`` columns
    -----------------------
    - ``id`` (UUID) — record row id.
    - ``created_at`` / ``updated_at`` (timestamptz) — provenance.
    - ``schema_version`` (text) — contract version, ``"v0"``.
    - ``payee`` (text) — who the bill is owed to.
    - ``amount_cents`` (text) — amount in minor units, string-decimal so no
      float drift ever enters a financial record (e.g. ``"14250"``).
    - ``currency`` (text) — ISO currency code, defaults ``USD``.
    - ``due_at`` (timestamptz) — the due datetime (tz-aware).
    - ``recurrence`` (text) — ``one-time`` / ``weekly`` / ``monthly`` /
      ``other``.
    - ``interval`` (JSONB) — recurrence interval payload, e.g.
      ``{"weeks": 4}``; ``{}`` for one-time.
    - ``status`` (text) — ``pending`` / ``confirmed_paid`` / ``disputed`` /
      ``stale``; never auto-set to ``confirmed_paid``.
    - ``confirmed_at`` (timestamptz, nullable) — when the user explicitly
      confirmed payment (NULL until then — a passed due date leaves this
      NULL).
    - ``evidence`` (JSONB array) — ``[{source_kind, source_id, snippet,
      confidence}]``; every extracted field keeps its source + confidence.
    - ``duplicate_of`` (self-FK ``bills.id``, nullable) — the original bill
      when this row was detected as a duplicate.
    """

    __tablename__ = "bills"

    id: Mapped[UUIDv4]
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()
    schema_version: Mapped[str] = mapped_column(String(16), default="v0")

    payee: Mapped[str] = mapped_column(String(512), nullable=False)
    amount_cents: Mapped[str] = mapped_column(String(32), nullable=False)
    currency: Mapped[str] = mapped_column(String(8), default="USD", nullable=False)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    recurrence: Mapped[str] = mapped_column(
        String(16), default="one-time", nullable=False
    )
    interval: Mapped[dict[str, Any]] = mapped_column(
        Jsonb, default=dict, nullable=False
    )
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    confirmed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    evidence: Mapped[list[Any]] = mapped_column(Jsonb, default=list, nullable=False)
    duplicate_of: Mapped[str | None] = mapped_column(
        String, ForeignKey("bills.id", ondelete="SET NULL"), nullable=True
    )

    __table_args__ = (
        Index("ix_bills_payee", "payee"),
        Index("ix_bills_due_at", "due_at"),
        Index("ix_bills_status", "status"),
        Index("ix_bills_duplicate_of", "duplicate_of"),
    )


class BillCandidateRow(Base):
    """An extracted bill proposal awaiting an explicit user decision.

    Table ``bill_candidates`` columns
    ---------------------------------
    - ``id`` (UUID) — record row id.
    - ``created_at`` / ``updated_at`` (timestamptz) — provenance.
    - ``schema_version`` (text) — contract version, ``"v0"``.
    - ``source_kind`` (text) — ``email`` / ``document`` / ``communication``.
    - ``source_id`` (text) — the source row id the candidate was extracted
      from (communication id / storage key) — the evidence anchor.
    - ``status`` (text) — ``proposed`` / ``accepted`` / ``rejected``.
    - ``extracted_fields`` (JSONB) — the extracted fields, each carrying its
      own confidence: ``{"payee": {"value": …, "confidence": …}, …}``.
    - ``evidence`` (JSONB array) — ``[{source_kind, source_id, snippet,
      confidence}]`` matched-text evidence (snippet capped, no full bodies).
    - ``needs_review`` (JSONB array) — field names whose confidence fell
      below threshold; surfaced to the user, never silently filled.
    - ``duplicate_of`` (self-FK ``bill_candidates.id``, nullable) — the
      candidate this one duplicates (same payee + amount ± tolerance + due
      date within window).
    - ``bill_id`` (FK ``bills.id``, nullable) — set when the user accepts
      the candidate and it is promoted to a bill.
    """

    __tablename__ = "bill_candidates"

    id: Mapped[UUIDv4]
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()
    schema_version: Mapped[str] = mapped_column(String(16), default="v0")

    source_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    source_id: Mapped[str] = mapped_column(String(512), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="proposed", nullable=False)
    extracted_fields: Mapped[dict[str, Any]] = mapped_column(
        Jsonb, default=dict, nullable=False
    )
    evidence: Mapped[list[Any]] = mapped_column(Jsonb, default=list, nullable=False)
    needs_review: Mapped[list[Any]] = mapped_column(
        Jsonb, default=list, nullable=False
    )
    duplicate_of: Mapped[str | None] = mapped_column(
        String, ForeignKey("bill_candidates.id", ondelete="SET NULL"), nullable=True
    )
    bill_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("bills.id", ondelete="SET NULL"), nullable=True
    )

    __table_args__ = (
        Index("ix_bill_candidates_status", "status"),
        Index("ix_bill_candidates_source_id", "source_id"),
        Index("ix_bill_candidates_bill_id", "bill_id"),
        Index("ix_bill_candidates_duplicate_of", "duplicate_of"),
    )


__all__ = [
    "BILL_RECURRENCES",
    "BILL_STATUSES",
    "CANDIDATE_STATUSES",
    "BillCandidateRow",
    "BillRow",
]
