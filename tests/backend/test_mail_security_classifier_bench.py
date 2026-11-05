"""NerveShield task 3.2 -- CPU baseline + dataset-free harness building blocks.

Honesty: everything here runs on SYNTHETIC, template-generated records. Passing
these tests proves the code behaves as specified. It says NOTHING about
real-world detection quality, and timings on this machine say nothing about the
GTi14 CPU or the Intel Arc GPU.
"""

from __future__ import annotations

import ast
import json
import math
from pathlib import Path

import pytest
from pydantic import ValidationError

from backend.mail_security.classifier import baseline, bench, features, metrics, synthetic
from backend.mail_security.models import ScoreSemantics

PKG_DIR = Path(features.__file__).resolve().parent


def _valid_kwargs() -> dict:
    return {
        "url_domain_ngrams": ("log", "ogi"),
        "finding_rule_counts": {"URL_SHORTENED_LINK": 1},
        "url_count": 1,
        "distinct_url_domain_count": 1,
        "attachment_count": 0,
        "mime_part_count": 2,
        "remote_image_count": 0,
        "sender_domain_tokens": ("example", "test"),
    }


# --------------------------------------------------------------------------- #
# generator determinism + schema strictness
# --------------------------------------------------------------------------- #


def test_generator_is_deterministic_by_seed() -> None:
    a = synthetic.generate_examples(seed=7, count=40)
    b = synthetic.generate_examples(seed=7, count=40)
    c = synthetic.generate_examples(seed=8, count=40)
    assert a == b
    assert a != c
    assert len(a) == 40
    labels = {e.label for e in a}
    assert labels == {features.Label.BENIGN, features.Label.MALICIOUS}


def test_generator_zero_and_negative_count() -> None:
    assert synthetic.generate_examples(seed=1, count=0) == []
    with pytest.raises(ValueError):
        synthetic.generate_examples(seed=1, count=-1)


def test_generated_records_validate_under_strict_schema() -> None:
    for ex in synthetic.generate_examples(seed=3, count=60):
        again = features.FeatureRecord.model_validate(ex.features.model_dump())
        assert again == ex.features


def test_feature_record_rejects_unknown_field() -> None:
    kwargs = _valid_kwargs() | {"subject": "hello"}
    with pytest.raises(ValidationError):
        features.FeatureRecord(**kwargs)


def test_feature_record_rejects_oversize_values() -> None:
    too_many = tuple("abc" for _ in range(features.MAX_NGRAMS + 1))
    with pytest.raises(ValidationError):
        features.FeatureRecord(**(_valid_kwargs() | {"url_domain_ngrams": too_many}))
    with pytest.raises(ValidationError):
        features.FeatureRecord(
            **(_valid_kwargs() | {"url_domain_ngrams": ("a" * (features.MAX_NGRAM_LEN + 1),)})
        )
    with pytest.raises(ValidationError):
        features.FeatureRecord(**(_valid_kwargs() | {"url_count": features.MAX_COUNT + 1}))
    with pytest.raises(ValidationError):
        features.FeatureRecord(**(_valid_kwargs() | {"url_count": -1}))
    many = {f"RULE_{i}": 1 for i in range(features.MAX_RULES + 1)}
    with pytest.raises(ValidationError):
        features.FeatureRecord(**(_valid_kwargs() | {"finding_rule_counts": many}))
    with pytest.raises(ValidationError):
        features.FeatureRecord(
            **(_valid_kwargs() | {"sender_domain_tokens": ("x" * (features.MAX_TOKEN_LEN + 1),)})
        )


def test_feature_record_rejects_address_or_text_shaped_values() -> None:
    with pytest.raises(ValidationError):
        features.FeatureRecord(**(_valid_kwargs() | {"sender_domain_tokens": ("a@b.com",)}))
    with pytest.raises(ValidationError):
        features.FeatureRecord(**(_valid_kwargs() | {"url_domain_ngrams": ("a b",)}))
    with pytest.raises(ValidationError):
        features.FeatureRecord(**(_valid_kwargs() | {"finding_rule_counts": {"free text here": 1}}))


