"""NerveShield task 3.3 -- arcforge_embed_head model + arcforge_http benchmark protocol.

Honesty: every test talks only to ``arcforge_fake_server.FakeArcForge`` on 127.0.0.1. The
fake returns meaningless pseudo-random unit vectors. Passing proves the head, the guarded
protocol and the report schema behave as specified; it proves NOTHING about real hardware,
real latency, real quality or real failure modes.
"""

from __future__ import annotations

import ast
import itertools
import json
import logging
import sys
from collections.abc import Iterator
from pathlib import Path

import arcforge_fake_server as F
import pytest

from backend.mail_security.classifier import arcforge_bench as B
from backend.mail_security.classifier import arcforge_client as C
from backend.mail_security.classifier import arcforge_head as H
from backend.mail_security.classifier import harness
from backend.mail_security.classifier.baseline import IntegrityError
from backend.mail_security.classifier.features import FeatureRecord, Label, LabeledExample

CANARY = "CANARY_ZXQ_RULE"
PKG_DIR = Path(B.__file__).resolve().parent


def small_cfg(**kw) -> B.BenchConfig:  # noqa: ANN003
    base = {
        "batch_sizes": (1, 2),
        "concurrencies": (1, 2),
        "requests_per_cell": 4,
        "warmup_requests_per_batch_size": 1,
        "max_requests_per_second": 100.0,
        "max_total_requests": 300,
    }
    base.update(kw)
    return B.BenchConfig(**base)


def run(fake: F.FakeArcForge | list[F.FakeArcForge], **kw) -> dict:  # noqa: ANN003
    fakes = fake if isinstance(fake, list) else [fake]
    kw.setdefault("config", small_cfg())
    kw.setdefault("train_count", 40)
    kw.setdefault("test_count", 20)
    kw.setdefault("acknowledged", True)
    return B.run_arcforge_http(targets=[f.url for f in fakes], **kw)


@pytest.fixture
def fake() -> Iterator[F.FakeArcForge]:
    with F.FakeArcForge(dim=16) as f:
        yield f


@pytest.fixture
def fake2() -> Iterator[F.FakeArcForge]:
    with F.FakeArcForge(dim=16) as f:
        yield f


# --------------------------------------------------------------------------- #
# head model
# --------------------------------------------------------------------------- #
def _toy() -> tuple[list[list[float]], list[int]]:
    xs = [[1.0, 0.0], [0.9, 0.1], [0.0, 1.0], [0.1, 0.9]] * 5
    ys = [1, 1, 0, 0] * 5
    return xs, ys


def _train(seed: int = 7, **kw) -> H.EmbedHeadModel:  # noqa: ANN003
    xs, ys = _toy()
    return H.train_head(xs, ys, seed, embedder_model_id="m", embedder_version="v", **kw)


def test_head_is_deterministic_and_digest_stable() -> None:
    a, b = _train(), _train()
    assert a.to_dict() == b.to_dict() and a.digest == b.digest
    assert len(a.digest) == 64
    assert _train(seed=8).digest != a.digest


def test_head_separates_toy_data_and_is_uncalibrated() -> None:
    m = _train()
    pos, neg = m.predict([1.0, 0.0]), m.predict([0.0, 1.0])
    assert pos.raw_score > 0 > neg.raw_score
    assert pos.calibrated is False and pos.semantics.value == "RAW_SCORE"
    payload = m.to_dict()
    assert payload["calibrated"] is False and payload["score_semantics"] == "RAW_SCORE"
    assert "UNCALIBRATED" in payload["score_notice"]
    assert not any("probab" in k for k in payload)


def test_head_roundtrip_and_integrity() -> None:
    m = _train()
    again = H.EmbedHeadModel.from_dict(json.loads(json.dumps(m.to_dict())))
    assert again.digest == m.digest and again.predict([1.0, 0.0]) == m.predict([1.0, 0.0])
    tampered = m.to_dict()
    tampered["bias"] = 99.0
    with pytest.raises(IntegrityError):
        H.EmbedHeadModel.from_dict(tampered)
    bad = m.to_dict()
    bad["calibrated"] = True
    bad["digest"] = H._digest(bad)
    with pytest.raises(ValueError):
        H.EmbedHeadModel.from_dict(bad)


