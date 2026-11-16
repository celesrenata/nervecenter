"""Pure-stdlib series statistics: lagged correlations, trendlines, streaks.

A correlation is an association between two daily series, nothing more. Labels
never claim one thing causes another. A pair with fewer than ``MIN_SAMPLES``
overlapping days is reported as ``too_few_samples`` instead of a number.
"""
from __future__ import annotations

import math
from datetime import date, timedelta
from statistics import mean

MIN_SAMPLES = 14
MAX_LAG = 7

Series = dict[date, float]


def pearson(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    if n < 3 or n != len(ys):
        return None
    mx, my = mean(xs), mean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx == 0 or syy == 0:
        return None
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True))
    return max(-1.0, min(1.0, sxy / math.sqrt(sxx * syy)))


def _ranks(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def spearman(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) != len(ys):
        return None
    return pearson(_ranks(xs), _ranks(ys))


def lagged_correlations(
    target: Series, factor: Series, *, max_lag: int = MAX_LAG, min_samples: int = MIN_SAMPLES
) -> dict:
    """Correlate ``factor`` on day d-lag with ``target`` on day d, lag 0..max_lag."""
    lags = []
    for lag in range(max_lag + 1):
        xs, ys = [], []
        for day, y in target.items():
            x = factor.get(day - timedelta(days=lag))
            if x is not None:
                xs.append(x)
                ys.append(y)
        n = len(xs)
        r = spearman(xs, ys) if n >= min_samples else None
        lags.append({"lag": lag, "n": n, "r": None if r is None else round(r, 4)})
    usable = [item for item in lags if item["r"] is not None]
    if not usable:
        return {
            "state": "too_few_samples",
            "min_samples": min_samples,
            "n": max((item["n"] for item in lags), default=0),
            "r": None,
            "lag": None,
            "lags": lags,
        }
    best = max(usable, key=lambda item: abs(item["r"]))
    return {
        "state": "ok",
        "min_samples": min_samples,
        "n": best["n"],
        "r": best["r"],
        "lag": best["lag"],
        "lags": lags,
    }


def rolling_mean(series: Series, window: int) -> list[dict]:
    days = sorted(series)
    out = []
    for i, day in enumerate(days):
        start = day - timedelta(days=window - 1)
        vals = [series[d] for d in days[: i + 1] if d >= start]
        out.append({"date": day.isoformat(), "value": round(mean(vals), 3)})
    return out


def change_point(series: Series, *, min_side: int = 7) -> dict | None:
    """Split with the largest difference in means (descriptive, not a test)."""
    days = sorted(series)
    vals = [series[d] for d in days]
    if len(vals) < 2 * min_side:
        return None
    best = None
    for k in range(min_side, len(vals) - min_side + 1):
        shift = mean(vals[k:]) - mean(vals[:k])
        if best is None or abs(shift) > abs(best[1]):
            best = (k, shift)
    assert best is not None
    k, shift = best
    return {
        "date": days[k].isoformat(),
        "before_mean": round(mean(vals[:k]), 2),
        "after_mean": round(mean(vals[k:]), 2),
        "shift": round(shift, 2),
    }


def streaks(series: Series, *, threshold: float) -> dict:
    """Longest and current run of consecutive days at or above ``threshold``."""
    longest = current = 0
    prev: date | None = None
    for day in sorted(series):
        if series[day] >= threshold:
            consecutive = prev is not None and (day - prev).days == 1 and current > 0
            current = current + 1 if consecutive else 1
        else:
            current = 0
        longest = max(longest, current)
        prev = day
    return {"threshold": threshold, "longest": longest, "current": current}
