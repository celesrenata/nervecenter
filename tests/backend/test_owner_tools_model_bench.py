"""Unit tests for the 6.3 benchmark harness logic (skipped without the ``ml`` extra)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("sklearn")

from backend.owner_tools import model_bench as mb  # noqa: E402


def test_received_time_prefers_topmost_received_over_forged_date() -> None:
    headers = [
        ("Received", "from mx.example by inbox.example; Tue, 3 Sep 2002 10:00:00 +0000"),
        ("Received", "from evil by relay; Mon, 1 Jan 1980 00:00:00 +0000"),
        ("Date", "Fri, 1 Jan 2028 00:00:00 +0000"),
    ]
    when, source = mb.received_time(headers)
    assert source == "received" and when == datetime(2002, 9, 3, 10, tzinfo=UTC)


def test_received_time_falls_back_to_date_then_none() -> None:
    assert mb.received_time([("Date", "Tue, 3 Sep 2002 10:00:00 +0000")])[1] == "date"
    assert mb.received_time([("Received", "no date here"), ("Date", "garbage")]) == (None, "none")


def test_metrics_counts_and_cost() -> None:
    y = np.array([1, 1, 0, 0, 0])
    p = np.array([0.9, 0.2, 0.8, 0.1, 0.05])
    m = mb.metrics(y, p, 0.5)
    assert (m["tp"], m["fp"], m["fn"], m["tn"]) == (1, 1, 1, 2)
    assert m["cost_fp5_fn1"] == 6 and m["precision"] == 0.5 and m["recall"] == 0.5


def test_ece_is_zero_for_perfect_calibration_and_large_when_overconfident() -> None:
    y = np.array([0, 1] * 50)
    assert mb.ece(y, np.full(100, 0.5)) == pytest.approx(0.0)
    assert mb.ece(np.zeros(100), np.full(100, 0.95)) == pytest.approx(0.95)


def test_recall_at_fpr() -> None:
    y = np.array([1, 1, 1, 0, 0, 0, 0, 0, 0, 0])
    p = np.array([0.99, 0.98, 0.1, 0.97, 0.2, 0.15, 0.12, 0.11, 0.05, 0.01])
    assert mb._recall_at_fpr(y, p, 0.0) == pytest.approx(2 / 3)


def test_paired_bootstrap_is_deterministic_and_finds_a_clear_difference() -> None:
    rng = np.random.default_rng(1)
    y = rng.integers(0, 2, 400)
    good = y + rng.normal(0, 0.3, 400)
    bad = y + rng.normal(0, 2.0, 400)
    a = mb.paired_bootstrap(y, {"bad": bad, "good": good}, "bad", n=200)
    b = mb.paired_bootstrap(y, {"bad": bad, "good": good}, "bad", n=200)
    assert a == b
    lo, hi = a["diff_vs_baseline_ci95"]["good"]["roc_auc"]
    assert lo > 0 and hi > lo
