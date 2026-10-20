"""Shared deterministic date parsing for detection (R103 step 2).

One parser serves the commitment / request / deadline extractors. It
understands, relative to an explicit ``now`` anchor (never the wall clock,
so identical input → identical output):

- explicit dates: ISO (``2026-11-02`` / full ISO timestamps) and common
  written forms (``November 2``, ``Nov 2nd``, ``11/2``, ``11/2/2026``);
- ``today`` / ``tonight`` (tonight → 20:00, a stated time-of-day);
- ``tomorrow`` (→ next day; no stated time → end of business day 17:00);
- ``next week`` → the next Monday at business-open (09:00);
- weekday forms (``by Tuesday``, ``by Friday``) → the *next* occurrence of
  that weekday at end of business day (17:00); never back-dated — a weekday
  named in a deadline always points forward;
- business-hour constraints: an explicit clock time ("by 5 pm",
  "at 3:30 pm", "noon", "EOD") wins over the day-level default and is
  recorded as ``time_stated``;
- "remind me …" forms are handled by the extractors, which reuse this same
  relative-date machinery ("remind me to call Sam tomorrow" → tomorrow).

All results are aware UTC datetimes; the day-level defaults are documented
here and on :class:`~backend.communications.detection.findings.DueInfo` so
the "why" strings stay explainable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from .findings import DueInfo

#: Default end-of-business hour used when a day-level due has no stated time.
DAY_END_BUSINESS_HOUR = 17

#: "Tonight" reads as this hour (an explicit time-of-day, not the EOD default).
TONIGHT_HOUR = 20

#: "Next week" anchors to the next Monday at business-open.
NEXT_WEEK_OPEN_HOUR = 9

_WEEKDAYS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}

_MONTH_DAYS = {
    "january": 1, "jan": 1,
    "february": 2, "feb": 2,
    "march": 3, "mar": 3,
    "april": 4, "apr": 4,
    "may": 5,
    "june": 6, "jun": 6,
    "july": 7, "jul": 7,
    "august": 8, "aug": 8,
    "september": 9, "sept": 9, "sep": 9,
    "october": 10, "oct": 10,
    "november": 11, "nov": 11,
    "december": 12, "dec": 12,
}

_MONTH_NAME = re.compile(
    r"\b(" + "|".join(sorted(_MONTH_DAYS, key=len, reverse=True)) + r")\s+"
    r"(\d{1,2})(?:st|nd|rd|th)?\b",
    re.IGNORECASE,
)
_SLASH_DATE = re.compile(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b")
_WEEKDAY = re.compile(
    r"\b(?:" + "|".join(sorted(_WEEKDAYS, key=len, reverse=True)) + r")\b",
    re.IGNORECASE,
)
_ISO_TS = re.compile(
    r"\b(\d{4})-(\d{2})-(\d{2})[T ](\d{1,2}):(\d{2})(?::(\d{2}))?"
)
_ISO_DATE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_CLOCK_AMPM = re.compile(
    r"\b(?:at|by|before|until|till)\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b",
    re.IGNORECASE,
)
_CLOCK_24 = re.compile(
    r"\b(?:at|by|before|until|till)\s*(\d{1,2})(?::(\d{2}))?(?!\s*(?:am|pm))\b",
    re.IGNORECASE,
)
_NOON = re.compile(r"\b(?:at|by|before|until|till)\s*(?:noon|midday|lunchtime)\b", re.IGNORECASE)
_EOD = re.compile(
    r"\b(?:eod|end of (?:the )?day|close of (?:the )?day|cutoff|cut-off)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class _StatedClock:
    """A stated time-of-day: hour/minute in 24h form + its short expr."""

    hour: int
    minute: int
    expr: str


def _utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _business_close(d: date) -> datetime:
    return _utc(datetime(d.year, d.month, d.day, hour=DAY_END_BUSINESS_HOUR))


def _next_weekday_anchor(now: datetime, weekday: int) -> date:
    """The date of the *next* occurrence of ``weekday`` after ``now``'s day.

    A weekday named in a deadline ("by Friday", said on a Tuesday) points
    forward — never back — so even ``now`` on a Friday resolves to the
    *next* Friday. Same-day deadlines are expressed with ``today`` instead,
    keeping the read conservative and unambiguous.
    """
    days_ahead = (weekday - now.weekday()) % 7
    if days_ahead == 0:
        days_ahead = 7
    return now.date() + timedelta(days=days_ahead)


def parse_due_expression(text: str, *, now: datetime) -> DueInfo | None:
    """Parse the first due expression found in ``text`` relative to ``now``.

    Returns ``None`` when no supported form is present (callers treat that
    as "no due date" rather than guessing). Given the same ``(text, now)``
    pair the output is always identical (deterministic).
    """
    now = _utc(now)
    low = text.lower()

    # 1) explicit ISO timestamp (date + stated time, most specific)
    m = _ISO_TS.search(low)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        hh, mm = int(m.group(4)), int(m.group(5))
        ss = int(m.group(6) or 0)
        try:
            dt = datetime(y, mo, d, hh, mm, ss)
        except ValueError:
            return None
        return DueInfo(
            due=_utc(dt),
            expr=m.group(0).replace("t", " ").strip(),
            time_stated=True,
            confidence=0.95,
        )

    # 2) explicit ISO date (day-level; no stated time)
    m = _ISO_DATE.search(low)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            day = date(y, mo, d)
        except ValueError:
            return None
        return DueInfo(
            due=_business_close(day), expr=m.group(0),
            time_stated=False, confidence=0.9,
        )

    # 3) written month form: "November 2" / "Nov 2nd"
    m = _MONTH_NAME.search(low)
    if m:
        month = _MONTH_DAYS.get(m.group(1).lower())
        day = int(m.group(2))
        if month is None or not 1 <= day <= 31:
            return None
        # Prefer the upcoming occurrence in this year, else next year.
        year = now.year
        try:
            d = date(year, month, day)
        except ValueError:
            d = None
        if d is None or d < now.date():
            year = now.year + 1
            try:
                d = date(year, month, day)
            except ValueError:
                return None
        return DueInfo(
            due=_business_close(d), expr=m.group(0).strip(),
            time_stated=False, confidence=0.9,
        )

    # 4) numeric slash form: "11/2" / "11/2/2026" (US M/D convention)
    m = _SLASH_DATE.search(low)
    if m:
        got = _parse_slash_date(m, now)
        if got is not None:
            return got

    # 5) relative day forms (tomorrow / tonight / today / next week / weekday)
    stated = _parse_clock(low)

    if re.search(r"\btomorrow\b", low):
        d = now.date() + timedelta(days=1)
        if stated is not None:
            return _compose(d, stated, "tomorrow " + stated.expr,
                            time_stated=True, confidence=0.95)
        return DueInfo(due=_business_close(d),
                       expr="tomorrow (by end of business day)",
                       time_stated=False, confidence=0.85)

    if re.search(r"\btonight\b", low):
        d = now.date()
        if stated is not None:
            return _compose(d, stated, "tonight " + stated.expr,
                            time_stated=True, confidence=0.95)
        return _compose(d, (TONIGHT_HOUR, 0), "tonight (by 8 pm)",
                        time_stated=True, confidence=0.8)

    if re.search(r"\btoday\b", low):
        d = now.date()
        if stated is not None:
            return _compose(d, stated, "today " + stated.expr,
                            time_stated=True, confidence=0.95)
        return _compose(d, (DAY_END_BUSINESS_HOUR, 0),
                        "today (by end of business day)",
                        time_stated=False, confidence=0.8)

    if re.search(r"\bnext\s+week\b", low):
        d = _next_weekday_anchor(now, 0)  # Monday
        if stated is not None:
            return _compose(d, stated,
                            "next week (" + stated.expr + " Monday)",
                            time_stated=True, confidence=0.9)
        return _compose(d, (NEXT_WEEK_OPEN_HOUR, 0),
                        "next week (by Monday business open)",
                        time_stated=True, confidence=0.75)

    m = _WEEKDAY.search(low)
    if m:
        weekday = _WEEKDAYS[m.group(0).lower()]
        d = _next_weekday_anchor(now, weekday)
        if stated is not None:
            return _compose(d, stated,
                            f"by {m.group(0).lower()} {stated.expr}",
                            time_stated=True, confidence=0.9)
        return _compose(d, (DAY_END_BUSINESS_HOUR, 0),
                        f"by {m.group(0).lower()} (end of business day)",
                        time_stated=False, confidence=0.8)

    # 6) a bare clock ("by 5 pm") with no date context → today, rolling
    #    forward a day when the stated time already passed (conservative).
    if stated is not None:
        d = now.date()
        cand = _compose(d, stated, stated.expr, time_stated=True, confidence=0.85)
        if cand.due <= now:
            cand = _compose(d + timedelta(days=1), stated,
                            "tomorrow " + stated.expr, time_stated=True,
                            confidence=0.85)
        return cand

    # 7) "next month" / "next year" without a time → end of that period.
    if re.search(r"\bnext\s+month\b", low):
        d = now.replace(day=1) + timedelta(days=32)
        d = date(d.year, d.month, 1)
        return DueInfo(due=_business_close(d),
                       expr="next month (end of business day)",
                       time_stated=False, confidence=0.7)
    if re.search(r"\bnext\s+year\b", low):
        d = date(now.year + 1, 1, 1)
        return DueInfo(due=_business_close(d),
                       expr="next year (end of business day)",
                       time_stated=False, confidence=0.7)

    return None


def _parse_slash_date(m: re.Match[str], now: datetime) -> DueInfo | None:
    a, b = int(m.group(1)), int(m.group(2))
    year_raw = m.group(3)
    if year_raw:
        y = int(year_raw)
        if y < 100:
            y += 2000
        month, day = a, b
    else:
        # US convention: M/D — "12/2" is December 2nd, not February 12th.
        month, day = a, b
        if month > 12 or day > 31:
            return None
        y = now.year
        try:
            d = date(y, month, day)
        except ValueError:
            return None
        if d < now.date():
            y += 1
            try:
                d = date(y, month, day)
            except ValueError:
                return None
        return DueInfo(due=_business_close(d), expr=m.group(0),
                       time_stated=False, confidence=0.85)
    if month > 12 or day > 31:
        return None
    try:
        d = date(y, month, day)
    except ValueError:
        return None
    return DueInfo(due=_business_close(d), expr=m.group(0),
                   time_stated=False, confidence=0.85)


def _parse_clock(low: str) -> _StatedClock | None:
    m = _CLOCK_AMPM.search(low)
    if m:
        h = int(m.group(1))
        if h > 12:
            return None
        minute = int(m.group(2) or 0)
        if minute > 59:
            return None
        suffix = m.group(3).lower()
        if suffix == "pm" and h != 12:
            h += 12
        elif suffix == "am" and h == 12:
            h = 0
        return _StatedClock(h, minute, f"{m.group(1)}:{minute:02d} {suffix}")
    m = _CLOCK_24.search(low)
    if m:
        h = int(m.group(1))
        if h > 23:
            return None
        minute = int(m.group(2) or 0)
        if minute > 59:
            return None
        return _StatedClock(h, minute, f"{h:02d}:{minute:02d}")
    if _NOON.search(low):
        return _StatedClock(12, 0, "noon")
    if _EOD.search(low):
        return _StatedClock(DAY_END_BUSINESS_HOUR, 0,
                           f"{DAY_END_BUSINESS_HOUR:02d}:00 (end of business day)")
    return None


def _compose(day: date, clock: _StatedClock | tuple[int, int], expr: str, *,
             time_stated: bool, confidence: float) -> DueInfo:
    if isinstance(clock, tuple):
        hh, mm = clock
    else:
        hh, mm = clock.hour, clock.minute
    return DueInfo(
        due=_utc(datetime(day.year, day.month, day.day, hh, mm, 0)),
        expr=expr,
        time_stated=time_stated,
        confidence=confidence,
    )


__all__ = [
    "DAY_END_BUSINESS_HOUR",
    "TONIGHT_HOUR",
    "NEXT_WEEK_OPEN_HOUR",
    "parse_due_expression",
]
