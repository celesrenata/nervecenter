"""Deterministic aggregation over structured rows (no model call).

Adapters hand back ``StructuredRow`` objects: a ``Record`` plus typed fields. ``compute`` applies the
intent's window and filters, then runs the operator in code and returns a ``Computation`` whose text
becomes the first ``COMPUTED:`` evidence item. Money is integer cents.

Recognised ``fields`` keys: ``amount_cents`` (int), ``currency`` (str), ``status`` (str),
``due_at`` (datetime or ISO string), ``ts`` (datetime or ISO string; falls back to ``record.ts``),
``payee`` (str), ``priority`` (number or low/medium/high/urgent).
"""
from __future__ import annotations

import os
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .intent import Intent, Operator
from .types import Record

DUE_SOON_DAYS = 7
_UNPAID = frozenset({"unpaid", "overdue", "due", "open", "pending", "stale", "outstanding"})
_PAID = frozenset({"paid", "confirmed_paid", "settled", "complete", "completed"})
_PRIORITY = {"low": 1, "medium": 2, "normal": 2, "high": 3, "urgent": 4, "critical": 4}
_FAR = float("inf")


@dataclass(frozen=True)
class StructuredRow:
    record: Record
    fields: dict[str, Any] = field(default_factory=dict, hash=False)


@dataclass(frozen=True)
class Computation:
    """A result computed in code. ``value`` is cents for money, an int for counts, an ISO string for
    dates, a list of ``(record_id, value)`` for top N, and ``{"count", "total_cents"}`` for
    overdue and due soon."""

    kind: str
    value: Any
    text: str
    scope: dict[str, Any] = field(default_factory=dict, hash=False)
    record_ids: tuple[str, ...] = ()
    truncated: bool = False


def user_zone() -> ZoneInfo | None:
    """The user's zone (``NERVE_USER_TIMEZONE``); unset, empty or invalid: None (UTC behaviour)."""
    name = os.environ.get("NERVE_USER_TIMEZONE", "").strip()
    if not name:
        return None
    try:
        return ZoneInfo(name)
    except Exception:  # noqa: BLE001 - a bad zone name must not break Ask
        return None


def _local(d: datetime) -> datetime:
    """An aware datetime in the user's zone for display; naive or no zone: unchanged."""
    zone = user_zone()
    if zone is None or d.tzinfo is None:
        return d
    return d.astimezone(zone)


def _day(d: datetime) -> str:
    return _local(d).date().isoformat()


def _at(row: StructuredRow, d: datetime) -> str:
    """`` at HH:MM`` (user zone) for a timed calendar event, so the text matches the record text."""
    if row.record.domain != "calendar" or d.tzinfo is None or user_zone() is None:
        return ""
    local = _local(d)
    return "" if (local.hour, local.minute) == (0, 0) else f" at {local:%H:%M}"


def _dt(value: Any, now: datetime | None) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    if now is not None:
        if value.tzinfo is None and now.tzinfo is not None:
            return value.replace(tzinfo=now.tzinfo)
        if value.tzinfo is not None and now.tzinfo is None:
            return value.replace(tzinfo=None)
    return value


def _due(row: StructuredRow, now: datetime) -> datetime | None:
    return _dt(row.fields.get("due_at"), now)


def _ts(row: StructuredRow, now: datetime) -> datetime | None:
    return _dt(row.fields.get("ts") or row.record.ts, now)


def _primary_date(row: StructuredRow, now: datetime) -> datetime | None:
    return _due(row, now) or _ts(row, now)


def _cents(row: StructuredRow) -> int | None:
    v = row.fields.get("amount_cents")
    if v is None or isinstance(v, bool):
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _priority(row: StructuredRow) -> float | None:
    v = row.fields.get("priority")
    if isinstance(v, str):
        return _PRIORITY.get(v.strip().lower())
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    return None


def _status(row: StructuredRow) -> str:
    return str(row.fields.get("status") or "").strip().lower()


def _is_unpaid(row: StructuredRow) -> bool:
    return _status(row) in _UNPAID


def _money(cents: int, currency: str | None) -> str:
    sign = "-" if cents < 0 else ""
    whole, frac = divmod(abs(cents), 100)
    cur = (currency or "USD").upper()
    prefix = "$" if cur == "USD" else ""
    suffix = "" if cur == "USD" else f" {cur}"
    return f"{sign}{prefix}{whole:,}.{frac:02d}{suffix}"


def _div_round(total: int, n: int) -> int:
    """Integer division rounding half away from zero."""
    q, r = divmod(abs(total), n)
    if 2 * r >= n:
        q += 1
    return q if total >= 0 else -q


def _iso(d: datetime | None) -> str | None:
    return _local(d).isoformat() if d else None


def _label(row: StructuredRow) -> str:
    return row.record.title or row.record.id