def test_head_input_validation() -> None:
    xs, ys = _toy()
    with pytest.raises(ValueError):
        H.train_head(xs, [1] * len(ys), 1, embedder_model_id="m", embedder_version="v")
    with pytest.raises(ValueError):
        H.train_head([[1.0], [1.0, 2.0]], [0, 1], 1, embedder_model_id="m", embedder_version="v")
    with pytest.raises(ValueError):
        H.train_head(
            [[float("nan"), 0.0], [0.0, 1.0]],
            [0, 1],
            1,
            embedder_model_id="m",
            embedder_version="v",
        )
    with pytest.raises(ValueError):
        _train().predict([1.0])


# --------------------------------------------------------------------------- #
# NOT_AVAILABLE gates: nothing is sent
# --------------------------------------------------------------------------- #
def test_no_target_is_not_available_with_zero_connections(fake: F.FakeArcForge) -> None:
    rep = B.run_arcforge_http(targets=[], acknowledged=True)
    assert rep["status"] == "NOT_AVAILABLE"
    assert rep["reason_codes"] == ["ARCFORGE_TARGET_NOT_PROVIDED"]
    assert rep["measurements"] is None and rep["quality"] is None
    assert fake.connections == 0


def test_missing_ack_is_not_available_with_zero_connections(fake: F.FakeArcForge) -> None:
    rep = B.run_arcforge_http(targets=[fake.url], acknowledged=False)
    assert rep["status"] == "NOT_AVAILABLE" and rep["reason_codes"] == ["ARCFORGE_ACK_MISSING"]
    assert rep["measurements"] is None and rep["quality"] is None
    assert fake.connections == 0


def test_neither_target_nor_ack_lists_both_codes() -> None:
    rep = B.run_arcforge_http()
    assert rep["reason_codes"] == ["ARCFORGE_ACK_MISSING", "ARCFORGE_TARGET_NOT_PROVIDED"]
    assert "honesty" in rep and rep["acceleration_claim"] is False


@pytest.mark.parametrize(
    "url", ["http://8.8.8.8:8100", "http://localhost:8100", "ftp://127.0.0.1:1"]
)
def test_refused_target_is_not_available_without_echo(url: str) -> None:
    rep = B.run_arcforge_http(targets=[url], acknowledged=True)
    assert rep["status"] == "NOT_AVAILABLE" and rep["reason_codes"] == ["ARCFORGE_TARGET_REFUSED"]
    assert "8.8.8.8" not in json.dumps(rep) and "localhost" not in json.dumps(rep)


def test_all_hosts_wedged_is_not_available_and_sends_no_embeddings(fake: F.FakeArcForge) -> None:
    fake.wedged = True
    fake.breaker_reason = "watchdog"
    rep = run(fake)
    assert rep["status"] == "NOT_AVAILABLE" and rep["reason_codes"] == ["ARCFORGE_NO_HEALTHY_HOST"]
    assert rep["measurements"] is None and rep["quality"] is None
    assert fake.embed_requests == 0


def test_invalid_artifact_sha256_rejected(fake: F.FakeArcForge) -> None:
    with pytest.raises(harness.HarnessError):
        run(fake, artifact_sha256="nothex")
    assert fake.connections == 0


