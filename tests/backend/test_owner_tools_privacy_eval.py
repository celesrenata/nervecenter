"""Unit tests for the 6.4 privacy-evaluation logic (skipped without the ``ml`` extra)."""

from __future__ import annotations

import os
import random
from datetime import UTC, datetime
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("sklearn")

from backend.mail_security.training_selection import detect_secrets  # noqa: E402
from backend.owner_tools import privacy_eval as pe  # noqa: E402
from backend.owner_tools.model_bench import Item  # noqa: E402

SALT = b"s" * 32


def test_redaction_suite_currently_passes() -> None:
    result = pe.redaction_regressions(SALT)
    assert result["failures"] == 0, result["missed"]
    assert result["suite_size"] == 11


def test_redaction_suite_detects_a_regression(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pe, "detect_secrets", lambda text: set())
    assert pe.redaction_regressions(SALT)["failures"] == 8


def test_canaries_evade_6_1_detectors_by_design() -> None:
    rng = random.Random(0)
    for _ in range(200):
        assert detect_secrets(pe._canary(rng)) == set()


def test_planting_respects_repetition_counts_and_hosts_are_benign() -> None:
    when = datetime(2002, 8, 1, tzinfo=UTC)
    fit = [Item(f"e{i}", i % 3 == 0, f"text {i}", when, f"t{i}", f"c{i}") for i in range(600)]
    planted, plan = pe.plant_canaries(fit)
    k = pe.CANARIES_PER_LEVEL
    assert [p["reps"] for p in plan] == [1] * k + [4] * k + [16] * k
    for p in plan:
        hosts = [i for i in planted if p["canary"] in i.text]
        assert len(hosts) == p["reps"] and all(h.label == 0 for h in hosts)
    assert len(planted) == len(fit)


def test_exposure_extremes() -> None:
    alts = np.linspace(1.0, 2.0, 999)
    assert pe.exposure(0.5, alts) == pytest.approx(np.log2(1000))  # ranked first: fully exposed
    assert pe.exposure(5.0, alts) == pytest.approx(0.0)  # ranked last
    assert 0 < pe.exposure(1.5, alts) < 2


def test_alternates_are_unique_and_exclude_the_true_canary() -> None:
    true = pe._canary(random.Random(3))
    alts = pe.alternates(true, 500, seed=1)
    assert len(set(alts)) == 500 and true not in alts


def test_mia_auc_is_near_chance_for_identical_distributions() -> None:
    rng = np.random.default_rng(0)
    y = rng.integers(0, 2, 2000)
    auc = pe.mia_auc(rng.normal(0, 1, 2000), y, rng.normal(0, 1, 2000), y)
    assert 0.45 < auc < 0.55


def test_mia_auc_detects_overfitting() -> None:
    y = np.array([0, 1] * 500)
    members = np.where(y == 1, 8.0, -8.0)  # confident and right on members
    non = np.where(y == 1, 0.5, -0.5)
    assert pe.mia_auc(members, y, non, y) > 0.95


def test_private_artifact_write_and_audit(tmp_path: Path) -> None:
    path = tmp_path / "models" / "A" / "scores.npy"
    pe.write_private(path, b"\x00\x01")
    assert pe.audit_artifact(path) == []
    os.chmod(path, 0o644)
    assert "FILE_ACCESSIBLE_BY_OTHERS" in pe.audit_artifact(path)
    os.chmod(path.parent, 0o755)
    assert "DIRECTORY_ACCESSIBLE_BY_OTHERS" in pe.audit_artifact(path)


def test_audit_flags_artifacts_inside_a_git_tree(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    path = tmp_path / "m" / "x"
    pe.write_private(path, b"x")
    assert "INSIDE_GIT_TREE" in pe.audit_artifact(path)


def test_returns_text_only_accepts_float_scores() -> None:
    assert pe.returns_text({"a": np.zeros(3)}, np.ones(2)) is False
    assert pe.returns_text({"a": np.array(["leaked text"])}) is True
    assert pe.returns_text(np.array([1, 2])) is True  # ids, not scores


def test_chance_levels_match_theory() -> None:
    c = pe.chance_levels(trials=4000)
    assert 0.7 < c["median_of_k_median"] < 1.3 and c["median_of_k_p99"] < 3.0