def _apply_filters(
    rows: list[StructuredRow], intent: Intent, now: datetime, op: Operator,
    miss: dict[str, Any] | None = None,
) -> list[StructuredRow]:
    out = rows
    if op.kind in ("count", "list"):
        # a health "summary" row restates its sample rows; counting it would double count
        out = [r for r in out if _status(r) != "summary"]
    status = intent.filters.get("status")
    if op.kind in ("overdue", "due_soon"):
        status = "unpaid"
    if status == "unpaid":
        out = [r for r in out if _is_unpaid(r)]
    elif status == "paid":
        out = [r for r in out if _status(r) in _PAID]
    elif status:
        out = [r for r in out if _status(r) == status]
    payee = intent.filters.get("payee")
    if payee:
        out = [r for r in out if payee in str(r.fields.get("payee") or r.record.title).lower()]
    name = intent.filters.get("name")
    if name:
        out = [
            r for r in out
            if name in " ".join((str(r.fields.get("payee") or ""), r.record.title, r.record.snippet)).lower()
        ]
    if intent.keywords:
        # "next dentist appointment": keep the rows naming a keyword, when any does
        from .intent import _stem

        def _hit(r: StructuredRow) -> bool:
            att = r.fields.get("attendees") or []
            if isinstance(att, str):
                att = [att]
            hay = f"{r.record.title} {r.record.snippet} {r.fields.get('payee') or ''} {' '.join(map(str, att))}".lower()
            tokens = [t for t in re.split(r"[^a-z0-9]+", hay) if t]
            words = {_stem(w) for w in hay.replace(",", " ").split()} | set(tokens)
            for k in intent.keywords:
                if k in words or (len(k) > 4 and k in hay):
                    return True
                if len(k) >= 3 and any(t.startswith(k) for t in tokens):
                    return True
            return False
        matched = [r for r in out if _hit(r)]
        if matched:
            out = matched
        elif miss is not None and op.kind in ("next", "earliest", "latest") and out:
            miss["filter"] = " ".join(intent.keywords)
            miss["rows"] = out
            out = []
    if intent.window and op.kind not in ("overdue", "due_soon", "next"):
        start, end = (_dt(intent.window[0], now), _dt(intent.window[1], now))
        out = [r for r in out if (d := _primary_date(r, now)) is not None and start <= d < end]
    return out


def _sort_key(row: StructuredRow, value: float, now: datetime, descending: bool) -> tuple:
    due = _due(row, now)
    return (-value if descending else value, due.timestamp() if due else _FAR, row.record.id)


def _value_of(row: StructuredRow, op: Operator, now: datetime) -> float | None:
    if op.field == "priority":
        return _priority(row)
    if op.field == "due":
        d = _due(row, now)
        return d.timestamp() if d else None
    if op.field == "ts":
        d = _ts(row, now)
        return d.timestamp() if d else None
    c = _cents(row)
    return float(c) if c is not None else None


def _no_match(filt: str, cands: list[StructuredRow], op: Operator, now: datetime, domain: str | None) -> Computation:
    """No row mentions the filter: say so, and list the nearest rows as labeled non-matches."""
    dated = [(r, d) for r in cands if (d := _ts(r, now) or _due(r, now)) is not None]
    if op.kind == "next":
        dated = [(r, d) for r, d in dated if d >= now]
    dated.sort(key=lambda rd: (-rd[1].timestamp() if op.kind == "latest" else rd[1].timestamp(), rd[0].record.id))
    near = dated[:3]
    text = f"COMPUTED: No upcoming event mentions {filt}."
    if near:
        text += " Not matching " + filt + " (nearest events, do not present as the answer): " + "; ".join(
            f"{_label(r)} on {_day(d)}" for r, d in near
        ) + "."
    return Computation(
        kind=op.kind, value=None, text=text,
        scope={"filter": filt, "matched": 0, "total_rows": len(cands)},
        record_ids=tuple(r.record.id for r, _ in near),
    )


