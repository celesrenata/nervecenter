"""Is this email actually a bill? (deterministic gate for mail -> candidates).

The extractor reads the FIRST dollar amount anywhere and the sender name as
payee, with no check that the email is a bill. On real mail (2026-11-14) that
made 127 candidates from 393 emails, 96 of them marketing. This gate runs
before extraction:

* **A bill phrase must be anchored to an amount.** "Amount due: $84.20",
  "your statement balance of $1,204.10", "invoice total $42.00". The amount
  used is that anchored one, not the first price in the email.
* **Already-paid wording is not a bill.** Receipts, "payment received",
  "thank you for your payment", "order total" describe money already spent.
* **Promotional wording disqualifies.** "% off", "save $", "shop now",
  "sale ends": a deadline plus a price is how sales are written.

Pure and network-free. Returns the anchored amount in cents when the email
qualifies, else ``None``; the caller skips the email.
"""
from __future__ import annotations

import html
import re
from dataclasses import dataclass

#: Phrases that name money the reader OWES. Each must have an amount within
#: :data:`_ANCHOR_WINDOW` characters after it.
_OWED = re.compile(
    r"\b(?:"
    r"amount\s+(?:now\s+)?due|total\s+(?:amount\s+)?due|balance\s+due|payment\s+due|"
    r"(?:new|statement|current|outstanding|account)\s+balance|minimum\s+(?:payment|amount)\s+due|"
    r"minimum\s+payment|amount\s+owed|past\s+due\s+amount|invoice\s+(?:total|amount)|"
    r"total\s+(?:to\s+)?pay|please\s+pay|amount\s+to\s+(?:be\s+)?(?:paid|charged)|"
    r"will\s+be\s+(?:charged|debited|withdrawn)|autopay\s+(?:of|amount)|"
    r"scheduled\s+payment\s+of|your\s+(?:bill|invoice|statement)\s+(?:is|for|of)"
    r")\b",
    re.IGNORECASE,
)
#: Flattened HTML tables put the amount in the next cell, well after the label.
_ANCHOR_WINDOW = 160
_NUM = r"(\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)"
#: "$84.20", "USD 12.34", "12.34 USD" (AWS writes the code, not the sign).
_MONEY = re.compile(
    rf"\$\s*{_NUM}(?!\d)|\bUSD\s*{_NUM}(?!\d)|(?<![\d.]){_NUM}\s*USD\b",
    re.IGNORECASE,
)

#: Wording that means the money was already paid / the email is a receipt.
_PAID = re.compile(
    r"\b(?:payment\s+(?:received|confirmed|successful|processed)|thank\s+you\s+for\s+your\s+"
    r"(?:payment|order|purchase)|we\s+(?:received|have\s+received)\s+your\s+payment|"
    r"order\s+(?:confirmation|total|summary)|you\s+paid|receipt\s+for|your\s+receipt|"
    r"has\s+been\s+paid|paid\s+in\s+full|refund(?:ed)?\s+of)\b",
    re.IGNORECASE,
)
#: Promotional wording.
_PROMO = re.compile(
    r"(?:\d{1,2}\s?%\s*off|\bsave\s+(?:up\s+to\s+)?\$|\bshop\s+(?:now|the)|\bsale\s+(?:ends|starts)|"
    r"\bfree\s+shipping|\bpromo\s+code|\buse\s+code\b|\blimited[-\s]time\s+offer|\bdeal\s+of)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class BillSignal:
    amount_cents: int
    phrase: str


def _cents(raw: str) -> int | None:
    try:
        value = float(raw.replace(",", ""))
    except ValueError:
        return None
    cents = int(round(value * 100))
    return cents if 0 < cents <= 10_000_000_00 else None


#: Some senders put HTML inside the text/plain part (seen live: a university
#: billing mail). Tags are dropped (bounded, no backtracking) before matching.
_TAG = re.compile(r"<[^<>]{0,2000}>")
_STYLE = re.compile(
    r"<(style|script)\b[^<>]{0,2000}>.{0,20000}?</\1\s*>", re.IGNORECASE | re.DOTALL
)


def _plain(value: str) -> str:
    return html.unescape(_TAG.sub(" ", _STYLE.sub(" ", value)))


def plain_text(value: str | None) -> str:
    """The text the gate matches on: HTML removed, whitespace collapsed."""
    return " ".join(_plain(value or "").split())


def bill_signal(subject: str | None, text: str | None) -> BillSignal | None:
    """The anchored owed amount if this email reads as a bill, else ``None``."""
    body = " ".join(_plain(f"{subject or ''}\n{text or ''}").split())
    if not body or _PAID.search(body) or _PROMO.search(body):
        return None
    for owed in _OWED.finditer(body):
        window = body[owed.end(): owed.end() + _ANCHOR_WINDOW]
        money = _MONEY.search(window)
        if money is None:
            continue
        cents = _cents(next(g for g in money.groups() if g is not None))
        if cents is not None:
            return BillSignal(amount_cents=cents, phrase=owed.group(0).lower())
    return None


__all__ = ["BillSignal", "bill_signal", "plain_text"]
