"""Financial helper tables (NS-ADR-102): manual debt accounts, statement imports, ledger.

Money is integer cents; APR is a decimal string so no float enters a record.
Descriptions follow the bills storage pattern (plain text in Postgres).
"""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import BigInteger, Date, DateTime, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, UUIDv4


class DebtAccountRow(Base):
    """A debt the owner tracks by hand (card, loan, line of credit)."""

    __tablename__ = "debt_accounts"

    id: Mapped[UUIDv4]
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    institution: Mapped[str] = mapped_column(String(32), nullable=False)
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    balance_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    apr_pct: Mapped[str] = mapped_column(String(16), nullable=False, default="0")
    min_payment_cents: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    due_day: Mapped[int | None] = mapped_column(Integer, nullable=True)
    as_of: Mapped[date] = mapped_column(Date, nullable=False)


class StatementImportRow(Base):
    """One imported statement file, identified by its SHA-256 digest."""

    __tablename__ = "statement_imports"

    id: Mapped[UUIDv4]
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    file_digest: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    institution: Mapped[str] = mapped_column(String(32), nullable=False)
    file_format: Mapped[str] = mapped_column(String(8), nullable=False)
    account_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("debt_accounts.id", ondelete="SET NULL"), nullable=True
    )
    row_count: Mapped[int] = mapped_column(Integer, nullable=False)
    added_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class LedgerEntryRow(Base):
    """One transaction. Negative cents mean money out (a charge)."""

    __tablename__ = "ledger_entries"

    id: Mapped[UUIDv4]
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    entry_date: Mapped[date] = mapped_column(Date, nullable=False)
    amount_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    institution: Mapped[str] = mapped_column(String(32), nullable=False)
    account_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("debt_accounts.id", ondelete="SET NULL"), nullable=True
    )
    import_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("statement_imports.id", ondelete="SET NULL"), nullable=True
    )
    dedupe_key: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)

    __table_args__ = (Index("ix_ledger_entries_entry_date", "entry_date"),)
