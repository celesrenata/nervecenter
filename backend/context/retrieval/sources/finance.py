"""Finance summaries for Ask: spending by payee and month, plus account balances.

Summaries only. The ledger is grouped and summed in the database, so individual ledger lines
(date, amount, description of one transaction) are never loaded. ``finance_ledger`` stays in
``EXCLUDED_STORES``. Money is signed integer cents; spending is the negative side (money out).
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import extract, func, select

from backend.context.retrieval.aggregate import StructuredRow
from backend.context.retrieval.intent import Intent
from backend.context.retrieval.sources._util import make_all
from backend.context.retrieval.types import Record

_ACCOUNT_CAP = 200


def _money(cents: Any) -> str:
    try:
        return f"{int(cents) / 100:.2f}"
    except (TypeError, ValueError):
        return "unknown amount"


def _engine(state: Any) -> Any:
    return getattr(state, "db_engine", None)


async def _load(state: Any, cap: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]] | None:
    """Return ``(accounts, payee_months)`` or None when finance storage is not wired."""
    engine = _engine(state)
    if engine is None:
        return None
    from backend.finance import service
    from backend.persistence.connection import make_session_factory
    from backend.persistence.models.finance import LedgerEntryRow

    accounts = (await service.list_accounts(engine))[:_ACCOUNT_CAP]
    year = extract("year", LedgerEntryRow.entry_date)
    month = extract("month", LedgerEntryRow.entry_date)
    total = func.sum(LedgerEntryRow.amount_cents)
    stmt = (
        select(LedgerEntryRow.description, year.label("y"), month.label("m"),
               total.label("total"), func.count().label("n"))
        .where(LedgerEntryRow.amount_cents < 0)
        .group_by(LedgerEntryRow.description, year, month)
        .order_by(year.desc(), month.desc(), total.asc())
        .limit(cap)
    )
    async with make_session_factory(engine)() as session:
        grouped = (await session.execute(stmt)).all()
    spend = [{"payee": str(r.description or "unknown payee").strip() or "unknown payee",
              "year": int(r.y), "month": int(r.m),
              "spent_cents": -int(r.total or 0), "count": int(r.n)}
             for r in grouped]
    return accounts, spend


def _specs(accounts: list[dict[str, Any]], spend: list[dict[str, Any]], cap: int
           ) -> tuple[list[tuple[Any, ...]], list[dict[str, Any]]]:
    specs: list[tuple[Any, ...]] = []
    fields: list[dict[str, Any]] = []
    for s in spend:
        if len(specs) >= cap:
            break
        when = datetime(s["year"], s["month"], 1, tzinfo=UTC)
        specs.append(("finance", f"spend:{s['year']:04d}-{s['month']:02d}:{s['payee']}",
                      f"Spending: {s['payee']}",
                      f"{_money(s['spent_cents'])} USD spent in {s['year']:04d}-{s['month']:02d} "
                      f"across {s['count']} charges", when))
        fields.append({"amount_cents": s["spent_cents"], "currency": "USD", "status": "spent",
                       "ts": when, "payee": s["payee"]})
    for a in accounts:
        if len(specs) >= cap:
            break
        due = f", due day {a['due_day']}" if a.get("due_day") else ""
        specs.append(("finance", f"account:{a['id']}", f"Account: {a['name']} ({a['institution']})",
                      f"balance {_money(a['balance_cents'])} USD, APR {a['apr_pct']}%, "
                      f"minimum {_money(a['min_payment_cents'])}{due}, as of {a['as_of']}",
                      a["as_of"]))
        fields.append({"amount_cents": a["balance_cents"], "currency": "USD", "status": "balance",
                       "ts": a["as_of"], "payee": a["name"]})
    return specs, fields


async def finance(state: Any, intent: Intent, limit: int) -> list[Record] | None:
    loaded = await _load(state, limit)
    if loaded is None:
        return None
    specs, _ = _specs(*loaded, limit)
    return await make_all(specs)


async def finance_structured(state: Any, intent: Intent, cap: int) -> list[StructuredRow] | None:
    """Every payee-month spending summary and account (up to ``cap``) with typed fields."""
    loaded = await _load(state, cap)
    if loaded is None:
        return None
    specs, fields = _specs(*loaded, cap)
    records = await make_all(specs)
    return [StructuredRow(r, f) for r, f in zip(records, fields, strict=True)]


finance.structured = finance_structured  # type: ignore[attr-defined]  # found by retrieve()
