"""Accounts, statement import and ledger storage (NS-ADR-102). No money movement."""
from __future__ import annotations

import hashlib
import re
from collections import Counter
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from backend.finance import parsers
from backend.finance.planner import Debt
from backend.persistence.connection import make_session_factory
from backend.persistence.models.finance import DebtAccountRow, LedgerEntryRow, StatementImportRow

INSTITUTIONS = ("BECU", "KeyBank", "Chase", "Apple Card", "PayPal", "other")
KINDS = ("credit_card", "loan", "line_of_credit", "other")


def parse_apr(value: Any) -> str:
    try:
        apr = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        raise ValueError("apr must be a number") from None
    if not apr.is_finite() or apr < 0 or apr > 100:
        raise ValueError("apr must be between 0 and 100")
    return format(apr.normalize(), "f")


def account_dict(row: DebtAccountRow) -> dict[str, Any]:
    return {
        "id": row.id, "institution": row.institution, "kind": row.kind, "name": row.name,
        "balance_cents": row.balance_cents, "apr_pct": row.apr_pct,
        "min_payment_cents": row.min_payment_cents, "due_day": row.due_day,
        "as_of": row.as_of.isoformat(),
    }


def _check(fields: dict[str, Any]) -> dict[str, Any]:
    if "institution" in fields and fields["institution"] not in INSTITUTIONS:
        raise ValueError("unknown institution")
    if "kind" in fields and fields["kind"] not in KINDS:
        raise ValueError("unknown kind")
    if "apr_pct" in fields:
        fields["apr_pct"] = parse_apr(fields["apr_pct"])
    for key in ("balance_cents", "min_payment_cents"):
        if key in fields and fields[key] < 0:
            raise ValueError(f"{key} cannot be negative")
    if fields.get("due_day") is not None and not 1 <= fields["due_day"] <= 31:
        raise ValueError("due_day must be 1 to 31")
    return fields


async def list_accounts(engine: AsyncEngine) -> list[dict[str, Any]]:
    async with make_session_factory(engine)() as session:
        rows = (await session.execute(
            select(DebtAccountRow).order_by(DebtAccountRow.institution, DebtAccountRow.name)
        )).scalars().all()
        return [account_dict(r) for r in rows]


async def create_account(engine: AsyncEngine, **fields: Any) -> dict[str, Any]:
    fields.setdefault("as_of", datetime.now(UTC).date())
    fields.setdefault("apr_pct", "0")
    fields = _check(fields)
    async with make_session_factory(engine)() as session:
        row = DebtAccountRow(**fields)
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return account_dict(row)


async def update_account(
    engine: AsyncEngine, account_id: str, **fields: Any
) -> dict[str, Any] | None:
    fields = _check(fields)
    async with make_session_factory(engine)() as session:
        row = await session.get(DebtAccountRow, account_id)
        if row is None:
            return None
        for key, value in fields.items():
            setattr(row, key, value)
        row.updated_at = datetime.now(UTC)
        await session.commit()
        await session.refresh(row)
        return account_dict(row)


async def delete_account(engine: AsyncEngine, account_id: str) -> bool:
    async with make_session_factory(engine)() as session:
        row = await session.get(DebtAccountRow, account_id)
        if row is None:
            return False
        await session.delete(row)
        await session.commit()
        return True


async def debts(engine: AsyncEngine) -> list[Debt]:
    async with make_session_factory(engine)() as session:
        rows = (await session.execute(select(DebtAccountRow))).scalars().all()
        return [
            Debt(r.id, r.name, r.balance_cents, Decimal(r.apr_pct), r.min_payment_cents)
            for r in rows
        ]


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def dedupe_keys(institution: str, rows: list[parsers.Row]) -> list[str]:
    """Stable per-row keys. FITID wins when present; otherwise date, amount and
    description plus the occurrence number so two identical same-day rows both count."""
    seen: Counter[str] = Counter()
    keys: list[str] = []
    for r in rows:
        if r.fitid:
            base = f"{institution}|fitid|{r.fitid}|{r.entry_date.isoformat()}|{r.amount_cents}"
        else:
            base = (f"{institution}|{r.entry_date.isoformat()}|{r.amount_cents}"
                    f"|{_norm(r.description)}")
        seen[base] += 1
        keys.append(hashlib.sha256(f"{base}|{seen[base]}".encode()).hexdigest())
    return keys


async def import_statement(
    engine: AsyncEngine, data: bytes, *, institution: str, account_id: str | None
) -> dict[str, Any]:
    if institution not in INSTITUTIONS:
        raise ValueError("unknown institution")
    if len(data) > parsers.MAX_BYTES:
        raise OverflowError("file too large")
    digest = hashlib.sha256(data).hexdigest()
    async with make_session_factory(engine)() as session:
        prior = (await session.execute(
            select(StatementImportRow).where(StatementImportRow.file_digest == digest)
        )).scalar_one_or_none()
        if prior is not None:
            return {"duplicate": True, "added": 0, "rows": prior.row_count, "import_id": prior.id}
        if account_id is not None and await session.get(DebtAccountRow, account_id) is None:
            raise ValueError("unknown account")
        rows, fmt = parsers.parse_statement(data)
        keys = dedupe_keys(institution, rows)
        existing: set[str] = set()
        for i in range(0, len(keys), 500):
            chunk = keys[i:i + 500]
            existing.update((await session.execute(
                select(LedgerEntryRow.dedupe_key).where(LedgerEntryRow.dedupe_key.in_(chunk))
            )).scalars().all())
        record = StatementImportRow(
            file_digest=digest, institution=institution, file_format=fmt,
            account_id=account_id, row_count=len(rows),
        )
        session.add(record)
        await session.flush()
        added = 0
        for row, key in zip(rows, keys, strict=True):
            if key in existing:
                continue
            session.add(LedgerEntryRow(
                entry_date=row.entry_date, amount_cents=row.amount_cents,
                description=row.description, institution=institution,
                account_id=account_id, import_id=record.id, dedupe_key=key,
            ))
            added += 1
        record.added_count = added
        await session.commit()
        return {"duplicate": False, "added": added, "rows": len(rows), "format": fmt,
                "import_id": record.id}


async def list_imports(engine: AsyncEngine) -> list[dict[str, Any]]:
    async with make_session_factory(engine)() as session:
        rows = (await session.execute(
            select(StatementImportRow).order_by(StatementImportRow.created_at.desc()).limit(100)
        )).scalars().all()
        return [{"id": r.id, "institution": r.institution, "format": r.file_format,
                 "row_count": r.row_count, "added_count": r.added_count,
                 "created_at": r.created_at.isoformat() if r.created_at else None} for r in rows]


async def list_ledger(engine: AsyncEngine, *, limit: int, offset: int) -> dict[str, Any]:
    async with make_session_factory(engine)() as session:
        count = select(func.count()).select_from(LedgerEntryRow)
        total = (await session.execute(count)).scalar_one()
        rows = (await session.execute(
            select(LedgerEntryRow).order_by(LedgerEntryRow.entry_date.desc(), LedgerEntryRow.id)
            .limit(limit).offset(offset)
        )).scalars().all()
        return {"total": total, "entries": [
            {"id": r.id, "date": r.entry_date.isoformat(), "amount_cents": r.amount_cents,
             "description": r.description, "institution": r.institution,
             "account_id": r.account_id} for r in rows]}


def next_month_start(today: date) -> date:
    return date(today.year + (today.month == 12), today.month % 12 + 1, 1)