# --------------------------------------------------------------------------- #
# complete run: report schema + honesty
# --------------------------------------------------------------------------- #
def test_complete_report_schema_and_honesty(fake: F.FakeArcForge) -> None:
    rep = run(fake)
    assert rep["status"] == "OK" and rep["reason_codes"] == []
    text = json.dumps(rep, allow_nan=False)  # strict JSON: no NaN/inf anywhere
    assert rep["schema_version"] == harness.REPORT_SCHEMA_VERSION
    be = rep["backend"]
    assert be["name"] == "arcforge_http" and be["model_id"] == fake.model_id
    assert be["device_selected"] == "GPU" and be["score_semantics"] == "RAW_SCORE"
    assert be["artifact_sha256"] == "NOT_RECORDED"
    assert be["runtime_version"] == "NOT_RECORDED" and be["driver_version"] == "NOT_RECORDED"
    assert be["calibration_id"] is None
    assert "inventory kit" in rep["runtime_note"]
    sm = rep["arcforge"]["service_model"]
    assert (sm["id"], sm["version"], sm["dim"], sm["device"]) == (
        fake.model_id,
        fake.version,
        16,
        "GPU",
    )
    assert "AS REPORTED" in sm["source"] and "AS REPORTED" in rep["hardware_verification"]
    assert rep["environment"]["python_version"] and rep["environment"]["platform"]
    assert rep["arcforge"]["grid"]["limits"]["max_total_requests"] == 300
    assert rep["arcforge"]["targets"] == [{"label": "host1", "address": None}]
    # honesty block
    joined = " ".join(rep["honesty"])
    for needle in (
        "SYNTHETIC-ONLY",
        "mean nothing about real mail",
        "over the LAN",
        "JSON",
        "UNCALIBRATED",
        "AS REPORTED",
        "NOT a CPU run of the same embedding model",
        "OpenVINO CPU device",
        "open item",
    ):
        assert needle in joined, needle
    assert rep["acceleration_claim"] is False
    assert rep["same_model_cpu_comparison"].startswith("NOT_RUN")
    assert rep["real_world_claim"] is False
    assert "Arc acceleration" not in text  # never claimed anywhere in the report
    assert rep["peak_rss_bytes"] and rep["dataset"]["kind"] == "SYNTHETIC"


def test_grid_cells_have_client_and_server_timing_and_counter_deltas(fake: F.FakeArcForge) -> None:
    rep = run(fake)
    cells = rep["measurements"]
    assert [(c["batch_size"], c["concurrency"]) for c in cells] == [(1, 1), (1, 2), (2, 1), (2, 2)]
    for c in cells:
        assert c["complete"] is True and c["ok"] == 4 and c["errors"] == 0
        lat = c["latency_ms"]
        assert 0 < lat["p50"] <= lat["p95"] <= lat["p99"]
        assert c["requests_per_second"] > 0 and c["items_per_second"] > 0
        assert c["items_per_second"] == pytest.approx(c["throughput_per_second"])
        assert c["server_infer_ms"]["p50"] == pytest.approx(fake.infer_s * 1000.0)
        assert c["server_infer_ms"]["samples"] == 4
        d = c["server_counter_delta"]
        assert d["available"] is True
        assert d["requests_by_status"]["ok"] == 4
        assert d["inference_seconds_count"] == 4
        assert d["inference_seconds_sum"] == pytest.approx(4 * fake.infer_s)
        assert d["items_total"] == 4 * c["batch_size"]
    total = rep["arcforge"]["server_counter_delta_total"]
    assert total["available"] is True
    assert (
        total["requests_by_status"]["ok"]
        == fake.status_counts["ok"]
        == rep["arcforge"]["requests"]["issued"]
    )


def test_quality_compares_head_with_baseline_on_same_split(fake: F.FakeArcForge) -> None:
    rep = run(fake)
    q = rep["quality"]
    assert q["scope"] == "SYNTHETIC_ONLY_NOT_REAL_WORLD" and q["model"] == H.HEAD_MODEL_ID
    base = q["comparison"]["cpu_baseline"]
    assert q["record_count"] == 20 and q["positives"] > 0
    for blk in (q, base):
        assert {"precision", "recall", "pr_auc", "false_positive_rate", "tp", "fp"} <= set(blk)
    assert base["same_held_out_split"] is True
    assert "NOT a CPU run of the embedding model" in base["algorithm"]
    head = rep["arcforge"]["head"]
    assert head["calibrated"] is False and head["score_semantics"] == "RAW_SCORE"
    assert len(head["digest_sha256"]) == 64 and head["dim"] == 16
    assert rep["arcforge"]["quality_status"] == "EVALUATED"
    assert "probability" not in json.dumps(q).replace("not a probability", "")


