"""Debt payoff planner: avalanche and snowball (NS-ADR-102).

Exact decimal math. Monthly interest is ``balance * APR / 12`` rounded half up
to the cent, per debt. Each month every debt gets its minimum (capped at what
it owes); the rest of the budget (sum of original minimums plus the extra
payment, so freed minimums roll over) goes to debts in strategy order.
Baseline for "interest saved" is minimums only, no rollover.
A plan that does not finish within MAX_MONTHS is reported as never reached,
with no payoff date and no interest total.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

MAX_MONTHS = 600
_CENT = Decimal("0.01")
_TWELVE_HUNDRED = Decimal(1200)


@dataclass(frozen=True)
class Debt:
    id: str
    name: str
    balance_cents: int
    apr_pct: Decimal
    min_payment_cents: int


def _interest(balance_cents: int, apr_pct: Decimal) -> int:
    raw = Decimal(balance_cents) * apr_pct / _TWELVE_HUNDRED
    return int(raw.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def _order(debts: list[Debt], strategy: str) -> list[Debt]:
    if strategy == "avalanche":
        return sorted(debts, key=lambda d: (-d.apr_pct, d.balance_cents, d.id))
    if strategy == "snowball":
        return sorted(debts, key=lambda d: (d.balance_cents, -d.apr_pct, d.id))
    raise ValueError("strategy must be avalanche or snowball")


def _add_months(start: date, months: int) -> date:
    idx = start.year * 12 + (start.month - 1) + months
    return date(idx // 12, idx % 12 + 1, 1)


def _simulate(
    debts: list[Debt], *, extra_cents: int, strategy: str | None, start: date
) -> dict[str, Any]:
    bal = {d.id: d.balance_cents for d in debts}
    order = _order(debts, strategy) if strategy else list(debts)
    budget = sum(d.min_payment_cents for d in debts) + extra_cents
    schedule: list[dict[str, Any]] = []
    total_interest = 0
    paid_off_month: dict[str, int] = {}
    for d in debts:
        if bal[d.id] == 0:
            paid_off_month[d.id] = 0
    month = 0
    while any(v > 0 for v in bal.values()) and month < MAX_MONTHS:
        month += 1
        interest = {k: _interest(v, next(d.apr_pct for d in debts if d.id == k)) if v > 0 else 0
                    for k, v in bal.items()}
        for k, i in interest.items():
            bal[k] += i
        paid = dict.fromkeys(bal, 0)
        for d in order:
            pay = min(d.min_payment_cents, bal[d.id])
            bal[d.id] -= pay
            paid[d.id] += pay
        if strategy:
            pool = budget - sum(paid.values())
            for d in order:
                if pool <= 0:
                    break
                pay = min(pool, bal[d.id])
                bal[d.id] -= pay
                paid[d.id] += pay
                pool -= pay
        total_interest += sum(interest.values())
        for k, v in bal.items():
            if v == 0 and k not in paid_off_month:
                paid_off_month[k] = month
        schedule.append({
            "month": month,
            "date": _add_months(start, month - 1).isoformat(),
            "payments": {k: v for k, v in paid.items() if v},
            "interest_cents": sum(interest.values()),
            "balance_cents": sum(bal.values()),
        })
    finished = all(v == 0 for v in bal.values())
    return {
        "never_reached": not finished,
        "months": month if finished else None,
        "payoff_date": schedule[-1]["date"] if finished and schedule else None,
        "total_interest_cents": total_interest if finished else None,
        "remaining_cents": sum(bal.values()),
        "debts": [
            {"id": d.id, "name": d.name, "payoff_month": paid_off_month.get(d.id)}
            for d in debts
        ],
        "schedule": schedule,
    }


def plan(debts: list[Debt], *, extra_cents: int = 0, start: date) -> dict[str, Any]:
    """Compare avalanche and snowball against minimums only."""
    if extra_cents < 0:
        raise ValueError("extra payment cannot be negative")
    active = [d for d in debts if d.balance_cents > 0]
    baseline = _simulate(active, extra_cents=0, strategy=None, start=start)
    out: dict[str, Any] = {
        "extra_cents": extra_cents,
        "total_balance_cents": sum(d.balance_cents for d in active),
        "minimums": {k: v for k, v in baseline.items() if k != "schedule"},
    }
    for strategy in ("avalanche", "snowball"):
        result = _simulate(active, extra_cents=extra_cents, strategy=strategy, start=start)
        base_i, own_i = baseline["total_interest_cents"], result["total_interest_cents"]
        result["interest_saved_cents"] = (
            base_i - own_i if base_i is not None and own_i is not None else None
        )
        out[strategy] = result
    return out
