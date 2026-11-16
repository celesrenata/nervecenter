"""Read side for Apple Health samples: raw list and per-kind summary (UTC days)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from backend.health.ingest import ALLOWED_KINDS
from backend.persistence.connection import make_session_factory
from backend.persistence.models.health import HealthSampleRow

#: kinds whose daily value is a total; the rest use the daily mean.
_SUM_KINDS = frozenset({"sleep", "step_count", "active_energy"})


def _iso(dt: datetime) -> str:
    return (dt if dt.tzinfo else dt.replace(tzinfo=UTC)).astimezone(UTC).isoformat()


async def list_samples(
    engine: AsyncEngine, *, kind: str | None, days: int, limit: int
) -> list[dict[str, Any]]:
    since = datetime.now(UTC) - timedelta(days=days)
    stmt = select(HealthSampleRow).where(HealthSampleRow.start_at >= since)
    if kind:
        stmt = stmt.where(HealthSampleRow.kind == kind)
    stmt = stmt.order_by(HealthSampleRow.start_at.desc()).limit(limit)
    async with make_session_factory(engine)() as session:
        rows = (await session.execute(stmt)).scalars().all()
    return [
        {
            "kind": r.kind,
            "start_at": _iso(r.start_at),
            "end_at": _iso(r.end_at),
            "value": r.value,
            "unit": r.unit,
            "source": r.source,
        }
        for r in rows
    ]


async def summarize(engine: AsyncEngine, *, days: int) -> dict[str, Any]:
    since = datetime.now(UTC) - timedelta(days=days)
    stmt = (
        select(HealthSampleRow)
        .where(HealthSampleRow.start_at >= since)
        .order_by(HealthSampleRow.start_at.asc())
    )
    async with make_session_factory(engine)() as session:
        rows = (await session.execute(stmt)).scalars().all()
    kinds: dict[str, Any] = {}
    last_sync: datetime | None = None
    for kind, (unit, _lo, _hi) in ALLOWED_KINDS.items():
        mine = [r for r in rows if r.kind == kind]
        if not mine:
            kinds[kind] = {"unit": unit, "count": 0, "latest": None, "mean": None,
                           "min": None, "max": None, "daily": []}
            continue
        values = [r.value for r in mine]
        per_day: dict[str, list[float]] = {}
        for r in mine:
            day = (r.start_at if r.start_at.tzinfo else r.start_at.replace(tzinfo=UTC))
            per_day.setdefault(day.astimezone(UTC).date().isoformat(), []).append(r.value)
            created = r.created_at
            if created is not None:
                created = created if created.tzinfo else created.replace(tzinfo=UTC)
                last_sync = created if last_sync is None else max(last_sync, created)
        agg = sum if kind in _SUM_KINDS else (lambda v: sum(v) / len(v))
        kinds[kind] = {
            "unit": unit,
            "count": len(mine),
            "latest": {"at": _iso(mine[-1].start_at), "value": mine[-1].value},
            "mean": sum(values) / len(values),
            "min": min(values),
            "max": max(values),
            "daily": [{"date": d, "value": round(agg(v), 2)} for d, v in sorted(per_day.items())],
        }
    return {
        "days": days,
        "last_sync_at": _iso(last_sync) if last_sync else None,
        "total_samples": len(rows),
        "kinds": kinds,
    }