def test_feature_record_is_frozen() -> None:
    rec = features.FeatureRecord(**_valid_kwargs())
    with pytest.raises(ValidationError):
        rec.url_count = 5  # type: ignore[misc]


def test_no_raw_text_fields_exist() -> None:
    banned = {
        "subject", "body", "text", "html", "sender", "from", "to", "address", "email",
        "url", "urls", "message_id", "raw", "content", "snippet", "filename",
    }  # fmt: skip
    for model in (features.FeatureRecord, features.LabeledExample):
        assert not banned & set(model.model_fields), model
    assert "label" not in features.FeatureRecord.model_fields  # no label leakage


def test_generated_values_contain_no_at_sign_or_whitespace() -> None:
    for ex in synthetic.generate_examples(seed=11, count=80):
        strings = [
            *ex.features.url_domain_ngrams,
            *ex.features.sender_domain_tokens,
            *ex.features.finding_rule_counts,
        ]
        assert strings
        assert all("@" not in s and not any(ch.isspace() for ch in s) for s in strings)


# --------------------------------------------------------------------------- #
# metrics vs hand-computed values
# --------------------------------------------------------------------------- #


def test_confusion_at_threshold_hand_computed() -> None:
    labels = [1, 1, 1, 0, 0, 0, 0]
    scores = [0.9, 0.6, 0.2, 0.7, 0.4, 0.1, 0.5]
    c = metrics.confusion_at_threshold(labels, scores, threshold=0.5)
    # predicted positive: 0.9(TP) 0.6(TP) 0.7(FP) 0.5(FP, >= threshold)
    assert (c.tp, c.fp, c.tn, c.fn) == (2, 2, 2, 1)
    assert c.precision == pytest.approx(0.5)
    assert c.recall == pytest.approx(2 / 3)
    assert c.false_positive_rate == pytest.approx(0.5)


def test_confusion_undefined_ratios_are_none_not_crash() -> None:
    c = metrics.confusion_at_threshold([0, 0], [0.1, 0.2], threshold=0.5)
    assert c.precision is None  # nothing predicted positive
    assert c.recall is None  # no actual positives
    assert c.false_positive_rate == 0.0
    e = metrics.confusion_at_threshold([], [], threshold=0.0)
    assert (e.tp, e.fp, e.tn, e.fn) == (0, 0, 0, 0)
    assert e.precision is None and e.recall is None and e.false_positive_rate is None


def test_pr_auc_tiny_worked_example() -> None:
    # sorted by score: 1(.9) 0(.8) 1(.7) 0(.6)
    # @.9: P=1,   R=.5  -> += .5 * 1
    # @.8: P=.5,  R=.5  -> += 0
    # @.7: P=2/3, R=1   -> += .5 * 2/3
    labels = [1, 0, 1, 0]
    scores = [0.9, 0.8, 0.7, 0.6]
    assert metrics.pr_auc(labels, scores) == pytest.approx(0.5 + (0.5 * 2 / 3))


def test_pr_auc_perfect_and_worst_ranking() -> None:
    assert metrics.pr_auc([1, 1, 0, 0], [0.9, 0.8, 0.2, 0.1]) == pytest.approx(1.0)
    # positives ranked last: @.4 P=1/3,R=.5 ; @.3 P=2/4,R=1 -> .5/3 + .5*.5
    assert metrics.pr_auc([0, 0, 1, 1], [0.9, 0.8, 0.4, 0.3]) == pytest.approx(
        0.5 * (1 / 3) + 0.5 * 0.5
    )


def test_pr_auc_ties_are_one_threshold_and_order_independent() -> None:
    # a single tied group: P = 1/2, R = 1 -> AP = 0.5 regardless of input order
    assert metrics.pr_auc([1, 0], [0.5, 0.5]) == pytest.approx(0.5)
    assert metrics.pr_auc([0, 1], [0.5, 0.5]) == pytest.approx(0.5)
    # mixed: tie group at .8 holds one pos + one neg, then a pos at .2
    # @.8: tp1 fp1 P=.5 R=.5 -> .25 ; @.2: tp2 fp1 P=2/3 R=1 -> .5*2/3
    a = metrics.pr_auc([1, 0, 1], [0.8, 0.8, 0.2])
    b = metrics.pr_auc([0, 1, 1], [0.8, 0.8, 0.2])
    assert a == pytest.approx(0.25 + (0.5 * 2 / 3))
    assert a == pytest.approx(b)


