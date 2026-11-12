"""NerveShield task 6.5: model registry, staged deployment, rollback.

Done criterion: a simulated regression prevents release and rolls back.
"""

from __future__ import annotations

import inspect
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from backend.mail_security import model_registry as mr
from backend.mail_security.model_registry import (
    Approval,
    Lineage,
    ModelRecord,
    ModelRegistry,
    RegistryError,
    Stage,
    WindowMetrics,
)

T0 = datetime(2026, 11, 11, tzinfo=UTC)
H = "a" * 64
ELIGIBLE = {
    "version": "mail-model-promotion-gate@2",
    "outcome": "ELIGIBLE_FOR_HUMAN_REVIEW",
    "reasons": [],
}
BLOCKED = {
    "version": "mail-model-promotion-gate@2",
    "outcome": "BLOCKED",
    "reasons": ["PROXY_DATA_ONLY"],
}
GOOD = WindowMetrics(
    messages=2000, fpr=0.004, recall=0.92, error_rate=0.0, latency_p95_ms=160, ece=0.02
)


def _rec(model_id: str, gate: dict = ELIGIBLE) -> ModelRecord:
    return ModelRecord(
        model_id=model_id,
        candidate="C_qwen3_0.6b_lora",
        lineage=Lineage(
            {"model.xml": H, "model.bin": "b" * 64},
            H,
            "ssh:SHA256:D+0xwbt4UdtQZtaPLbbUndLpMyKkmfbqSQn2By08yLw",
            H,
            H,
            {"epochs": 2, "lr": 2e-4},
            "Qwen/Qwen3-0.6B",
            "902b028",
        ),
        gate=gate,
        registered_at=T0.isoformat(),
    )


def _ok(n: int = 0) -> Approval:
    return Approval("owner", f"approval-{n}", T0 + timedelta(hours=n))


def _to_active(reg: ModelRegistry, model_id: str, start: int = 0) -> None:
    reg.register(_rec(model_id), T0)
    reg.approve_transition(model_id, Stage.SHADOW, _ok(start))
    reg.record_window(model_id, GOOD, T0)
    reg.approve_transition(model_id, Stage.CANARY, _ok(start + 1))
    reg.record_window(model_id, GOOD, T0)
    reg.approve_transition(model_id, Stage.ACTIVE, _ok(start + 2))


# -- done criterion ------------------------------------------------------------------------


def test_regression_in_canary_prevents_release_and_rolls_back() -> None:
    reg = ModelRegistry()
    _to_active(reg, "m1")
    reg.record_window("m1", GOOD, T0)
    reg.register(_rec("m2"), T0)
    reg.approve_transition("m2", Stage.SHADOW, _ok(10))
    reg.record_window("m2", GOOD, T0)
    reg.approve_transition("m2", Stage.CANARY, _ok(11))
    regressed = replace(GOOD, fpr=0.02)  # simulated false-positive regression
    assert reg.record_window("m2", regressed, T0) is Stage.ROLLED_BACK
    assert reg.active == "m1" and reg.stage("m1") is Stage.ACTIVE
    with pytest.raises(RegistryError):
        reg.approve_transition("m2", Stage.ACTIVE, _ok(12))  # release prevented
    assert reg.route("any-message")["decides"] == "m1"
    last = reg.events[-1]
    assert last["kind"] == "ROLLED_BACK" and "FPR_ABOVE_LIMIT" in last["data"]["problems"]


def test_regression_after_activation_restores_last_known_good() -> None:
    reg = ModelRegistry()
    _to_active(reg, "m1")
    reg.record_window("m1", GOOD, T0)
    _to_active(reg, "m2", start=10)
    assert reg.stage("m1") is Stage.RETIRED and reg.active == "m2"
    assert reg.record_window("m2", replace(GOOD, recall=0.5), T0) is Stage.ROLLED_BACK
    assert reg.active == "m1" and reg.stage("m1") is Stage.ACTIVE


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        ({"fpr": 0.02}, "FPR_ABOVE_LIMIT"),
        ({"error_rate": 0.01}, "ERROR_RATE_ABOVE_LIMIT"),
        ({"latency_p95_ms": 900}, "LATENCY_ABOVE_LIMIT"),
        ({"ece": 0.09}, "CALIBRATION_DRIFT"),
        ({"recall": 0.80}, "RECALL_WORSE_THAN_ACTIVE"),
        ({"fpr": 0.008}, "FPR_WORSE_THAN_ACTIVE"),
    ],
)
def test_each_regression_kind_rolls_back(change: dict, problem: str) -> None:
    reg = ModelRegistry()
    _to_active(reg, "m1")
    reg.record_window("m1", GOOD, T0)
    reg.register(_rec("m2"), T0)
    reg.approve_transition("m2", Stage.SHADOW, _ok(20))
    assert reg.record_window("m2", replace(GOOD, **change), T0) is Stage.ROLLED_BACK
    assert problem in reg.events[-1]["data"]["problems"]


