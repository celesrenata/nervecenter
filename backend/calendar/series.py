"""Recurring-series helpers for calendar deletion (pure, no I/O).

Ending a series is one operation: rewrite each ``RRULE`` so it stops at the
cutoff. Occurrences before the cutoff stay. Deleting one occurrence at a time
is only a bounded fallback (see ``MAX_INSTANCES`` and ``HORIZON_DAYS``): an
open-ended rule has infinitely many occurrences and must never be walked.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

#: Hard cap on per-occurrence deletes for one series (about a year of weekly meetings).
MAX_INSTANCES = 52
#: Hard cap on how far ahead occurrences are enumerated or deleted one by one.
HORIZON_DAYS = 400


def rrule_parts(line: str) -> dict[str, str]:
    body = line.split(":", 1)[1] if ":" in line else line
    parts: dict[str, str] = {}
    for item in body.split(";"):
        if "=" in item:
            key, value = item.split("=", 1)
            parts[key.strip().upper()] = value.strip()
    return parts


def _is_rrule(line: str) -> bool:
    return line.strip().upper().startswith("RRULE")


def rrules(recurrence: list[str] | None) -> list[str]:
    return [line for line in (recurrence or []) if _is_rrule(line)]


def is_open_ended(recurrence: list[str] | None) -> bool:
    """True when some RRULE has neither COUNT nor UNTIL (infinitely many occurrences)."""
    return any(
        "COUNT" not in (p := rrule_parts(line)) and "UNTIL" not in p for line in rrules(recurrence)
    )


def repeats_label(recurrence: list[str] | None) -> str:
    """Short human wording: "repeats weekly", "repeats every 2 weeks", ..."""
    rules = rrules(recurrence)
    if not rules:
        return "repeats"
    parts = rrule_parts(rules[0])
    unit = {"DAILY": "day", "WEEKLY": "week", "MONTHLY": "month", "YEARLY": "year"}.get(
        parts.get("FREQ", ""), ""
    )
    if not unit:
        return "repeats"
    adverb = {"day": "daily", "week": "weekly", "month": "monthly", "year": "yearly"}[unit]
    interval = parts.get("INTERVAL", "1")
    return f"repeats {adverb}" if interval in ("", "1") else f"repeats every {interval} {unit}s"


def end_recurrence_at(recurrence: list[str], cutoff: datetime, all_day: bool) -> list[str]:
    """Rewrite each RRULE to end before ``cutoff``; leave EXDATE/RDATE untouched.

    COUNT is removed. An existing UNTIL that is already earlier is kept. Timed
    events get a UTC ``UNTIL`` (``YYYYMMDDTHHMMSSZ``); all-day events a date.
    """
    cut = cutoff if cutoff.tzinfo else cutoff.replace(tzinfo=UTC)
    if all_day:
        until = (cut.date() - timedelta(days=1)).strftime("%Y%m%d")
    else:
        until = (cut.astimezone(UTC) - timedelta(seconds=1)).strftime("%Y%m%dT%H%M%SZ")
    out: list[str] = []
    for line in recurrence:
        if not _is_rrule(line):
            out.append(line)
            continue
        prefix, _, body = line.partition(":")
        items = [i for i in body.split(";") if i]
        existing = next((i.split("=", 1)[1] for i in items if i.upper().startswith("UNTIL=")), None)
        kept = [i for i in items if i.split("=", 1)[0].upper() not in ("COUNT", "UNTIL")]
        if existing and existing[:8] < until[:8]:
            kept.append(f"UNTIL={existing}")
        else:
            kept.append(f"UNTIL={until}")
        out.append(f"{prefix}:{';'.join(kept)}")
    return out


def series_starts_in_future(master_start: datetime | None, cutoff: datetime) -> bool:
    """A series whose first occurrence is after the cutoff has no past to keep."""
    if master_start is None:
        return False
    start = master_start if master_start.tzinfo else master_start.replace(tzinfo=UTC)
    cut = cutoff if cutoff.tzinfo else cutoff.replace(tzinfo=UTC)
    return start > cut