def test_pr_auc_edge_cases_do_not_crash() -> None:
    assert metrics.pr_auc([], []) is None
    assert metrics.pr_auc([0, 0, 0], [0.1, 0.2, 0.3]) is None  # no positives
    assert metrics.pr_auc([1, 1], [0.1, 0.2]) == pytest.approx(1.0)  # all positive
    assert metrics.pr_auc([1], [0.3]) == pytest.approx(1.0)


def test_metrics_reject_length_mismatch_and_nonfinite() -> None:
    with pytest.raises(ValueError):
        metrics.pr_auc([1, 0], [0.1])
    with pytest.raises(ValueError):
        metrics.confusion_at_threshold([1], [0.1, 0.2], 0.0)
    with pytest.raises(ValueError):
        metrics.pr_auc([1, 0], [math.nan, 0.1])


# --------------------------------------------------------------------------- #
# latency statistics with an injected clock
# --------------------------------------------------------------------------- #


def test_percentile_nearest_rank() -> None:
    values = [float(i) for i in range(1, 101)]
    assert metrics.percentile(values, 50) == 50.0
    assert metrics.percentile(values, 95) == 95.0
    assert metrics.percentile(values, 99) == 99.0
    assert metrics.percentile(list(reversed(values)), 99) == 99.0
    assert metrics.percentile([4.0], 99) == 4.0
    assert metrics.percentile([], 50) is None


class _FakeClock:
    """Each call advances by the next scripted step (seconds)."""

    def __init__(self, steps: list[float]) -> None:
        self._steps = iter(steps)
        self.now = 0.0

    def __call__(self) -> float:
        self.now += next(self._steps)
        return self.now


def test_measure_latency_with_injected_clock() -> None:
    # call sequence: total_start, (start,end)*3, total_end
    clock = _FakeClock([0.0, 0.0, 0.010, 0.0, 0.020, 0.0, 0.030, 0.0])
    seen: list[int] = []
    report = metrics.measure_latency(seen.append, [1, 2, 3], clock=clock)
    assert seen == [1, 2, 3]
    assert report.count == 3
    assert report.p50_ms == pytest.approx(20.0)
    assert report.p95_ms == pytest.approx(30.0)
    assert report.p99_ms == pytest.approx(30.0)
    assert report.total_seconds == pytest.approx(0.060)
    assert report.throughput_per_second == pytest.approx(3 / 0.060)


def test_measure_latency_empty_and_zero_elapsed() -> None:
    empty = metrics.measure_latency(lambda _x: None, [], clock=lambda: 1.0)
    assert empty.count == 0
    assert empty.p50_ms is None and empty.throughput_per_second is None
    frozen = metrics.measure_latency(lambda _x: None, [1, 2], clock=lambda: 5.0)
    assert frozen.count == 2
    assert frozen.throughput_per_second is None  # zero elapsed: not infinity


# --------------------------------------------------------------------------- #
# baseline: determinism, digest, uncalibrated scores
# --------------------------------------------------------------------------- #


def _train(seed: int = 1) -> baseline.BaselineModel:
    return baseline.train_baseline(synthetic.generate_examples(seed=100, count=120), seed=seed)


def test_training_is_deterministic_and_digest_stable() -> None:
    m1, m2 = _train(1), _train(1)
    assert m1.digest == m2.digest
    assert m1.to_dict() == m2.to_dict()
    assert len(m1.digest) == 64
    assert _train(2).digest != m1.digest  # seed changes the shuffle order


def test_serialization_roundtrip_and_tamper_detection() -> None:
    model = _train()
    payload = json.loads(json.dumps(model.to_dict()))
    restored = baseline.BaselineModel.from_dict(payload)
    assert restored.digest == model.digest
    rec = synthetic.generate_examples(seed=5, count=1)[0].features
    assert restored.predict(rec).raw_score == model.predict(rec).raw_score

    payload["bias"] = payload["bias"] + 1.0
    with pytest.raises(baseline.IntegrityError):
        baseline.BaselineModel.from_dict(payload)


