"""Statement parsers: CSV (generic and Apple Card) and OFX/QFX (NS-ADR-102).

Convention: ``amount_cents`` is signed, negative = money out (a charge).
No logging here: file names and contents never reach the logs.
OFX is read with regular expressions, not an XML parser, because OFX 1.x is
SGML (unclosed tags) and this also avoids entity-expansion attacks.
"""
from __future__ import annotations

import csv
import html
import io
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

MAX_BYTES = 2 * 1024 * 1024
MAX_ROWS = 20_000


class StatementError(ValueError):
    """The file is not a statement this parser understands. Message has no content."""


@dataclass(frozen=True)
class Row:
    entry_date: date
    amount_cents: int
    description: str
    fitid: str | None = None


_DATE_COLS = ("transaction date", "date", "posted date", "posting date", "post date", "trans date")
_DESC_COLS = ("description", "merchant", "memo", "payee", "name", "details")
_AMOUNT_COLS = ("amount", "amount (usd)")
_DEBIT_COLS = ("debit", "withdrawal", "withdrawals")
_CREDIT_COLS = ("credit", "deposit", "deposits")
_DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%Y/%m/%d")


def to_cents(raw: str) -> int:
    text = raw.strip().replace("$", "").replace(",", "").replace(" ", "")
    negative = text.startswith("(") and text.endswith(")")
    text = text.strip("()")
    if text.endswith("-"):
        negative, text = True, text[:-1]
    try:
        value = Decimal(text)
    except InvalidOperation:
        raise StatementError("unreadable amount") from None
    if not value.is_finite():
        raise StatementError("unreadable amount")
    cents = int((value * 100).to_integral_value())
    return -abs(cents) if negative else cents


def parse_date(raw: str) -> date:
    text = raw.strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise StatementError("unreadable date")


def _pick(headers: dict[str, str], names: tuple[str, ...]) -> str | None:
    for name in names:
        if name in headers:
            return headers[name]
    return None


def detect_format(data: bytes) -> str:
    head = data[:4096].upper()
    if b"OFXHEADER" in head or b"<OFX" in head:
        return "ofx"
    return "csv"


def _decode(data: bytes) -> str:
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    raise StatementError("unreadable text encoding")


def parse_csv(
    data: bytes,
    *,
    date_col: str | None = None,
    description_col: str | None = None,
    amount_col: str | None = None,
    invert: bool | None = None,
) -> tuple[list[Row], str]:
    """Return ``(rows, flavor)``; flavor is ``apple_card`` or ``generic``.

    Apple Card's monthly export lists purchases as positive amounts, so those
    are negated; a generic CSV keeps its sign unless ``invert`` is set.
    """
    reader = csv.DictReader(io.StringIO(_decode(data)))
    if not reader.fieldnames:
        raise StatementError("no header row")
    headers = {h.strip().lower(): h for h in reader.fieldnames if h}
    apple = "clearing date" in headers and "amount (usd)" in headers
    dcol = date_col or _pick(headers, _DATE_COLS)
    ncol = description_col or _pick(headers, ("merchant", "description") if apple else _DESC_COLS)
    acol = amount_col or _pick(headers, _AMOUNT_COLS)
    debit = _pick(headers, _DEBIT_COLS) if acol is None else None
    credit = _pick(headers, _CREDIT_COLS) if acol is None else None
    if dcol is None or ncol is None or (acol is None and debit is None and credit is None):
        raise StatementError("could not find date, description and amount columns")
    flip = apple if invert is None else invert
    rows: list[Row] = []
    for rec in reader:
        if len(rows) >= MAX_ROWS:
            raise StatementError("too many rows")
        if not any((v or "").strip() for v in rec.values() if isinstance(v, str)):
            continue
        day = parse_date(rec.get(dcol) or "")
        if acol is not None:
            cents = to_cents(rec.get(acol) or "")
        else:
            out = to_cents(rec.get(debit) or "0") if debit else 0
            into = to_cents(rec.get(credit) or "0") if credit else 0
            cents = abs(into) - abs(out)
        rows.append(Row(day, -cents if flip else cents, (rec.get(ncol) or "").strip()[:500]))
    return rows, "apple_card" if apple else "generic"


_STMTTRN = re.compile(r"<STMTTRN>(.*?)(?=</STMTTRN>|<STMTTRN>|</BANKTRANLIST>|</CCSTMTRS>|$)",
                      re.IGNORECASE | re.DOTALL)


def _tag(block: str, name: str) -> str | None:
    m = re.search(rf"<{name}>([^<\r\n]*)", block, re.IGNORECASE)
    return m.group(1).strip() if m else None


def parse_ofx(data: bytes) -> list[Row]:
    text = _decode(data)
    rows: list[Row] = []
    for m in _STMTTRN.finditer(text):
        if len(rows) >= MAX_ROWS:
            raise StatementError("too many rows")
        block = m.group(1)
        posted, amount = _tag(block, "DTPOSTED"), _tag(block, "TRNAMT")
        if not posted or amount is None or len(posted) < 8 or not posted[:8].isdigit():
            raise StatementError("malformed transaction")
        day = datetime.strptime(posted[:8], "%Y%m%d").date()
        desc = " ".join(p for p in (_tag(block, "NAME"), _tag(block, "MEMO")) if p)
        fitid = _tag(block, "FITID") or None
        rows.append(Row(day, to_cents(amount), html.unescape(desc)[:500], fitid))
    if not rows:
        raise StatementError("no transactions found")
    return rows


def parse_statement(data: bytes) -> tuple[list[Row], str]:
    """Detect the format and parse. Returns ``(rows, format)`` where format is csv or ofx."""
    if not data.strip():
        raise StatementError("empty file")
    if detect_format(data) == "ofx":
        return parse_ofx(data), "ofx"
    return parse_csv(data)[0], "csv"
