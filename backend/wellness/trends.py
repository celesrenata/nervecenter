"""Deterministic trend engine for the Wellness Journal (spec §10).

**Privacy contract (normative):** this module is *statistics only*. It
computes means, least-squares slopes, sample standard deviations, and one
Pearson correlation between sleep duration and mood. It performs **no
inference, no model calls, and no diagnosis** — the output vocabulary is
whitelisted, data-describing phrases ("mood trend is upward over 30 days").
Nothing in this package imports ``backend.inference``,
``backend.omniroute``, or any transport/cloud module (enforced by the
import-graph test in ``tests/backend/test_wellness.py``).

Contract (what is computed, and the gates):

- :func:`TrendEngine.trend_for` — per-metric trend over the window:
  ``n``, ``mean``, ``first``, ``last``, ``slope_per_day`` (least squares),
  ``relative_change``, and ``direction`` ∈
  ``upward | downward | flat | insufficient``.
  Gates: ``n < 3`` → ``insufficient``; ``|relative_change| < 5%`` → ``flat``;
  ``|relative_change| < 15%`` is phrased "slightly".
- :func:`TrendEngine.sleep_consistency` — over days with a
  ``sleep_minutes`` value (self-reported, or merged from the workstream-H
  sleep records when provided): ``n``, ``avg_minutes``, ``stdev_minutes``
  (sample stdev), ``regularity`` ∈ ``stable | variable | unknown``.
  Gates: ``n < 3`` → ``unknown`` (sparse); ``stdev <= 45 min`` → ``stable``;
  else → ``variable``.
- :func:`TrendEngine.correlation` — Pearson ``r`` between
  ``sleep_minutes`` and ``mood`` over days where **both** are present,
  reporting ``r`` + ``n``. Gate: ``n < 7`` → status ``insufficient``
  ("insufficient data", never a diagnosis word). ``r`` is ``null`` with
  status ``no_variation`` when either axis is constant.

All outputs carry ``sensitivity: "personal"`` (see
:mod:`backend.persistence.models.wellness`).
"""

from __future__ import annotations

import math
from datetime import date
from typing import Any

from backend.persistence.models.wellness import WELLNESS_SENSITIVITY

#: Minimum observed points before a per-metric trend is reported.
MIN_TREND_POINTS = 3
#: Minimum paired (sleep, mood) days before a correlation is reported.
MIN_CORRELATION_PAIRS = 7
#: |relative_change| below this fraction of the mean is called "flat".
FLAT_THRESHOLD = 0.05
#: |relative_change| below this fraction is phrased "slightly".
SLIGHT_THRESHOLD = 0.15
#: Sleep-regularity gates (minutes of sample stdev).
SPARSE_SLEEP_DAYS = 3
STABLE_STDEV_MINUTES = 45.0
VARIABLE_STDEV_MINUTES = 120.0


def _pearson(pairs: list[tuple[float, float]]) -> float | None:
    """Pearson r over (x, y) pairs; ``None`` when either axis is constant."""
    n = len(pairs)
    if n < 2:
        return None
    mx = sum(x for x, _ in pairs) / n
    my = sum(y for _, y in pairs) / n
    sxx = sum((x - mx) ** 2 for x, _ in pairs)
    syy = sum((y - my) ** 2 for _, y in pairs)
    if sxx == 0 or syy == 0:
        return None
    sxy = sum((x - mx) * (y - my) for x, y in pairs)
    return sxy / math.sqrt(sxx * syy)


def _ls_slope(points: list[tuple[float, float]]) -> float:
    """Least-squares slope of y on ordinal x (per-day slope of the series)."""
    n = len(points)
    if n < 2:
        return 0.0
    mx = sum(x for x, _ in points) / n
    my = sum(y for _, y in points) / n
    sxx = sum((x - mx) ** 2 for x, _ in points)
    if sxx == 0:
        return 0.0
    sxy = sum((x - mx) * (y - my) for x, y in points)
    return sxy / sxx


