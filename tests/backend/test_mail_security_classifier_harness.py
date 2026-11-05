"""NerveShield task 3.3 (harness half only) -- portable benchmark harness tests.

Honesty: these prove the harness code and its refusal paths. Nothing here ran
OpenVINO, an Intel Arc GPU or a GTi14; task 3.3 stays open until a human takes
real hardware measurements.
"""

from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path

import pytest

from backend.mail_security.classifier import harness, synthetic

PKG_DIR = Path(harness.__file__).resolve().parent

_REQUIRED_TOP = {
    "schema_version", "status", "reason_codes", "backend", "hardware_verification",
    "environment", "seed", "dataset", "batch_sizes", "cold_start", "warmup_runs",
    "measurements", "peak_rss_bytes", "quality", "real_world_claim", "caveats",
}  # fmt: skip
_REQUIRED_BACKEND = {
    "name", "device_requested", "device_selected", "model_id", "artifact_sha256",
    "runtime_version", "driver_version", "category", "calibration_id", "score_semantics",
}  # fmt: skip


def _small_cpu_report(**kwargs: object) -> dict:
    args: dict = {"seed": 3, "train_count": 200, "test_count": 60, "batch_sizes": (1, 8)}
    args.update(kwargs)
    return harness.run_harness("cpu_baseline", **args)


def test_cpu_report_schema_and_json_roundtrip() -> None:
    report = _small_cpu_report()
    assert _REQUIRED_TOP <= set(report)
    assert _REQUIRED_BACKEND <= set(report["backend"])
    assert report["status"] == "OK"
    assert report["reason_codes"] == []
    assert report["backend"]["name"] == "cpu_baseline"
    assert report["backend"]["device_selected"] == "cpu"
    assert report["backend"]["calibration_id"] is None
    assert report["backend"]["score_semantics"] == "RAW_SCORE"
    assert len(report["backend"]["artifact_sha256"]) == 64
    env = report["environment"]
    assert {"python_version", "platform", "machine"} <= set(env)
    assert report["seed"] == 3
    assert report["batch_sizes"] == [1, 8]
    assert len(report["dataset"]["digest_sha256"]) == 64
    assert report["dataset"]["kind"] == "SYNTHETIC"
    assert report["cold_start"]["latency_ms"] is not None
    assert report["warmup_runs"] >= 0
    assert [m["batch_size"] for m in report["measurements"]] == [1, 8]
    for m in report["measurements"]:
        assert {"latency_ms", "throughput_per_second", "record_count"} <= set(m)
        assert {"p50", "p95", "p99"} <= set(m["latency_ms"])
    assert report["peak_rss_bytes"] > 0
    q = report["quality"]
    assert {"precision", "recall", "false_positive_rate", "pr_auc", "threshold"} <= set(q)
    assert q["scope"] == "SYNTHETIC_ONLY_NOT_REAL_WORLD"
    assert json.loads(json.dumps(report, allow_nan=False)) == report


def test_cpu_report_is_deterministic_where_not_timing() -> None:
    a = _small_cpu_report()
    b = _small_cpu_report()
    assert a["backend"]["artifact_sha256"] == b["backend"]["artifact_sha256"]
    assert a["dataset"] == b["dataset"]
    assert a["quality"] == b["quality"]


def test_openvino_not_available_here_and_never_numbers() -> None:
    try:
        import openvino  # type: ignore  # noqa: F401
    except ImportError:
        pass
    else:  # pragma: no cover - only on a host where the owner installed it
        pytest.skip("openvino installed; this test pins the no-module behaviour")
    report = harness.run_harness("openvino", seed=1, train_count=50, test_count=20)
    assert report["status"] == "NOT_AVAILABLE"
    assert "OPENVINO_MODULE_MISSING" in report["reason_codes"]
    assert "MODEL_ARTIFACT_NOT_PROVIDED" in report["reason_codes"]
    assert report["measurements"] is None
    assert report["quality"] is None
    assert report["peak_rss_bytes"] is None
    assert report["cold_start"] is None
    assert report["backend"]["name"] == "openvino"
    assert report["backend"]["device_selected"] is None
    assert report["backend"]["runtime_version"] is None
    assert report["hardware_verification"] == "UNTESTED ON HARDWARE"
    assert report["human_steps"]
    # no CPU substitution: no identity or digest is filled in from the CPU path
    assert report["backend"]["model_id"] is None
    assert report["backend"]["artifact_sha256"] is None
    assert set(report["reason_codes"]) <= harness.REASON_CODES


def test_openvino_missing_artifact_file_is_not_available(tmp_path: Path) -> None:
    report = harness.run_harness(
        "openvino",
        artifact_path=tmp_path / "absent.onnx",
        expected_artifact_sha256="0" * 64,
    )
    assert report["status"] == "NOT_AVAILABLE"
    assert "MODEL_ARTIFACT_FILE_MISSING" in report["reason_codes"]
    assert report["measurements"] is None


def test_digest_mismatch_rejected_and_nothing_runs(tmp_path: Path) -> None:
    artifact = tmp_path / "model.onnx"
    artifact.write_bytes(b"not a real model")

    class Spy:
        calls: list[str] = []

        name = "spy"

        def prepare(self, *a: object, **k: object) -> None:
            self.calls.append("prepare")

        def score_batch(self, *a: object, **k: object) -> None:
            self.calls.append("score")

    spy = Spy()
    with pytest.raises(harness.ArtifactDigestMismatch) as info:
        harness.run_harness(
            "openvino",
            artifact_path=artifact,
            expected_artifact_sha256="a" * 64,
            backend_override=spy,
        )
    assert spy.calls == []
    assert info.value.code == "ARTIFACT_DIGEST_MISMATCH"
    assert isinstance(info.value, harness.HarnessError)
    # the message carries no digest values or paths
    assert "a" * 64 not in str(info.value)
    assert str(tmp_path) not in str(info.value)


