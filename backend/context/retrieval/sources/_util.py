"""Helpers shared by the source adapters."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from backend.context.retrieval.policy import (
    TITLE_CHARS,
    link_for,
    record_id,
    scrub,
    when_iso,
)
from backend.context.retrieval.types import Record


def aware(value: Any) -> datetime | None:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def make(
    domain: str,
    source_ref: str,
    title: object,
    snippet: object = "",
    when: datetime | str | None = None,
) -> Record:
    ts = aware(when)
    return Record(
        id=record_id(domain, source_ref),
        domain=domain,
        title=scrub(title, TITLE_CHARS) or "(untitled)",
        snippet=scrub(snippet),
        when=when_iso(ts or when),
        link=link_for(domain),
        ts=ts,
    )


def newest(items: list[Any], key: Any, limit: int) -> list[Any]:
    floor = datetime.min.replace(tzinfo=UTC)
    return sorted(items, key=lambda i: aware(key(i)) or floor, reverse=True)[:limit]
async def make_all(specs: list[tuple[Any, ...]]) -> list[Record]:
    """Build records from ``make`` argument tuples in a worker thread.

    Scrubbing runs regexes over every snippet. That is CPU-bound, so it must
    not run on the event loop: it would stall every other adapter's database
    await past its timeout. Callers snapshot the source rows on the loop and
    pass plain values here, so the thread never touches a live service.
    """
    return await asyncio.to_thread(lambda: [make(*spec) for spec in specs])
