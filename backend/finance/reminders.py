"""Reminders: upcoming and overdue bills plus card due dates (NS-ADR-102).

Bills come from the existing bills plane and its deterministic escalation
ladder; their attention items are already mirrored by the bills endpoints.
Card due dates are recurring and the app cannot see payments, so a card is
never reported overdue: it is "due soon" within 7 days (level 1) or 2 days
(level 2). Level >= 1 cards get one attention item per due date. Nothing here
pays anything.
"""
from __future__ import annotations

import calendar
from datetime import date, datetime
from typing import Any

import structlog

from backend.bills.service import _bill_amount_cents, bill_escalation_level

log = structlog.get_logger(__name__)

CARD_SOON_DAYS = 7
CARD_URGENT_DAYS = 2
BILL_WINDOW_DAYS = 14


def next_due(due_day: int, today: date) -> date:
    def clamp(y: int, m: int) -> date:
        return date(y, m, min(due_day, calendar.monthrange(y, m)[1]))

    this = clamp(today.year, today.month)
    if this >= today:
        return this
    return clamp(today.year + (today.month == 12), today.month % 12 + 1)


def card_reminders(accounts: list[dict[str, Any]], today: date) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for acc in accounts:
        if acc.get("due_day") is None or acc.get("balance_cents", 0) <= 0:
            continue
        due = next_due(acc["due_day"], today)
        days = (due - today).days
        level = 2 if days <= CARD_URGENT_DAYS else 1 if days <= CARD_SOON_DAYS else 0
        out.append({
            "kind": "card_due", "id": acc["id"],
            "title": f"{acc['name']} ({acc['institution']}) payment due",
            "due_date": due.isoformat(), "days_until": days, "level": level,
            "amount_cents": acc.get("min_payment_cents", 0),
            "amount_label": "minimum payment",
        })
    return out


def bill_reminders(bills: list[Any], now: datetime) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for bill in bills:
        level = bill_escalation_level(bill, now)
        if level is None:
            continue
        due = bill.due_at if bill.due_at.tzinfo else bill.due_at.replace(tzinfo=now.tzinfo)
        days = (due.date() - now.date()).days
        if level == 0 and days > BILL_WINDOW_DAYS:
            continue
        out.append({
            "kind": "bill", "id": bill.id, "title": f"{bill.payee} bill",
            "due_date": due.date().isoformat(), "days_until": days, "level": level,
            "overdue": days < 0, "amount_cents": int(_bill_amount_cents(bill)),
            "amount_label": "amount due",
        })
    return out


async def mirror_card_attention(registry: Any, reminders: list[dict[str, Any]]) -> int:
    """Create one active attention item per card due date at level >= 1. Returns new items."""
    if registry is None:
        return 0
    created = 0
    try:
        existing = await registry.attention.list_attention_items(state="active")
        refs = {r for item in existing for r in list(item.source_refs or [])}
        for rem in reminders:
            if rem["kind"] != "card_due" or rem["level"] < 1:
                continue
            ref = f"debt:{rem['id']}:{rem['due_date']}"
            if ref in refs:
                continue
            await registry.attention.create_attention_item(
                title=f"{rem['title']} in {rem['days_until']} days"
                if rem["days_until"] else f"{rem['title']} today",
                body="Card due date from your debt list. NerveCenter never pays this; "
                     "it only reminds you.",
                urgency="high" if rem["level"] >= 2 else "normal",
                importance=7 if rem["level"] >= 2 else 5,
                deadline_at=datetime.fromisoformat(rem["due_date"] + "T12:00:00+00:00"),
                source_refs=[ref],
                recommended_action="Pay it yourself in your bank or card app",
                escalation_policy={"finance.card_level": rem["level"]},
            )
            created += 1
    except Exception as exc:  # noqa: BLE001 - reminders still return without attention
        log.warning("finance.attention_mirror_failed", error=type(exc).__name__)
    return created