def test_artifact_without_pin_is_rejected(tmp_path: Path) -> None:
    artifact = tmp_path / "model.onnx"
    artifact.write_bytes(b"x")
    with pytest.raises(harness.ArtifactNotPinned):
        harness.run_harness("openvino", artifact_path=artifact)
    with pytest.raises(harness.ArtifactNotPinned):
        harness.run_harness("openvino", artifact_path=artifact, expected_artifact_sha256="not-hex")


def test_matching_digest_passes_verification(tmp_path: Path) -> None:
    artifact = tmp_path / "model.onnx"
    artifact.write_bytes(b"bytes")
    digest = hashlib.sha256(b"bytes").hexdigest()
    assert harness.verify_artifact(artifact, digest.upper()) == digest


def test_cpu_baseline_pin_mismatch_rejected() -> None:
    with pytest.raises(harness.ArtifactDigestMismatch):
        _small_cpu_report(expected_artifact_sha256="b" * 64)


def test_cpu_baseline_pin_match_runs() -> None:
    digest = _small_cpu_report()["backend"]["artifact_sha256"]
    assert _small_cpu_report(expected_artifact_sha256=digest)["status"] == "OK"


def test_synthetic_quality_cannot_be_called_real_world() -> None:
    with pytest.raises(harness.RealWorldClaimError):
        _small_cpu_report(claim_real_world=True)
    report = _small_cpu_report()
    assert report["real_world_claim"] is False
    assert "real-world" in " ".join(report["caveats"]).lower()


def test_owner_dataset_requires_attestation_and_is_never_real_world(tmp_path: Path) -> None:
    examples = synthetic.generate_examples(seed=99, count=30)
    path = tmp_path / "held_out.jsonl"
    path.write_text("\n".join(e.model_dump_json() for e in examples) + "\n")
    with pytest.raises(harness.DatasetError):
        _small_cpu_report(dataset_path=path)
    report = _small_cpu_report(dataset_path=path, attest_deidentified=True)
    assert report["dataset"]["kind"] == "OWNER_SUPPLIED_UNVERIFIED"
    assert report["dataset"]["record_count"] == 30
    assert report["dataset"]["digest_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert report["quality"]["scope"] == "OWNER_SUPPLIED_PROVENANCE_UNVERIFIED"
    assert report["real_world_claim"] is False


def test_bad_dataset_line_error_leaks_no_content(tmp_path: Path) -> None:
    path = tmp_path / "bad.jsonl"
    path.write_text('{"features": {"url_count": 1}, "label": "BENIGN", "secret": "a@b.example"}\n')
    with pytest.raises(harness.DatasetError) as info:
        _small_cpu_report(dataset_path=path, attest_deidentified=True)
    assert "a@b.example" not in str(info.value)
    assert "line 1" in str(info.value)


def test_unknown_backend_and_bad_batch_sizes() -> None:
    with pytest.raises(harness.HarnessError):
        harness.run_harness("tensorrt")
    with pytest.raises(harness.HarnessError):
        _small_cpu_report(batch_sizes=(0,))


def test_cli_cpu_and_openvino(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "r.json"
    rc = harness.main(
        ["--backend", "cpu_baseline", "--train", "100", "--test", "40",
         "--batch-sizes", "1,4", "--output", str(out)]
    )  # fmt: skip
    assert rc == 0
    assert json.loads(out.read_text())["status"] == "OK"
    rc = harness.main(["--backend", "openvino"])
    assert rc == harness.EXIT_NOT_AVAILABLE
    assert json.loads(capsys.readouterr().out)["status"] == "NOT_AVAILABLE"
    rc = harness.main(["--backend", "openvino", "--artifact", str(out),
                       "--artifact-sha256", "c" * 64])  # fmt: skip
    assert rc == harness.EXIT_REJECTED


# --------------------------------------------------------------------------- #
# static checks
# --------------------------------------------------------------------------- #
_FORBIDDEN = {
    "socket", "ssl", "subprocess", "urllib", "http", "requests", "httpx", "aiohttp",
    "ftplib", "smtplib", "asyncio", "multiprocessing", "ctypes", "numpy", "scipy",
    "sklearn", "torch", "onnx", "onnxruntime", "openvino", "psutil", "pandas", "os",
    "shutil", "tempfile",
}  # fmt: skip


def test_harness_imports_no_network_subprocess_or_third_party() -> None:
    import sys

    path = PKG_DIR / "harness.py"
    tree = ast.parse(path.read_text())
    tops: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            tops.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            tops.add(node.module.split(".")[0])
    assert not tops & _FORBIDDEN, tops & _FORBIDDEN
    for top in tops - {"__future__"}:
        assert top in sys.stdlib_module_names or top in {"backend", "pydantic"}, top


def test_openvino_only_reachable_through_lazy_import_module() -> None:
    text = (PKG_DIR / "harness.py").read_text()
    assert "import_module(" in text  # lazy, inside the backend only
    tree = ast.parse(text)
    for node in tree.body:  # module-level: no openvino import statement
        assert not (
            isinstance(node, ast.Import | ast.ImportFrom) and "openvino" in ast.unparse(node)
        )


def test_harness_not_wired_into_runtime() -> None:
    root = PKG_DIR.parents[1]
    offenders = [
        str(p)
        for p in root.rglob("*.py")
        if PKG_DIR not in p.parents and "classifier.harness" in p.read_text()
    ]
    assert offenders == []