def compute(rows: list[StructuredRow], intent: Intent, now: datetime) -> Computation | None:
    """Run the intent's operator over ``rows``. Returns ``None`` for empty input, no operator, or no
    row that the operator can use."""
    op = intent.operator
    if not rows or op is None:
        return None
    total_rows = len(rows)
    domain = rows[0].record.domain
    miss: dict[str, Any] = {}
    rows = _apply_filters(list(rows), intent, now, op, miss)
    if miss:
        return _no_match(miss["filter"], miss["rows"], op, now, domain)
    scope: dict[str, Any] = {
        "window": [_iso(intent.window[0]), _iso(intent.window[1])] if intent.window else None,
        "status": ("unpaid" if op.kind in ("overdue", "due_soon") else intent.filters.get("status")),
        "payee": intent.filters.get("payee"),
        "name": intent.filters.get("name"),
        "rows": len(rows),
        "total_rows": total_rows,
    }
    bits = [f"{len(rows)} of {total_rows} {domain or 'records'}"]
    for key in ("status", "payee", "name"):
        if scope[key]:
            bits.append(f"{key} {scope[key]}")
    if scope["window"]:
        bits.append(f"window {scope['window'][0][:10]} to {scope['window'][1][:10]}")
    scope_text = "Based on " + ", ".join(bits)

    def done(value: Any, statement: str, used: list[StructuredRow]) -> Computation:
        return Computation(
            kind=op.kind, value=value, text=f"COMPUTED: {statement}. {scope_text}.",
            scope=scope, record_ids=tuple(r.record.id for r in used),
        )

    if op.kind in ("count", "list"):
        if not rows:
            return done(0, f"There are 0 matching {domain or 'records'}", [])
        return done(len(rows), f"There are {len(rows)} matching {domain or 'records'}", rows)

    if op.kind in ("overdue", "due_soon"):
        if op.kind == "overdue":
            sel = [r for r in rows if (d := _due(r, now)) is not None and d < now]
            what = "overdue"
        else:
            start, end = (
                (_dt(intent.window[0], now), _dt(intent.window[1], now)) if intent.window
                else (now, now + timedelta(days=DUE_SOON_DAYS))
            )
            sel = [r for r in rows if (d := _due(r, now)) is not None and start <= d < end]
            what = "due soon"
        sel.sort(key=lambda r: (_due(r, now).timestamp(), r.record.id))
        priced = [(c, r.fields.get("currency")) for r in sel if (c := _cents(r)) is not None]
        total = sum(c for c, _ in priced)
        cur = Counter(c for _, c in priced).most_common(1)[0][0] if priced else None
        tail = f", totalling {_money(total, cur)}" if priced else ""
        titles = "; ".join(_label(r) for r in sel[:5])
        stmt = f"{len(sel)} {what}{tail}" + (f": {titles}" if sel else "")
        return done({"count": len(sel), "total_cents": total}, stmt, sel)

    if op.kind in ("sum", "avg"):
        priced = [(r, c) for r in rows if (c := _cents(r)) is not None]
        if not priced:
            return None
        by_cur: dict[str, list[int]] = {}
        for r, c in priced:
            by_cur.setdefault(str(r.fields.get("currency") or "USD").upper(), []).append(c)
        results = {
            cur: (sum(v) if op.kind == "sum" else _div_round(sum(v), len(v))) for cur, v in by_cur.items()
        }
        word = "total" if op.kind == "sum" else "average"
        text = "; ".join(f"{word} {_money(v, cur)} over {len(by_cur[cur])}" for cur, v in sorted(results.items()))
        value = next(iter(results.values())) if len(results) == 1 else results
        return done(value, text, [r for r, _ in priced])

    if op.kind in ("max", "min", "top_n"):
        scored = [(r, v) for r in rows if (v := _value_of(r, op, now)) is not None]
        if not scored:
            return None
        desc = op.kind != "min"
        scored.sort(key=lambda rv: _sort_key(rv[0], rv[1], now, desc))

        def show(r: StructuredRow) -> str:
            if op.field == "amount":
                return _money(_cents(r) or 0, r.fields.get("currency"))
            if op.field == "priority":
                return f"priority {r.fields.get('priority')}"
            d = _due(r, now) if op.field == "due" else _ts(r, now)
            return (_day(d) if d else "")
        if op.kind == "top_n":
            top = scored[: max(1, op.n or 3)]
            text = f"Top {len(top)}: " + "; ".join(f"{_label(r)} ({show(r)})" for r, _ in top)
            return done([(r.record.id, v) for r, v in top], text, [r for r, _ in top])
        if op.offset >= len(scored):
            return None
        best, _ = scored[op.offset]
        word = "highest" if op.kind == "max" else "lowest"
        if op.offset:
            word = f"number {op.offset + 1} by {'highest' if op.kind == 'max' else 'lowest'}"
        raw = best.fields.get("amount_cents") if op.field == "amount" else None
        value = int(raw) if raw is not None else show(best)
        text = f"The {word} is {_label(best)} ({show(best)})"
        due = _due(best, now)
        if due and op.field != "due":
            text += f", due {_day(due)}"
        return done(value, text, [best])

    # earliest, latest, next
    dated = [(r, d) for r in rows if (d := _ts(r, now) or _due(r, now)) is not None]
    if op.kind == "next":
        dated = [(r, d) for r, d in dated if d >= now]
    if not dated:
        return None
    pick_latest = op.kind == "latest"
    dated.sort(key=lambda rd: (-rd[1].timestamp() if pick_latest else rd[1].timestamp(), rd[0].record.id))
    if op.offset >= len(dated):
        return None
    best, when = dated[op.offset]
    word = {"earliest": "earliest", "latest": "latest", "next": "next"}[op.kind]
    if op.offset:
        word = f"number {op.offset + 1} by {word}"
    return done(_iso(when), f"The {word} is {_label(best)} on {_day(when)}{_at(best, when)}", [best])
