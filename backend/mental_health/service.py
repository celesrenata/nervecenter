"""Daily form storage, history, trends and factor correlations (NS-ADR-101).

Local only: nothing here calls a model, and the package imports no inference
code (a test enforces that).
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from backend.mental_health import correlations as corr
from backend.mental_health.crypto import open_, seal
from backend.mental_health.instruments import (
    INSTRUMENTS,
    MAX_COMMENT_CHARS,
    score,
    validate_answers,
)
from backend.persistence.connection import make_session_factory
from backend.persistence.models.mental_health import MhFormRow

FACTORS = ("sleep_minutes", "steps", "weight_kg", "resting_hr", "spend_dollars", "event_count")
FACTOR_LABELS = {
    "sleep_minutes": "Sleep duration",
    "steps": "Steps",
    "weight_kg": "Weight",
    "resting_hr": "Resting heart rate",
    "spend_dollars": "Spending (paid bills)",
    "event_count": "Calendar events",
}
_HEALTH_KIND = {
    "sleep_minutes": "sleep",
    "steps": "step_count",
    "weight_kg": "body_mass",
    "resting_hr": "resting_heart_rate",
}


def _ctx(day: date, instrument: str, field: str) -> str:
    return f"{day.isoformat()}:{instrument}:{field}"


async def submit_form(
    engine: AsyncEngine,
    keyring: Any,
    *,
    day: date,
    instrument: str,
    answers: list[int],
    comment: str | None,
) -> dict[str, Any]:
    inst = INSTRUMENTS[instrument]
    answers = validate_answers(inst, answers)
    comment = (comment or "").strip() or None
    if comment is not None and len(comment) > MAX_COMMENT_CHARS:
        raise ValueError("comment is too long")
    total = score(answers)
    answers_enc = seal(keyring, json.dumps(answers), _ctx(day, instrument, "answers"))
    comment_enc = seal(keyring, comment, _ctx(day, instrument, "comment")) if comment else None
    async with make_session_factory(engine)() as session:
        row = (
            await session.execute(
                select(MhFormRow).where(
                    MhFormRow.form_date == day, MhFormRow.instrument == instrument
                )
            )
        ).scalar_one_or_none()
        created = row is None
        if row is None:
            row = MhFormRow(form_date=day, instrument=instrument, total_score=total,
                            answers_enc=answers_enc, comment_enc=comment_enc)
            session.add(row)
        else:
            row.total_score = total
            row.answers_enc = answers_enc
            row.comment_enc = comment_enc
            row.updated_at = datetime.now(UTC)
        await session.commit()
    return {"date": day.isoformat(), "instrument": instrument, "total": total,
            "band": inst.band(total), "max": inst.max_score, "created": created}


async def history(engine: AsyncEngine, keyring: Any, *, days: int) -> list[dict[str, Any]]:
    since = datetime.now(UTC).date() - timedelta(days=days)
    async with make_session_factory(engine)() as session:
        rows = (
            await session.execute(
                select(MhFormRow).where(MhFormRow.form_date >= since)
                .order_by(MhFormRow.form_date.desc(), MhFormRow.instrument)
            )
        ).scalars().all()
    out = []
    for r in rows:
        inst = INSTRUMENTS[r.instrument]
        d = r.form_date
        out.append({
            "date": d.isoformat(),
            "instrument": r.instrument,
            "total": r.total_score,
            "max": inst.max_score,
            "band": inst.band(r.total_score),
            "answers": json.loads(open_(keyring, r.answers_enc, _ctx(d, r.instrument, "answers"))),
            "comment": open_(keyring, r.comment_enc, _ctx(d, r.instrument, "comment"))
            if r.comment_enc else None,
        })
    return out


async def score_series(engine: AsyncEngine, *, days: int) -> dict[str, corr.Series]:
    """Daily totals per instrument. Reads only dates and scores (no decryption)."""
    since = datetime.now(UTC).date() - timedelta(days=days)
    async with make_session_factory(engine)() as session:
        rows = (await session.execute(
            select(MhFormRow.form_date, MhFormRow.instrument, MhFormRow.total_score)
            .where(MhFormRow.form_date >= since)
        )).all()
    series: dict[str, corr.Series] = {k: {} for k in INSTRUMENTS}
    for d, inst, total in rows:
        series[inst][d] = float(total)
    return series


async def factor_series(engine: AsyncEngine, *, days: int) -> dict[str, corr.Series]:
    """Daily series for each factor, from Apple Health, paid bills and the calendar."""
    from backend.health.service import summarize
    from backend.persistence.models.bills import BillRow
    from backend.persistence.models.personal import CalendarEventRow

    out: dict[str, corr.Series] = {f: {} for f in FACTORS}
    summary = await summarize(engine, days=days + MAX_LAG_PAD)
    for factor, kind in _HEALTH_KIND.items():
        for point in summary["kinds"].get(kind, {}).get("daily", []):
            out[factor][date.fromisoformat(point["date"])] = float(point["value"])
    since = datetime.now(UTC) - timedelta(days=days + MAX_LAG_PAD)
    async with make_session_factory(engine)() as session:
        bills = (await session.execute(
            select(BillRow.amount_cents, BillRow.confirmed_at)
            .where(BillRow.status == "confirmed_paid", BillRow.confirmed_at >= since)
        )).all()
        events = (await session.execute(
            select(CalendarEventRow.start).where(
                CalendarEventRow.start >= since, CalendarEventRow.deleted.is_(False)
            )
        )).all()
    spend: dict[date, float] = defaultdict(float)
    for cents, at in bills:
        try:
            spend[at.astimezone(UTC).date()] += int(cents) / 100
        except (TypeError, ValueError):
            continue
    out["spend_dollars"] = dict(spend)
    counts: dict[date, float] = defaultdict(float)
    for (start,) in events:
        if start is not None:
            counts[(start if start.tzinfo else start.replace(tzinfo=UTC)).astimezone(UTC).date()] += 1  # noqa: E501
    out["event_count"] = dict(counts)
    return out


MAX_LAG_PAD = corr.MAX_LAG


async def trends(engine: AsyncEngine, *, days: int) -> dict[str, Any]:
    series = await score_series(engine, days=days)
    result: dict[str, Any] = {"days": days, "instruments": {}}
    for key, s in series.items():
        inst = INSTRUMENTS[key]
        latest = max(s) if s else None
        result["instruments"][key] = {
            "title": inst.title,
            "max": inst.max_score,
            "count": len(s),
            "latest": None if latest is None else {
                "date": latest.isoformat(), "total": int(s[latest]), "band": inst.band(int(s[latest]))  # noqa: E501
            },
            "daily": [{"date": d.isoformat(), "value": v} for d, v in sorted(s.items())],
            "rolling_7": corr.rolling_mean(s, 7),
            "rolling_30": corr.rolling_mean(s, 30),
            "change_point": corr.change_point(s),
            "streaks": corr.streaks(s, threshold=10),
        }
    return result


async def correlations(engine: AsyncEngine, *, days: int) -> dict[str, Any]:
    scores = await score_series(engine, days=days)
    factors = await factor_series(engine, days=days)
    rows = []
    for key, target in scores.items():
        for factor, fs in factors.items():
            res = corr.lagged_correlations(target, fs)
            rows.append({"instrument": key, "factor": factor, "label": FACTOR_LABELS[factor], **res})  # noqa: E501
    rows.sort(key=lambda r: (r["state"] != "ok", -abs(r["r"] or 0)))
    return {
        "days": days,
        "min_samples": corr.MIN_SAMPLES,
        "note": "Associations between daily series. They do not show that one thing causes another.",  # noqa: E501
        "rows": rows,
    }

