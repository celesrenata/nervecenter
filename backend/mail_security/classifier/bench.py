"""Dataset-free benchmark harness for the CPU baseline (task 3.2).

Generates SYNTHETIC train/held-out sets from seeds, trains the baseline, and
reports quality (precision, recall, PR-AUC, FPR at a fixed threshold) and
latency/throughput. No files, network, real mail or private data are touched.

Honesty (every rendered line carries its own caveat):
* Quality numbers are on SYNTHETIC template data whose classes are trivially
  separable; they are NOT real-world detection quality.
* Latency/throughput describe only the machine that ran this; they are NOT
  representative of the GTi14 CPU or the Intel Arc GPU.
* The decision threshold is fixed at 0.0 on the raw score a priori and is not
  tuned. Scores are UNCALIBRATED raw scores, not a probability.

Run: ``python -m backend.mail_security.classifier.bench --seed 1``
"""

from __future__ import annotations

import argparse
import platform
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from backend.mail_security.classifier.baseline import SCORE_NOTICE, train_baseline
from backend.mail_security.classifier.features import Label
from backend.mail_security.classifier.metrics import (
    Confusion,
    LatencyReport,
    confusion_at_threshold,
    measure_latency,
    pr_auc,
)
from backend.mail_security.classifier.synthetic import generate_examples

#: A-priori decision threshold on the raw score. Not tuned.
DEFAULT_THRESHOLD = 0.0
#: Offset so the held-out set uses a different seed than the training set.
_HELD_OUT_SEED_OFFSET = 1_000_003


@dataclass(frozen=True)
class BenchmarkResult:
    seed: int
    train_count: int
    test_count: int
    test_positive_count: int
    threshold: float
    model_digest: str
    confusion: Confusion
    pr_auc: float | None
    single_latency: LatencyReport
    batch_count: int
    batch_seconds: float
    batch_throughput_per_second: float | None
    platform_description: str
    python_version: str


def platform_description() -> str:
    return f"{platform.system()} {platform.machine()} ({platform.python_implementation()})"


def run_benchmark(
    seed: int,
    train_count: int = 2000,
    test_count: int = 1000,
    threshold: float = DEFAULT_THRESHOLD,
    clock: Callable[[], float] = time.perf_counter,
) -> BenchmarkResult:
    """Train on seeded synthetic data, evaluate on a differently seeded set."""
    train = generate_examples(seed=seed, count=train_count)
    test = generate_examples(seed=seed + _HELD_OUT_SEED_OFFSET, count=test_count)
    model = train_baseline(train, seed=seed)

    records = [e.features for e in test]
    labels = [1 if e.label is Label.MALICIOUS else 0 for e in test]
    scores = [p.raw_score for p in model.predict_batch(records)]

    single = measure_latency(model.predict, records, clock=clock)
    batch_start = clock()
    model.predict_batch(records)
    batch_seconds = clock() - batch_start
    throughput = (len(records) / batch_seconds) if batch_seconds > 0 and records else None

    return BenchmarkResult(
        seed=seed,
        train_count=len(train),
        test_count=len(test),
        test_positive_count=sum(labels),
        threshold=threshold,
        model_digest=model.digest,
        confusion=confusion_at_threshold(labels, scores, threshold),
        pr_auc=pr_auc(labels, scores),
        single_latency=single,
        batch_count=len(records),
        batch_seconds=batch_seconds,
        batch_throughput_per_second=throughput,
        platform_description=platform_description(),
        python_version=platform.python_version(),
    )


def _fmt(value: float | None, digits: int = 4) -> str:
    return "undefined" if value is None else f"{value:.{digits}f}"


def render_report(result: BenchmarkResult) -> list[str]:
    """Render report lines; each line is self-contained with its caveat."""
    quality = "[SYNTHETIC data only; not real-world detection quality]"
    timing = (
        f"[SYNTHETIC inputs; measured on {result.platform_description}; "
        "not representative of GTi14 CPU or Arc GPU]"
    )
    c = result.confusion
    lat = result.single_latency
    return [
        f"seed={result.seed} train={result.train_count} held_out={result.test_count} "
        f"positives={result.test_positive_count} model_digest={result.model_digest} {quality}",
        f"scores: {SCORE_NOTICE} threshold={result.threshold} (fixed a priori, not tuned) "
        f"{quality}",
        f"confusion tp={c.tp} fp={c.fp} tn={c.tn} fn={c.fn} {quality}",
        f"precision={_fmt(c.precision)} recall={_fmt(c.recall)} "
        f"false_positive_rate={_fmt(c.false_positive_rate)} {quality}",
        f"pr_auc(average precision)={_fmt(result.pr_auc)}; classes are trivially separable "
        f"by construction, so a high value is expected and meaningless {quality}",
        f"per-message latency ms p50={_fmt(lat.p50_ms, 5)} p95={_fmt(lat.p95_ms, 5)} "
        f"p99={_fmt(lat.p99_ms, 5)} n={lat.count} {timing}",
        f"single-call throughput/s={_fmt(lat.throughput_per_second, 1)} {timing}",
        f"batch throughput/s={_fmt(result.batch_throughput_per_second, 1)} "
        f"(n={result.batch_count}, python={result.python_version}) {timing}",
    ]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="NerveShield CPU baseline synthetic benchmark")
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--train", type=int, default=2000)
    parser.add_argument("--test", type=int, default=1000)
    args = parser.parse_args(argv)
    result = run_benchmark(args.seed, args.train, args.test)
    for line in render_report(result):
        sys.stdout.write(line + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
