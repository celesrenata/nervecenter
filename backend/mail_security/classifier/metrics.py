"""From-scratch evaluation metrics and latency statistics (task 3.2, R-021.1).

Pure stdlib. Quality metrics are only as meaningful as the labels they are
computed on; on SYNTHETIC data they say nothing about real-world detection.
Latency figures describe only the machine they were measured on.

Conventions
-----------
* Scores are UNCALIBRATED raw scores; ``score >= threshold`` is "positive".
* Labels are truthy (positive / malicious) or falsy.
* A ratio with a zero denominator is ``None`` (undefined), never a fake 0.
* :func:`pr_auc` is *average precision* (step-wise sum of
  ``(R_n - R_{n-1}) * P_n``), with tied scores collapsed into one threshold.
  It is not trapezoid-interpolated PR area.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import TypeVar

T = TypeVar("T")


def _check(labels: Sequence[object], scores: Sequence[float]) -> None:
    if len(labels) != len(scores):
        raise ValueError("labels and scores must have the same length")
    if any(not math.isfinite(s) for s in scores):
        raise ValueError("scores must be finite")


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


@dataclass(frozen=True)
class Confusion:
    """Confusion counts at one threshold (``score >= threshold`` is positive)."""

    tp: int
    fp: int
    tn: int
    fn: int

    @property
    def precision(self) -> float | None:
        return _ratio(self.tp, self.tp + self.fp)

    @property
    def recall(self) -> float | None:
        return _ratio(self.tp, self.tp + self.fn)

    @property
    def false_positive_rate(self) -> float | None:
        return _ratio(self.fp, self.fp + self.tn)


def confusion_at_threshold(
    labels: Sequence[int | bool], scores: Sequence[float], threshold: float
) -> Confusion:
    """Count TP/FP/TN/FN with ``score >= threshold`` predicted positive."""
    _check(labels, scores)
    tp = fp = tn = fn = 0
    for label, score in zip(labels, scores, strict=True):
        predicted = score >= threshold
        if label:
            tp += predicted
            fn += not predicted
        else:
            fp += predicted
            tn += not predicted
    return Confusion(tp=tp, fp=fp, tn=tn, fn=fn)


def pr_auc(labels: Sequence[int | bool], scores: Sequence[float]) -> float | None:
    """Average precision. ``None`` when there are no positives (undefined)."""
    _check(labels, scores)
    positives = sum(1 for label in labels if label)
    if positives == 0:
        return None
    order = sorted(range(len(scores)), key=lambda i: -scores[i])
    tp = fp = 0
    previous_recall = 0.0
    area = 0.0
    i = 0
    while i < len(order):
        j = i
        while j < len(order) and scores[order[j]] == scores[order[i]]:
            if labels[order[j]]:
                tp += 1
            else:
                fp += 1
            j += 1
        recall = tp / positives
        area += (recall - previous_recall) * (tp / (tp + fp))
        previous_recall = recall
        i = j
    return area


def percentile(values: Iterable[float], q: float) -> float | None:
    """Nearest-rank percentile (``q`` in (0, 100]); ``None`` for no values."""
    ordered = sorted(values)
    if not ordered:
        return None
    rank = max(1, math.ceil(q / 100.0 * len(ordered)))
    return ordered[min(rank, len(ordered)) - 1]


@dataclass(frozen=True)
class LatencyReport:
    """Per-call latency percentiles (ms) and overall throughput.

    Only meaningful for the machine it was measured on.
    """

    count: int
    p50_ms: float | None
    p95_ms: float | None
    p99_ms: float | None
    total_seconds: float
    throughput_per_second: float | None


def measure_latency(
    func: Callable[[T], object],
    items: Sequence[T],
    clock: Callable[[], float] = time.perf_counter,
) -> LatencyReport:
    """Time ``func(item)`` per item with an injectable monotonic ``clock``.

    The clock is called once before the loop, twice per item, and once after
    the loop. Throughput is ``None`` when no time elapsed (never infinity).
    """
    total_start = clock()
    durations_ms: list[float] = []
    for item in items:
        start = clock()
        func(item)
        durations_ms.append((clock() - start) * 1000.0)
    total = clock() - total_start
    return LatencyReport(
        count=len(durations_ms),
        p50_ms=percentile(durations_ms, 50),
        p95_ms=percentile(durations_ms, 95),
        p99_ms=percentile(durations_ms, 99),
        total_seconds=total,
        throughput_per_second=(len(durations_ms) / total) if total > 0 and durations_ms else None,
    )