def test_head_digest_stable_across_runs(fake: F.FakeArcForge) -> None:
    a, b = run(fake), run(fake)
    assert a["arcforge"]["head"]["digest_sha256"] == b["arcforge"]["head"]["digest_sha256"]
    assert a["dataset"]["digest_sha256"] == b["dataset"]["digest_sha256"]


def test_artifact_sha256_flag_is_recorded(fake: F.FakeArcForge) -> None:
    digest = "AB" * 32
    assert run(fake, artifact_sha256=digest)["backend"]["artifact_sha256"] == digest.lower()


def test_old_report_keys_remain_a_subset() -> None:
    old = harness.run_harness("cpu_baseline", train_count=60, test_count=30, batch_sizes=(4,))
    with F.FakeArcForge(dim=16) as f:
        new = run(f)
    assert set(old) <= set(new)
    assert set(old["backend"]) <= set(new["backend"])
    assert old["schema_version"] == new["schema_version"]


# --------------------------------------------------------------------------- #
# privacy: no IPs, no feature text, no identifiers
# --------------------------------------------------------------------------- #
def test_reports_contain_no_ip_unless_flag_set(fake: F.FakeArcForge) -> None:
    text = json.dumps(run(fake))
    assert "127.0.0.1" not in text and str(fake.port) not in text
    shown = run(fake, include_target_addresses=True)
    assert shown["arcforge"]["targets"][0]["address"] == f"127.0.0.1:{fake.port}"


def test_requests_carry_only_model_and_input(fake: F.FakeArcForge) -> None:
    run(fake)
    assert fake.captured_bodies
    for raw in fake.captured_bodies:
        body = json.loads(raw)
        assert set(body) == {"model", "input"}
        assert all(isinstance(t, str) and "@" not in t for t in body["input"])


def _canary_dataset(path: Path) -> None:
    rows = []
    for i in range(10):
        label = Label.MALICIOUS if i % 2 else Label.BENIGN
        rec = FeatureRecord(finding_rule_counts={f"{CANARY}_{i % 3}": 1}, url_count=i)
        rows.append(LabeledExample(features=rec, label=label).model_dump_json())
    path.write_text("\n".join(rows) + "\n")


