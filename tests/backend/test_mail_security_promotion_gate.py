"""6.4 blocked-promotion criteria."""

from __future__ import annotations

from typing import Any

import pytest

from backend.mail_security.promotion_gate import (
    BlockReason as B,
)
from backend.mail_security.promotion_gate import (
    GateOutcome,
    GateThresholds,
    evaluate,
)

GOOD: dict[str, Any] = {
    "redaction_regressions": 0,
    "canary_exposure_bits": {1: 0.8, 4: 1.5, 16: 2.9},
    "mia_auc": 0.52,
    "artifact_findings": [],
    "retrieval_returns_text": False,
    "auc_diff_vs_baseline_ci_low": 0.002,
    "ece": 0.03,
    "proxy_data": False,
    "ran_on_target_runtime": True,
}


def test_a_clean_report_is_only_eligible_for_human_review() -> None:
    decision = evaluate(GOOD)
    assert decision.outcome is GateOutcome.ELIGIBLE_FOR_HUMAN_REVIEW and decision.reasons == ()
    assert {o.value for o in GateOutcome} == {
        "BLOCKED",
        "ELIGIBLE_FOR_HUMAN_REVIEW",
    }  # no "PROMOTE"


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"redaction_regressions": 1}, B.REDACTION_REGRESSION),
        ({"canary_exposure_bits": {1: 3.5, 4: 4, 16: 5}}, B.CANARY_EXPOSURE_HIGH),
        ({"canary_exposure_bits": {1: 1, 4: 2, 16: 5.5}}, B.CANARY_EXPOSURE_HIGH),
        ({"canary_exposure_bits": {1: 1, 4: 2, 16: 3.08}}, B.CANARY_EXPOSURE_HIGH),  # 6.4 baseline
        ({"mia_auc": 0.65}, B.MEMBERSHIP_INFERENCE_RISK),
        ({"artifact_findings": ["WORLD_READABLE"]}, B.ARTIFACT_ACCESS_UNSAFE),
        ({"retrieval_returns_text": True}, B.RETRIEVAL_LEAKAGE),
        ({"auc_diff_vs_baseline_ci_low": -0.001}, B.NOT_BETTER_THAN_BASELINE),
        ({"auc_diff_vs_baseline_ci_low": 0.0}, B.NOT_BETTER_THAN_BASELINE),
        ({"ece": 0.09}, B.POORLY_CALIBRATED),
        ({"proxy_data": True}, B.PROXY_DATA_ONLY),
        ({"ran_on_target_runtime": False}, B.NOT_RUN_ON_TARGET_RUNTIME),
    ],
)
def test_each_failure_blocks_with_its_reason(change: dict[str, Any], reason: B) -> None:
    decision = evaluate({**GOOD, **change})
    assert decision.outcome is GateOutcome.BLOCKED and decision.reasons == (reason,)


@pytest.mark.parametrize("key", list(GOOD))
def test_missing_evidence_blocks(key: str) -> None:
    report = {k: v for k, v in GOOD.items() if k != key}
    decision = evaluate(report)
    assert decision.outcome is GateOutcome.BLOCKED and B.MISSING_EVIDENCE in decision.reasons


@pytest.mark.parametrize(
    "change",
    [
        {"mia_auc": float("nan")},
        {"mia_auc": "0.5"},
        {"mia_auc": True},
        {"canary_exposure_bits": {}},
        {"canary_exposure_bits": {1: None}},
        {"canary_exposure_bits": "lots"},
        {"redaction_regressions": "0"},
        {"artifact_findings": "none"},
        {"retrieval_returns_text": "no"},
    ],
)
def test_malformed_evidence_blocks(change: dict[str, Any]) -> None:
    assert evaluate({**GOOD, **change}).outcome is GateOutcome.BLOCKED


def test_thresholds_are_explicit_owner_choices() -> None:
    relaxed = GateThresholds(allow_proxy_data=True, require_target_runtime=False)
    report = {**GOOD, "proxy_data": True, "ran_on_target_runtime": False}
    assert evaluate(report, relaxed).outcome is GateOutcome.ELIGIBLE_FOR_HUMAN_REVIEW
    assert evaluate(report).outcome is GateOutcome.BLOCKED


def test_decision_serialises_with_version() -> None:
    d = evaluate({**GOOD, "ece": 0.2}).to_dict()
    assert d == {
        "version": "mail-model-promotion-gate@2",
        "outcome": "BLOCKED",
        "reasons": ["POORLY_CALIBRATED"],
    }
