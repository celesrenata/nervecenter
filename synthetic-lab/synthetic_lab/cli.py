"""Synthetic-lab CI gate CLI (T003.AC10 regression gate).

Usage (from the repo root):

    PYTHONPATH=. uv run python -m synthetic_lab [corpus.json] [--json] [--quiet]

Loads the versioned corpus, replays it deterministically (fixture-only:
no network, no secrets, no clock reads beyond the corpus anchor), and
evaluates every case against its labeled expectation. Prints per-case
verdicts with evidence, detection-plane precision/recall, and the
regression verdict.

Exit codes:
    0 — no regression (pending ask cases are reported, not failed)
    1 — at least one case failed (regression)
    2 — corpus could not be loaded / validated
"""

from __future__ import annotations

import argparse
import json
import sys

from .evaluator import evaluate
from .replay import corpus_fingerprint, replay_corpus
from .schema import default_corpus_path, load_corpus


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="synthetic-lab", description=__doc__)
    parser.add_argument(
        "corpus",
        nargs="?",
        help="path to corpus.json (default: synthetic-lab/corpus/corpus.json)",
    )
    parser.add_argument("--json", action="store_true", help="emit a JSON report instead of text")
    parser.add_argument("--quiet", action="store_true", help="emit only the exit-relevant lines")
    args = parser.parse_args(argv)

    try:
        corpus = load_corpus(args.corpus or default_corpus_path())
    except Exception as exc:  # noqa: BLE001 — any load/validate failure is gate error 2
        print(f"corpus error: {exc}", file=sys.stderr)
        return 2

    observations = replay_corpus(corpus)
    report = evaluate(corpus, observations, fingerprint=corpus_fingerprint(corpus))

    if args.json:
        print(
            json.dumps(
                {
                    "corpus": report.corpus_id,
                    "fingerprint": report.fingerprint,
                    "exit_code": report.exit_code,
                    "regression": report.regression,
                    "pass": report.passed,
                    "fail": report.failed,
                    "pending": report.pending,
                    "detection": {
                        "precision": report.detection_metrics.precision,
                        "recall": report.detection_metrics.recall,
                        "f1": report.detection_metrics.f1,
                        "tp": report.detection_metrics.true_positives,
                        "fp": report.detection_metrics.false_positives,
                        "fn": report.detection_metrics.false_negatives,
                        "tn": report.detection_metrics.true_negatives,
                    },
                    "verdicts": [
                        {
                            "id": v.case_id,
                            "plane": v.plane,
                            "status": v.status,
                            "evidence": v.evidence,
                        }
                        for v in report.verdicts
                    ],
                },
                indent=2,
            )
        )
    elif args.quiet:
        for verdict in report.verdicts:
            if verdict.status != "pass":
                print(f"[{verdict.status.upper()}] {verdict.case_id}")
        print(f"regression={'yes' if report.regression else 'no'} exit={report.exit_code}")
    else:
        print(report.summary())
    return report.exit_code


if __name__ == "__main__":
    sys.exit(main())