def test_canary_feature_text_never_in_report_logs_or_errors(
    fake: F.FakeArcForge, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    ds = tmp_path / "owner.jsonl"
    _canary_dataset(ds)
    with caplog.at_level(logging.DEBUG):
        rep = run(fake, dataset_path=ds, attest_deidentified=True)
    assert any(CANARY.encode() in b for b in fake.captured_bodies)  # it WAS legitimately sent
    assert CANARY not in json.dumps(rep) and CANARY not in caplog.text
    assert rep["dataset"]["kind"] == "OWNER_SUPPLIED_UNVERIFIED"
    assert rep["quality"]["scope"] == "OWNER_SUPPLIED_PROVENANCE_UNVERIFIED"


def test_owner_dataset_requires_attestation_and_sends_nothing(
    fake: F.FakeArcForge, tmp_path: Path
) -> None:
    ds = tmp_path / "owner.jsonl"
    _canary_dataset(ds)
    with pytest.raises(harness.DatasetError):
        run(fake, dataset_path=ds, attest_deidentified=False)
    assert fake.connections == 0


# --------------------------------------------------------------------------- #
# safety limits and automatic ABORT
# --------------------------------------------------------------------------- #
def test_per_host_concurrency_cap_never_exceeded(fake: F.FakeArcForge) -> None:
    fake.delay_s = 0.01
    rep = run(fake, config=small_cfg(concurrencies=(4,), batch_sizes=(1,), requests_per_cell=12))
    assert rep["status"] == "OK"
    assert fake.max_concurrent <= C.DEFAULT_PER_HOST_CONCURRENCY
    assert rep["arcforge"]["hosts"][0]["max_outstanding_observed"] <= 2
    assert rep["arcforge"]["requests"]["per_host_concurrency_cap"] == 2


def test_two_hosts_both_used_and_labeled(fake: F.FakeArcForge, fake2: F.FakeArcForge) -> None:
    fake.delay_s = fake2.delay_s = 0.01
    rep = run([fake, fake2], config=small_cfg(concurrencies=(4,), requests_per_cell=12))
    assert rep["status"] == "OK"
    assert [t["label"] for t in rep["arcforge"]["targets"]] == ["host1", "host2"]
    assert fake.embed_requests > 0 and fake2.embed_requests > 0
    assert max(fake.max_concurrent, fake2.max_concurrent) <= 2


def test_abort_on_wedge_between_cells_keeps_partial_results(fake: F.FakeArcForge) -> None:
    def wedge(idx: int) -> None:
        if idx == 0:
            fake.wedged = True
            fake.breaker_reason = "watchdog"

    rep = run(fake, after_cell=wedge)
    assert rep["status"] == "ABORTED" and rep["reason_codes"] == ["ABORT_HOST_WEDGED"]
    assert rep["arcforge"]["partial"] is True and rep["arcforge"]["abort"]["code"].endswith(
        "WEDGED"
    )
    assert len(rep["measurements"]) == 1 and rep["measurements"][0]["complete"] is True
    assert rep["quality"] is not None  # quality finished before the abort and is kept


def test_abort_when_one_of_two_hosts_is_wedged_at_start_sends_no_embeddings(
    fake: F.FakeArcForge, fake2: F.FakeArcForge
) -> None:
    fake2.wedged = True
    rep = run([fake, fake2])
    assert rep["status"] == "ABORTED" and rep["reason_codes"] == ["ABORT_HOST_WEDGED"]
    assert rep["measurements"] == [] and fake.embed_requests == fake2.embed_requests == 0


def test_abort_when_ready_flips_to_not_ready(fake: F.FakeArcForge) -> None:
    def flip(idx: int) -> None:
        fake.ready = False

    rep = run(fake, after_cell=flip)
    assert rep["status"] == "ABORTED" and rep["reason_codes"] == ["ABORT_READY_FLIPPED"]
    assert len(rep["measurements"]) == 1


def test_abort_on_high_error_rate_marks_cell_incomplete(fake: F.FakeArcForge) -> None:
    def break_service(idx: int) -> None:
        fake.embed_fault = "500"

    cfg = small_cfg(concurrencies=(1,), requests_per_cell=8, min_samples_for_error_rate=4)
    rep = run(fake, config=cfg, after_cell=break_service)
    assert rep["status"] == "ABORTED" and rep["reason_codes"] == ["ABORT_ERROR_RATE"]
    first, second = rep["measurements"]
    assert first["complete"] is True and second["complete"] is False
    assert second["errors"] >= 4 and second["error_rate"] > 0.2
    assert second["requests_completed"] < second["requests_planned"]
    assert rep["arcforge"]["partial"] is True


def test_abort_on_p95_ceiling_keeps_the_offending_cell(fake: F.FakeArcForge) -> None:
    fake.delay_s = 0.05
    rep = run(fake, config=small_cfg(p95_ceiling_ms=5.0))
    assert rep["status"] == "ABORTED" and rep["reason_codes"] == ["ABORT_P95_CEILING"]
    assert len(rep["measurements"]) == 1
    assert rep["measurements"][0]["latency_ms"]["p95"] > 5.0
    assert rep["arcforge"]["partial"] is True


def test_warmup_failure_aborts_without_measurements(fake: F.FakeArcForge) -> None:
    fake.embed_fault = "500"
    rep = run(fake)
    assert rep["status"] == "ABORTED" and rep["reason_codes"] == ["ABORT_WARMUP_FAILED"]
    assert rep["measurements"] == [] and rep["quality"] is None


def test_total_request_cap_enforced(fake: F.FakeArcForge) -> None:
    cfg = small_cfg(
        batch_sizes=(1,), concurrencies=(1, 2, 4), requests_per_cell=5, max_total_requests=12
    )
    rep = run(fake, config=cfg, train_count=16, test_count=16)
    assert rep["status"] == "ABORTED" and rep["reason_codes"] == ["ABORT_REQUEST_CAP"]
    assert fake.embed_requests <= 12
    assert rep["arcforge"]["requests"]["issued"] <= 12
    assert len(rep["measurements"]) < 3 and rep["arcforge"]["partial"] is True


def test_quality_skipped_when_cap_cannot_cover_it(fake: F.FakeArcForge) -> None:
    cfg = small_cfg(batch_sizes=(1,), concurrencies=(1,), requests_per_cell=4, max_total_requests=8)
    rep = run(fake, config=cfg, train_count=160, test_count=160)
    assert rep["arcforge"]["quality_status"] == "SKIPPED_REQUEST_CAP"
    assert rep["quality"] is None and rep["status"] == "OK"
    assert fake.embed_requests <= 8


def test_wall_time_cap_enforced_with_injected_clock(fake: F.FakeArcForge) -> None:
    ticks = itertools.count()
    rep = run(
        fake,
        config=small_cfg(max_wall_seconds=40.0),
        clock=lambda: float(next(ticks)),
        sleep=lambda s: None,
    )
    assert rep["status"] == "ABORTED" and rep["reason_codes"] == ["ABORT_WALL_TIME_CAP"]
    assert rep["arcforge"]["partial"] is True
    assert rep["arcforge"]["requests"]["issued"] < 4 * 4 + 6 + 3  # fewer than a full run


def test_rate_limiter_spaces_requests() -> None:
    now = [100.0]
    slept: list[float] = []

    def sleep(s: float) -> None:
        slept.append(s)
        now[0] += s

    limiter = B.RateLimiter(10.0, lambda: now[0], sleep)
    for _ in range(4):
        limiter.acquire()
    assert slept == pytest.approx([0.1, 0.1, 0.1])  # first request is immediate
    assert now[0] == pytest.approx(100.3)


def test_hard_ceilings_reject_oversized_configs() -> None:
    for kw in (
        {"max_total_requests": B.MAX_TOTAL_REQUESTS_CEILING + 1},
        {"max_wall_seconds": B.MAX_WALL_SECONDS_CEILING + 1},
        {"max_requests_per_second": B.MAX_RPS_CEILING + 1},
        {"batch_sizes": (33,)},
        {"batch_sizes": ()},
        {"concurrencies": (B.MAX_CONCURRENCY_CEILING + 1,)},
        {"requests_per_cell": 0},
        {"p95_ceiling_ms": 0},
    ):
        with pytest.raises(harness.HarnessError):
            B.BenchConfig(**kw)
    d = B.BenchConfig()
    assert d.batch_sizes == (1, 8, 16) and d.concurrencies == (1, 2, 4)


# --------------------------------------------------------------------------- #
# CLI (harness.main)
# --------------------------------------------------------------------------- #
def test_cli_without_target_exits_not_available(
    capsys: pytest.CaptureFixture[str], fake: F.FakeArcForge
) -> None:
    code = harness.main(["--backend", "arcforge_http"])
    out = json.loads(capsys.readouterr().out)
    assert code == harness.EXIT_NOT_AVAILABLE and out["status"] == "NOT_AVAILABLE"
    code = harness.main(["--backend", "arcforge_http", "--target", fake.url])
    out = json.loads(capsys.readouterr().out)
    assert code == harness.EXIT_NOT_AVAILABLE
    assert out["reason_codes"] == ["ARCFORGE_ACK_MISSING"]
    assert fake.connections == 0


def test_cli_full_run_writes_report(fake: F.FakeArcForge, tmp_path: Path) -> None:
    out = tmp_path / "r.json"
    argv = [
        "--backend", "arcforge_http",
        "--target", fake.url,
        "--i-understand-service-is-unauthenticated",
        "--batch-sizes", "1,2",
        "--concurrencies", "1,2",
        "--requests-per-cell", "3",
        "--warmup", "1",
        "--train", "40",
        "--test", "20",
        "--max-rps", "100",
        "--output", str(out),
    ]  # fmt: skip
    assert harness.main(argv) == harness.EXIT_OK
    rep = json.loads(out.read_text())
    assert rep["status"] == "OK" and len(rep["measurements"]) == 4
    assert "127.0.0.1" not in out.read_text()


def test_cli_abort_exit_code_and_bad_args(fake: F.FakeArcForge, tmp_path: Path) -> None:
    fake.delay_s = 0.05
    base = [
        "--backend", "arcforge_http", "--target", fake.url,
        "--i-understand-service-is-unauthenticated",
        "--batch-sizes", "1", "--concurrencies", "1", "--requests-per-cell", "3",
        "--warmup", "1", "--train", "16", "--test", "16", "--max-rps", "100",
    ]  # fmt: skip
    out = tmp_path / "a.json"
    assert harness.main([*base, "--p95-ceiling-ms", "5", "--output", str(out)]) == (
        harness.EXIT_ABORTED
    )
    assert json.loads(out.read_text())["status"] == "ABORTED"
    assert harness.main([*base, "--max-total-requests", "999999"]) == harness.EXIT_REJECTED
    assert harness.main([*base, "--artifact-sha256", "zz"]) == harness.EXIT_REJECTED


def test_existing_backends_unchanged_through_main(capsys: pytest.CaptureFixture[str]) -> None:
    assert harness.main(["--backend", "cpu_baseline", "--train", "80", "--test", "40"]) == 0
    assert json.loads(capsys.readouterr().out)["backend"]["name"] == "cpu_baseline"


# --------------------------------------------------------------------------- #
# static: stdlib only, no subprocess, no DNS
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("name", ["arcforge_bench.py", "arcforge_head.py"])
def test_new_modules_import_only_stdlib_and_backend(name: str) -> None:
    tree = ast.parse((PKG_DIR / name).read_text())
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            roots.add(node.module.split(".")[0])
    allowed = set(sys.stdlib_module_names) | {"backend", "__future__"}
    assert roots <= allowed, roots - allowed
    assert not roots & {"subprocess", "os", "requests", "httpx", "urllib3", "numpy"}
    assert "getaddrinfo" not in (PKG_DIR / name).read_text()


# --------------------------------------------------------------------------- #
# regression: server-counter deltas were silently zero against the REAL service
# --------------------------------------------------------------------------- #

#: Literal /metrics lines captured from a live ArcForge host (labels included).
_REAL_METRICS = """\
arcforge_embedding_requests_total{model="qwen3-embedding-0.6b",status="ok"} 206.0
arcforge_embedding_items_total{model="qwen3-embedding-0.6b"} 1160.0
arcforge_embedding_inference_seconds_count{model="qwen3-embedding-0.6b"} 206.0
arcforge_embedding_inference_seconds_sum{model="qwen3-embedding-0.6b"} 92.4379985332489
"""


def test_summarize_reads_real_labeled_series() -> None:
    from backend.mail_security.classifier import arcforge_bench as B
    from backend.mail_security.classifier.arcforge_client import parse_prometheus

    s = B._summarize(parse_prometheus(_REAL_METRICS))
    assert s["inference_seconds_count"] == 206.0
    assert s["items_total"] == 1160.0
    assert abs(s["inference_seconds_sum"] - 92.4379985332489) < 1e-9
    assert s["requests"] == {"ok": 206.0}


def test_summarize_sums_across_label_sets_and_accepts_bare_series() -> None:
    from backend.mail_security.classifier import arcforge_bench as B

    s = B._summarize(
        {
            'arcforge_embedding_items_total{model="a"}': 3.0,
            'arcforge_embedding_items_total{model="b"}': 4.0,
            "arcforge_embedding_inference_seconds_count": 2.0,
            'arcforge_embedding_requests_total{status="ok"}': 5.0,
            'arcforge_embedding_requests_total{model="a",status="error"}': 1.0,
            'arcforge_embedding_requests_total{model="b",status="error"}': 2.0,
            "arcforge_other_total": 99.0,
        }
    )
    assert s["items_total"] == 7.0
    assert s["inference_seconds_count"] == 2.0
    assert s["requests"] == {"ok": 5.0, "error": 3.0}


def test_summarize_ignores_unknown_and_malformed_keys() -> None:
    from backend.mail_security.classifier import arcforge_bench as B

    s = B._summarize({"not a metric": 1.0, 'arcforge_embedding_items_total{': 5.0, "": 2.0})
    assert s["items_total"] == 0.0 and s["requests"] == {}