def test_scores_are_labeled_uncalibrated_raw_scores() -> None:
    model = _train()
    rec = synthetic.generate_examples(seed=5, count=1)[0].features
    pred = model.predict(rec)
    assert pred.semantics is ScoreSemantics.RAW_SCORE
    assert pred.calibrated is False
    assert isinstance(pred.raw_score, float) and math.isfinite(pred.raw_score)
    assert "probab" not in {f.lower() for f in type(pred).__dataclass_fields__}
    assert "UNCALIBRATED" in baseline.SCORE_NOTICE
    assert model.to_dict()["score_semantics"] == "RAW_SCORE"
    assert model.to_dict()["calibrated"] is False


def test_batch_predict_matches_single_predict() -> None:
    model = _train()
    recs = [e.features for e in synthetic.generate_examples(seed=6, count=10)]
    assert model.predict_batch(recs) == [model.predict(r) for r in recs]
    assert model.predict_batch([]) == []


def test_train_rejects_empty_and_single_class() -> None:
    with pytest.raises(ValueError):
        baseline.train_baseline([], seed=0)
    only_benign = [
        e for e in synthetic.generate_examples(seed=2, count=30) if e.label is features.Label.BENIGN
    ]
    with pytest.raises(ValueError):
        baseline.train_baseline(only_benign, seed=0)


# --------------------------------------------------------------------------- #
# harness report honesty
# --------------------------------------------------------------------------- #


def test_benchmark_report_lines_all_carry_caveats() -> None:
    result = bench.run_benchmark(seed=1, train_count=80, test_count=40, clock=_tick_clock())
    lines = bench.render_report(result)
    assert lines
    for line in lines:
        assert "SYNTHETIC" in line, line
        assert "not real-world" in line.lower() or "not representative" in line.lower(), line
    text = "\n".join(lines)
    assert "UNCALIBRATED" in text
    assert "GTi14" in text and "Arc" in text
    assert "probability" not in text.lower().replace("not a probability", "")


def test_benchmark_is_reproducible_for_quality_numbers() -> None:
    a = bench.run_benchmark(seed=4, train_count=80, test_count=40, clock=_tick_clock())
    b = bench.run_benchmark(seed=4, train_count=80, test_count=40, clock=_tick_clock())
    assert a.model_digest == b.model_digest
    assert a.pr_auc == b.pr_auc
    assert a.confusion == b.confusion


def _tick_clock():
    state = {"t": 0.0}

    def clock() -> float:
        state["t"] += 0.001
        return state["t"]

    return clock


# --------------------------------------------------------------------------- #
# static: no network / subprocess / third-party imports in the new modules
# --------------------------------------------------------------------------- #

_FORBIDDEN = {
    "socket", "ssl", "subprocess", "urllib", "http", "requests", "httpx", "aiohttp",
    "ftplib", "smtplib", "asyncio", "multiprocessing", "ctypes", "numpy", "scipy",
    "sklearn", "torch", "onnx", "onnxruntime", "openvino", "psutil", "hypothesis",
    "pandas", "os",
}  # fmt: skip
_ALLOWED_NON_STDLIB = {"pydantic", "backend"}


def _imported_top_levels(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split(".")[0])
    return names


def test_new_modules_import_no_network_subprocess_or_third_party() -> None:
    import sys

    files = sorted(PKG_DIR.glob("*.py"))
    assert {p.name for p in files} >= {
        "__init__.py", "features.py", "synthetic.py", "baseline.py", "metrics.py", "bench.py",
    }  # fmt: skip
    for path in files:
        tops = _imported_top_levels(path)
        assert not tops & _FORBIDDEN, (path.name, tops & _FORBIDDEN)
        for top in tops - {"__future__"}:
            assert top in sys.stdlib_module_names or top in _ALLOWED_NON_STDLIB, (path.name, top)


def test_new_modules_are_not_wired_into_runtime() -> None:
    root = PKG_DIR.parents[1]  # backend/
    offenders = []
    for path in root.rglob("*.py"):
        if PKG_DIR in path.parents:
            continue
        if "mail_security.classifier" in path.read_text():
            offenders.append(str(path))
    assert offenders == []  # classifier output is not an authority (I-08)
