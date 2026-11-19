"""Health samples for Ask: sleep, mood, and energy by day, with average and range over a window.

Reads two numeric sources through the services the screens use: Apple Health samples
(``backend.health.service``, sleep) and the wellness check-in numbers (mood, energy, sleep
minutes). Journal text, stress notes, observations, and mental-health form answers are never
read; ``mh_forms`` and ``mental_health`` stay in ``EXCLUDED_STORES``. The ``wellness`` adapter
keeps returning whole check-ins; this adapter returns one record per metric per day plus one
summary record per metric (average, lowest, highest, count) over the requested window.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from backend.context.retrieval.aggregate import StructuredRow
from backend.context.retrieval.intent import Intent
from backend.context.retrieval.sources._util import aware, make_all
from backend.context.retrieval.types import Record

_DEFAULT_DAYS = 120
_SAMPLE_CAP = 5000
_UNITS = {"sleep": "min", "mood": "/10", "energy": "/10"}
_WANTED = ("sleep", "mood", "energy")


def _span(intent: Intent) -> tuple[datetime, datetime]:
    now = intent.now or datetime.now(UTC)
    if intent.window:
        return aware(intent.window[0]) or now, aware(intent.window[1]) or now
    return now - timedelta(days=_DEFAULT_DAYS), now + timedelta(days=1)


def _wanted_kinds(intent: Intent) -> tuple[str, ...]:
    """Metrics named in the question, or all of them when none is named."""
    terms = {t.lower() for t in (*intent.keywords, *intent.phrases)}
    picked = tuple(k for k in _WANTED if k in terms or (k == "sleep" and "slept" in terms))
    return picked or _WANTED


async def _load(state: Any, intent: Intent) -> list[dict[str, Any]] | None:
    """Per-day samples ``{kind, day, value, unit}``, or None when no health service is wired."""
    engine = getattr(state, "db_engine", None)
    registry = getattr(state, "repository_registry", None)
    factory = getattr(registry, "session_factory", None)
    if engine is None and factory is None:
        return None
    start, end = _span(intent)
    now = intent.now or datetime.now(UTC)
    days = max(1, (now - start).days + 2)
    by_key: dict[tuple[str, str], dict[str, Any]] = {}
    if factory is not None:
        from backend.persistence.repositories.wellness_repo import WellnessRepository
        from backend.wellness.service import WellnessService

        svc = WellnessService(WellnessRepository(factory))
        for e in await svc.list_entries(from_dt=start, to_dt=end):
            at = aware(e["at"])
            if at is None:
                continue
            for kind, raw in (("mood", e["mood"]), ("energy", e["energy"]),
                              ("sleep", e["sleep_minutes"])):
                if raw is not None:
                    by_key[(kind, at.date().isoformat())] = {
                        "kind": kind, "day": at.date().isoformat(), "value": float(raw),
                        "unit": _UNITS[kind]}
    if engine is not None:
        from backend.health.service import list_samples

        nights: dict[str, float] = {}
        for s in await list_samples(engine, kind="sleep", days=days, limit=_SAMPLE_CAP):
            when = aware(s["start_at"])
            if when is not None and start <= when < end:
                day = when.date().isoformat()
                nights[day] = nights.get(day, 0.0) + float(s["value"])
        for day, total in nights.items():  # device samples win over a manual check-in
            by_key[("sleep", day)] = {"kind": "sleep", "day": day, "value": total, "unit": "min"}
    return sorted(by_key.values(), key=lambda r: (r["day"], r["kind"]), reverse=True)


def _fmt(kind: str, value: float) -> str:
    if kind == "sleep":
        return f"{value / 60:.1f} h ({value:.0f} min)"
    return f"{value:.1f}"


def _specs(samples: list[dict[str, Any]], intent: Intent, cap: int,
           ) -> tuple[list[tuple[Any, ...]], list[dict[str, Any]]]:
    kinds = _wanted_kinds(intent)
    start, end = _span(intent)
    specs: list[tuple[Any, ...]] = []
    fields: list[dict[str, Any]] = []
    chosen = [s for s in samples if s["kind"] in kinds]
    for kind in kinds:
        vals = [s["value"] for s in chosen if s["kind"] == kind]
        if not vals:
            continue
        avg = sum(vals) / len(vals)
        when = end - timedelta(seconds=1) if intent.window else datetime.fromisoformat(
            max(s["day"] for s in chosen if s["kind"] == kind)).replace(tzinfo=UTC)
        label = (f"{start.date().isoformat()} to {end.date().isoformat()}"
                 if intent.window else "recent days")
        specs.append(("health", f"summary:{kind}:{start.date()}:{end.date()}",
                      f"Health summary: {kind}",
                      f"average {_fmt(kind, avg)}, lowest {_fmt(kind, min(vals))}, "
                      f"highest {_fmt(kind, max(vals))}, over {len(vals)} days ({label})", when))
        fields.append({"status": "summary", "ts": when, "payee": kind, "kind": kind,
                       "average": avg, "min": min(vals), "max": max(vals), "count": len(vals),
                       "unit": _UNITS[kind]})
    for s in chosen:
        when = datetime.fromisoformat(s["day"]).replace(tzinfo=UTC)
        specs.append(("health", f"{s['kind']}:{s['day']}", f"Health: {s['kind']} on {s['day']}",
                      f"{s['kind']} {_fmt(s['kind'], s['value'])} on {s['day']}", when))
        fields.append({"status": "sample", "ts": when, "payee": s["kind"], "kind": s["kind"],
                       "value": s["value"], "unit": s["unit"]})
    return specs[:cap], fields[:cap]


async def health(state: Any, intent: Intent, limit: int) -> list[Record] | None:
    samples = await _load(state, intent)
    if samples is None:
        return None
    specs, _ = _specs(samples, intent, limit)
    return await make_all(specs)


async def health_structured(state: Any, intent: Intent, cap: int) -> list[StructuredRow] | None:
    """Window summaries (average, range) and every daily sample up to ``cap`` with typed values."""
    samples = await _load(state, intent)
    if samples is None:
        return None
    specs, fields = _specs(samples, intent, cap)
    records = await make_all(specs)
    return [StructuredRow(r, f) for r, f in zip(records, fields, strict=True)]


health.structured = health_structured  # type: ignore[attr-defined]  # found by retrieve()