def test_too_small_window_changes_nothing() -> None:
    reg = ModelRegistry()
    reg.register(_rec("m1"), T0)
    reg.approve_transition("m1", Stage.SHADOW, _ok())
    assert reg.record_window("m1", replace(GOOD, messages=10), T0) is Stage.SHADOW
    with pytest.raises(RegistryError, match="NO_PASSING_WINDOW"):
        reg.approve_transition("m1", Stage.CANARY, _ok(1))


# -- no auto-promote -----------------------------------------------------------------------


def test_good_windows_never_promote() -> None:
    reg = ModelRegistry()
    reg.register(_rec("m1"), T0)
    reg.approve_transition("m1", Stage.SHADOW, _ok())
    for _ in range(50):
        assert reg.record_window("m1", GOOD, T0) is Stage.SHADOW
    assert reg.active is None


def test_only_one_public_method_moves_forward() -> None:
    public = [
        n for n, _ in inspect.getmembers(ModelRegistry, inspect.isfunction) if not n.startswith("_")
    ]
    assert sorted(public) == [
        "approve_transition",
        "check_window",
        "export",
        "record_window",
        "register",
        "route",
        "stage",
        "verify_log",
    ]
    src = inspect.getsource(mr)
    assert "auto_promote" not in src.replace("No auto-promote", "")


def test_blocked_gate_cannot_enter_shadow() -> None:
    reg = ModelRegistry()
    assert reg.register(_rec("m1", BLOCKED), T0) is Stage.BLOCKED
    with pytest.raises(RegistryError):
        reg.approve_transition("m1", Stage.SHADOW, _ok())


@pytest.mark.parametrize("target", [Stage.CANARY, Stage.ACTIVE, Stage.RETIRED, Stage.REGISTERED])
def test_stages_cannot_be_skipped(target: Stage) -> None:
    reg = ModelRegistry()
    reg.register(_rec("m1"), T0)
    with pytest.raises(RegistryError, match="ILLEGAL_TRANSITION"):
        reg.approve_transition("m1", target, _ok())


def test_each_stage_needs_its_own_passing_window() -> None:
    reg = ModelRegistry()
    reg.register(_rec("m1"), T0)
    reg.approve_transition("m1", Stage.SHADOW, _ok())
    reg.record_window("m1", GOOD, T0)
    reg.approve_transition("m1", Stage.CANARY, _ok(1))
    with pytest.raises(RegistryError, match="NO_PASSING_WINDOW"):
        reg.approve_transition("m1", Stage.ACTIVE, _ok(2))


def test_rolled_back_model_stays_down() -> None:
    reg = ModelRegistry()
    reg.register(_rec("m1"), T0)
    reg.approve_transition("m1", Stage.SHADOW, _ok())
    reg.record_window("m1", replace(GOOD, fpr=0.5), T0)
    for target in Stage:
        with pytest.raises(RegistryError):
            reg.approve_transition("m1", target, _ok(3))
    with pytest.raises(RegistryError, match="NOT_DEPLOYED"):
        reg.record_window("m1", GOOD, T0)


@pytest.mark.parametrize("approval", [Approval("", "x", T0), Approval("owner", "has space", T0)])
def test_approvals_must_be_valid_references(approval: Approval) -> None:
    reg = ModelRegistry()
    reg.register(_rec("m1"), T0)
    with pytest.raises(RegistryError, match="INVALID_APPROVAL"):
        reg.approve_transition("m1", Stage.SHADOW, approval)