def _sample_stdev(values: list[float]) -> float:
    n = len(values)
    if n < 2:
        return 0.0
    m = sum(values) / n
    return math.sqrt(sum((v - m) ** 2 for v in values) / (n - 1))


class TrendEngine:
    """Deterministic, data-describing statistics over per-day rows.

    ``rows`` is the repository's per-day aggregate list (see
    :meth:`WellnessRepository.trend_rows`): ``{day, mood, energy, stress,
    sleep_minutes, n}`` sorted by day ascending. ``sleep_records`` (optional)
    is the workstream-H sleep payload — records with ``bed_at`` / ``wake_at`` /
    ``duration_minutes`` / ``source`` — merged in for days where the user did
    not self-report ``sleep_minutes``.
    """

    # -- merging -------------------------------------------------------------

    @staticmethod
    def merge_sleep(
        rows: list[dict[str, Any]],
        sleep_records: list[dict[str, Any]] | None,
    ) -> list[dict[str, Any]]:
        """Fill ``sleep_minutes`` from H's sleep records where it is absent.

        Matching is by calendar date. Self-reported values always win; a
        same-date record only fills the gap.
        """
        if not sleep_records:
            return rows
        by_day: dict[str, int | float] = {}
        for rec in sleep_records:
            bed_at = rec.get("bed_at") or rec.get("at") or ""
            day = str(bed_at)[:10]
            if day and (rec.get("duration_minutes") is not None):
                by_day[day] = float(rec["duration_minutes"])
        out = []
        for row in rows:
            row = dict(row)
            if row.get("sleep_minutes") is None:
                day = (row.get("day") or "")[:10]
                if day in by_day:
                    row["sleep_minutes"] = by_day[day]
            out.append(row)
        return out

    # -- per-metric trends ----------------------------------------------------

    def trend_for(
        self,
        *,
        metric: str,
        rows: list[dict[str, Any]],
        days: int,
    ) -> dict[str, Any]:
        values = [r[metric] for r in rows if r.get(metric) is not None]
        n = len(values)
        base: dict[str, Any] = {
            "metric": metric,
            "window_days": days,
            "n": n,
            "mean": _round3(sum(values) / n) if n else None,
            "first": values[0] if values else None,
            "last": values[-1] if values else None,
            "slope_per_day": None,
            "relative_change": None,
            "direction": "insufficient",
            "description": f"insufficient data to describe the {metric} trend",
        }
        if n < MIN_TREND_POINTS:
            return base
        # x = ordinal day offset from the first observed day of the metric.
        offsets: list[float] = []
        cursor: date | None = None
        x: list[float] = []
        for r in rows:
            v = r.get(metric)
            if v is None:
                continue
            d = r.get("day")
            if d:
                if cursor is None:
                    cursor = date.fromisoformat(str(d)[:10])
                offsets.append((date.fromisoformat(str(d)[:10]) - cursor).days)
            else:  # pragma: no cover — rows always carry a day
                offsets.append(float(len(x)))
            x.append(offsets[-1])
        slope = _ls_slope(list(zip(x, values)))
        mean = sum(values) / n
        rel = (slope * max(x) / mean) if (x and mean) else 0.0
        if abs(rel) < FLAT_THRESHOLD:
            direction = "flat"
            description = f"{metric} is steady over {days} days"
        else:
            direction = "upward" if rel > 0 else "downward"
            qualifier = "slightly " if abs(rel) < SLIGHT_THRESHOLD else ""
            description = f"{metric} trend is {qualifier}{direction} over {days} days"
        base.update(
            {
                "slope_per_day": _round3(slope),
                "relative_change": _round3(rel),
                "direction": direction,
                "description": description,
            }
        )
        return base

    # -- sleep consistency ----------------------------------------------------

    def sleep_consistency(self, *, rows: list[dict[str, Any]], days: int) -> dict[str, Any]:
        values = [float(r["sleep_minutes"]) for r in rows if r.get("sleep_minutes") is not None]
        n = len(values)
        if n < SPARSE_SLEEP_DAYS:
            return {
                "n": n,
                "avg_minutes": _round0(sum(values) / n) if n else None,
                "stdev_minutes": None,
                "regularity": "unknown",
                "description": "insufficient data to assess sleep regularity",
            }
        stdev = _sample_stdev(values)
        if stdev <= STABLE_STDEV_MINUTES:
            regularity = "stable"
            description = f"sleep duration is steady day to day (average {_round0(sum(values) / n)} min, ±{_round0(stdev)} min over {days} days)"
        else:
            regularity = "variable"
            description = f"sleep duration varies day to day (average {_round0(sum(values) / n)} min, ±{_round0(stdev)} min over {days} days)"
        return {
            "n": n,
            "avg_minutes": _round0(sum(values) / n),
            "stdev_minutes": _round1(stdev),
            "regularity": regularity,
            "description": description,
        }

    # -- correlation (sleep ↔ mood) -------------------------------------------

    def correlation(self, *, rows: list[dict[str, Any]], days: int) -> dict[str, Any]:
        pairs = [
            (float(r["sleep_minutes"]), float(r["mood"]))
            for r in rows
            if r.get("sleep_minutes") is not None and r.get("mood") is not None
        ]
        n = len(pairs)
        if n < MIN_CORRELATION_PAIRS:
            return {
                "metric": "sleep_vs_mood",
                "n": n,
                "r": None,
                "status": "insufficient",
                "description": (
                    f"insufficient data ({n} of {MIN_CORRELATION_PAIRS}+ days with both "
                    "sleep and mood) to compare sleep and mood"
                ),
            }
        r = _pearson(pairs)
        if r is None:
            return {
                "metric": "sleep_vs_mood",
                "n": n,
                "r": None,
                "status": "no_variation",
                "description": "no day-to-day variation in the data to compare sleep and mood",
            }
        strength, direction = _describe_r(r)
        return {
            "metric": "sleep_vs_mood",
            "n": n,
            "r": _round3(r),
            "status": "ok",
            "description": f"the data show a {strength} {direction} association between sleep duration and mood over {days} days",
        }

    # -- top level -------------------------------------------------------------

    def trends(
        self,
        *,
        rows: list[dict[str, Any]],
        days: int,
        sleep_records: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """The full ``GET /api/wellness/trends`` payload (statistics only)."""
        merged = self.merge_sleep(rows, sleep_records)
        return {
            "sensitivity": WELLNESS_SENSITIVITY,
            "window_days": days,
            "n_entries": sum(int(r.get("n") or 0) for r in rows),
            "trends": {
                "mood": self.trend_for(metric="mood", rows=merged, days=days),
                "energy": self.trend_for(metric="energy", rows=merged, days=days),
                "stress": self.trend_for(metric="stress", rows=merged, days=days),
                "sleep_minutes": self.trend_for(metric="sleep_minutes", rows=merged, days=days),
            },
            "sleep": self.sleep_consistency(rows=merged, days=days),
            "correlation": self.correlation(rows=merged, days=days),
        }


def _describe_r(r: float) -> tuple[str, str]:
    """Whitelisted magnitude words for a reported |r| (no diagnosis)."""
    magnitude = abs(r)
    if magnitude >= 0.7:
        strength = "strong"
    elif magnitude >= 0.5:
        strength = "moderate"
    elif magnitude >= 0.3:
        strength = "weak"
    else:
        strength = "negligible"
    return strength, ("positive" if r > 0 else "negative")


def _round3(v: float) -> float:
    return round(v, 3)


def _round1(v: float) -> float:
    return round(v, 1)


def _round0(v: float) -> float:
    return round(v)