# -- lineage -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "lineage_change",
    [
        {"artifact_sha256": {}},
        {"artifact_sha256": {"model.bin": "not-a-digest"}},
        {"dataset_manifest_sha256": "x"},
        {"privacy_report_sha256": ""},
        {"code_commit": ""},
    ],
)
def test_incomplete_lineage_is_refused(lineage_change: dict) -> None:
    rec = _rec("m1")
    bad = replace(rec, lineage=replace(rec.lineage, **lineage_change))
    with pytest.raises(RegistryError, match="LINEAGE_INCOMPLETE"):
        ModelRegistry().register(bad, T0)


@pytest.mark.parametrize("key_id", ["UNSIGNED", "bad id", "ssh:SHA256:short", "0123", ""])
def test_unsigned_dataset_manifest_is_refused(key_id: str) -> None:
    rec = _rec("m1")
    bad = replace(rec, lineage=replace(rec.lineage, dataset_signing_key_id=key_id))
    with pytest.raises(RegistryError, match="DATASET_MANIFEST_NOT_SIGNED"):
        ModelRegistry().register(bad, T0)


def test_file_key_ids_are_accepted() -> None:
    rec = _rec("m1")
    ok = replace(rec, lineage=replace(rec.lineage, dataset_signing_key_id="0123456789abcdef"))
    assert ModelRegistry().register(ok, T0) is Stage.REGISTERED


def test_missing_gate_and_duplicates_are_refused() -> None:
    reg = ModelRegistry()
    with pytest.raises(RegistryError, match="GATE_DECISION_MISSING"):
        reg.register(replace(_rec("m1"), gate={}), T0)
    reg.register(_rec("m1"), T0)
    with pytest.raises(RegistryError, match="DUPLICATE"):
        reg.register(_rec("m1"), T0)


# -- log -----------------------------------------------------------------------------------


def _busy_registry() -> ModelRegistry:
    reg = ModelRegistry()
    _to_active(reg, "m1")
    reg.record_window("m1", GOOD, T0)
    reg.register(_rec("m2"), T0)
    reg.approve_transition("m2", Stage.SHADOW, _ok(9))
    reg.record_window("m2", replace(GOOD, fpr=0.3), T0)
    return reg


def test_event_log_verifies_and_detects_tampering() -> None:
    reg = _busy_registry()
    events = reg.events
    assert ModelRegistry.verify_log(events)
    edited = [dict(e) for e in events]
    edited[3]["data"] = {**edited[3]["data"], "approver": "mallory"}
    assert not ModelRegistry.verify_log(edited)
    assert not ModelRegistry.verify_log(events[:2] + events[3:])  # deletion
    assert not ModelRegistry.verify_log([events[1], events[0], *events[2:]])  # reorder
    assert ModelRegistry.verify_log(events[:-1])  # a chain alone cannot see a dropped tail...
    assert not ModelRegistry.verify_log(events[:-1], expected_head=reg.head)  # ...the anchor can
    assert ModelRegistry.verify_log(events, expected_head=reg.head)


def test_events_are_a_copy() -> None:
    reg = _busy_registry()
    reg.events[0]["kind"] = "X"
    assert reg.events[0]["kind"] == "REGISTERED"


# -- routing -------------------------------------------------------------------------------


def test_canary_gets_about_its_fraction_and_shadow_never_decides() -> None:
    reg = ModelRegistry()
    _to_active(reg, "m1")
    reg.record_window("m1", GOOD, T0)
    reg.register(_rec("m2"), T0)
    reg.approve_transition("m2", Stage.SHADOW, _ok(10))
    reg.record_window("m2", GOOD, T0)
    reg.approve_transition("m2", Stage.CANARY, _ok(11))
    reg.register(_rec("m3"), T0)
    reg.approve_transition("m3", Stage.SHADOW, _ok(12))
    routes = [reg.route(f"msg-{i}") for i in range(20000)]
    share = sum(r["decides"] == "m2" for r in routes) / len(routes)
    assert 0.04 < share < 0.06
    assert all(r["decides"] in ("m1", "m2") and r["observes"] == "m3" for r in routes)
    assert reg.route("msg-7") == reg.route("msg-7")  # deterministic


def test_export_round_trips_to_json() -> None:
    import json

    data = _busy_registry().export()
    assert json.loads(json.dumps(data))["stages"] == {"m1": "ACTIVE", "m2": "ROLLED_BACK"}
