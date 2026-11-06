"""Portable benchmark harness (NerveShield task 3.3, HARNESS HALF ONLY).

Task 3.3 itself is NOT done: it needs measurements on the real GTi14 / Intel Arc
host, which this code has never touched. The ``openvino`` backend is UNTESTED ON
HARDWARE; it was written without being run and is labelled as such in every
report.

Run: ``python -m backend.mail_security.classifier.harness --backend cpu_baseline``

Rules the harness enforces
--------------------------
* Standard library only. ``openvino`` is imported lazily, by name, inside the
  OpenVINO backend; there is no module-level import of it.
* Backends: ``cpu_baseline`` (Deliverable B, always available) and ``openvino``.
  A missing module, device, artifact or encoder yields status ``NOT_AVAILABLE``
  with safe reason codes, ``measurements``/``quality`` set to ``null``, and NEVER
  numbers, and NEVER the CPU result in its place.
* Artifacts are pinned by an owner-supplied expected sha256 (T-27). A mismatch
  raises :class:`ArtifactDigestMismatch` before any backend is prepared or run.
* Synthetic quality is never presented as real-world (:class:`RealWorldClaimError`).
* Owner-supplied datasets must be local JSONL of already de-identified
  ``LabeledExample`` records and require ``--attest-deidentified``. Real private
  mail must NOT be used until the Phase 6 consent controls exist.
* No raw mail content, network, subprocess or GPU/LLM calls. Not wired into the
  worker, policy, API or startup (I-08).

CPU fallback semantics (for later tasks, NOT built here)
--------------------------------------------------------
Fallback to CPU must be explicit and never silent: a caller that asked for
``openvino`` and cannot get it receives a typed unavailable result, and any CPU
scoring that follows must be requested by name, reported under the
``cpu_baseline`` identity (its own model id and artifact digest), and be
distinguishable from accelerator output in every response and metric.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import platform
import re
import resource
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Protocol

from pydantic import ValidationError

from backend.mail_security.classifier.baseline import SCORE_NOTICE, train_baseline
from backend.mail_security.classifier.features import FeatureRecord, Label, LabeledExample
from backend.mail_security.classifier.metrics import (
    confusion_at_threshold,
    percentile,
    pr_auc,
)
from backend.mail_security.classifier.synthetic import generate_examples

REPORT_SCHEMA_VERSION = "nervshield.bench_report.v1"
DEFAULT_THRESHOLD = 0.0
DEFAULT_BATCH_SIZES = (1, 8, 32)
DEFAULT_WARMUP_RUNS = 2
_HELD_OUT_SEED_OFFSET = 1_000_003
MODEL_ID_CPU = "nervshield.cpu_baseline.v1"
CATEGORY = "mail_threat_raw_score"
HW_UNTESTED = "UNTESTED ON HARDWARE"
HW_CPU_THIS_HOST = "MEASURED ON THIS HOST ONLY; NOT GTi14, NOT ARC"

EXIT_OK = 0
EXIT_NOT_AVAILABLE = 2
EXIT_REJECTED = 3
EXIT_ABORTED = 4

REASON_CODES = frozenset(
    {
        "OPENVINO_MODULE_MISSING",
        "OPENVINO_DEVICE_MISSING",
        "MODEL_ARTIFACT_NOT_PROVIDED",
        "MODEL_ARTIFACT_FILE_MISSING",
        "OPENVINO_ENCODER_NOT_PROVIDED",
        "OPENVINO_RUNTIME_ERROR",
    }
)

#: What a human must do on a GTi14 before the openvino backend can yield numbers.
HUMAN_STEPS = (
    "Run the read-only inventory kit (task 3.1) and record versions/drivers.",
    "Owner decision: which libraries may be installed and pinned "
    "(openvino, optionally OVMS/onnxruntime).",
    "Owner decision: which model families to compare and where the artifact comes from.",
    "Convert/export the chosen model on the host; record its sha256 and pin it "
    "via --artifact-sha256.",
    "Write a feature encoder for the chosen model and pass it to OpenVinoBackend (Python API).",
    "Run: python -m backend.mail_security.classifier.harness --backend openvino "
    "--artifact PATH --artifact-sha256 HEX --device GPU --output report.json",
    "Expected report shape: status OK, backend.device_selected from the OpenVINO device list, "
    "measurements per batch size, cold_start separate, quality scope SYNTHETIC unless an "
    "attested owner dataset is used.",
    "Run the same harness with --backend cpu_baseline on the same host and compare; "
    "a worse accelerator path is not deployed (EXP-02).",
)

_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")


class HarnessError(Exception):
    """Typed harness failure. Messages never carry paths, digests or content."""

    code = "HARNESS_ERROR"

    def __init__(self, message: str = "") -> None:
        super().__init__(message or self.code)


class ArtifactDigestMismatch(HarnessError):
    code = "ARTIFACT_DIGEST_MISMATCH"


class ArtifactNotPinned(HarnessError):
    code = "ARTIFACT_NOT_PINNED"


class DatasetError(HarnessError):
    code = "DATASET_REJECTED"


class RealWorldClaimError(HarnessError):
    code = "REAL_WORLD_CLAIM_REFUSED"


class BackendNotAvailable(Exception):
    """Internal: backend cannot run; carries only safe reason codes."""

    def __init__(self, reasons: Sequence[str]) -> None:
        super().__init__(",".join(reasons))
        self.reasons = sorted(set(reasons))


# --------------------------------------------------------------------------- #
# artifact pinning
# --------------------------------------------------------------------------- #
def verify_artifact(path: Path, expected_sha256: str | None) -> str:
    """Hash ``path`` and require it to equal the owner-pinned digest (T-27)."""
    if not expected_sha256 or not _SHA256_RE.match(expected_sha256):
        raise ArtifactNotPinned("an expected sha256 (64 hex chars) must be supplied")
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            hasher.update(chunk)
    actual = hasher.hexdigest()
    if actual != expected_sha256.lower():
        raise ArtifactDigestMismatch("artifact digest does not match the pinned value")
    return actual


# --------------------------------------------------------------------------- #
# backends
# --------------------------------------------------------------------------- #
class Backend(Protocol):
    name: str

    def prepare(self) -> dict[str, Any]: ...

    def score_batch(self, records: Sequence[FeatureRecord]) -> list[float]: ...


class CpuBaselineBackend:
    """Deliverable B logistic regression, trained on SYNTHETIC data."""

    name = "cpu_baseline"

    def __init__(self, seed: int, train_count: int, expected_sha256: str | None = None) -> None:
        self._seed = seed
        self._train_count = train_count
        self._expected = expected_sha256
        self._model: Any = None

    def prepare(self) -> dict[str, Any]:
        model = train_baseline(
            generate_examples(seed=self._seed, count=self._train_count), self._seed
        )
        if self._expected is not None:
            if not _SHA256_RE.match(self._expected) or model.digest != self._expected.lower():
                raise ArtifactDigestMismatch("model digest does not match the pinned value")
        self._model = model
        return {
            "device_selected": "cpu",
            "model_id": MODEL_ID_CPU,
            "artifact_sha256": model.digest,
            "runtime_version": f"python-{platform.python_version()} (stdlib)",
            "driver_version": None,
            "hardware_verification": HW_CPU_THIS_HOST,
        }

    def score_batch(self, records: Sequence[FeatureRecord]) -> list[float]:
        return [p.raw_score for p in self._model.predict_batch(records)]


class OpenVinoBackend:
    """OpenVINO contract. UNTESTED ON HARDWARE: written, never executed.

    Needs an owner-pinned artifact and a model-specific ``encoder`` (records ->
    model input). Output convention assumed (unverified): the first output
    holds one raw score per record. No model is chosen, converted or
    downloaded here.
    """

    name = "openvino"

    def __init__(
        self,
        artifact_path: Path | None,
        device: str = "GPU",
        encoder: Callable[[Sequence[FeatureRecord]], Any] | None = None,
    ) -> None:
        self._artifact_path = artifact_path
        self._device = device
        self._encoder = encoder
        self._compiled: Any = None

    def prepare(self) -> dict[str, Any]:
        reasons: list[str] = []
        if self._artifact_path is None:
            reasons.append("MODEL_ARTIFACT_NOT_PROVIDED")
        elif not self._artifact_path.is_file():
            reasons.append("MODEL_ARTIFACT_FILE_MISSING")
        try:
            ov = importlib.import_module("openvino")
        except ImportError:
            reasons.append("OPENVINO_MODULE_MISSING")
            raise BackendNotAvailable(reasons) from None
        core: Any = None
        try:
            core = ov.Core()
            if self._device not in list(core.available_devices):
                reasons.append("OPENVINO_DEVICE_MISSING")
        except Exception:  # noqa: BLE001 - never leak runtime error text
            reasons.append("OPENVINO_RUNTIME_ERROR")
        if self._encoder is None:
            reasons.append("OPENVINO_ENCODER_NOT_PROVIDED")
        if reasons:
            raise BackendNotAvailable(reasons)
        try:
            model = core.read_model(str(self._artifact_path))
            self._compiled = core.compile_model(model, self._device)
            driver = _optional_property(core, self._device, "DRIVER_VERSION")
            full_name = _optional_property(core, self._device, "FULL_DEVICE_NAME")
        except Exception:  # noqa: BLE001
            raise BackendNotAvailable(["OPENVINO_RUNTIME_ERROR"]) from None
        return {
            "device_selected": self._device,
            "device_name": full_name,
            "model_id": self._artifact_path.name if self._artifact_path else None,
            "runtime_version": str(getattr(ov, "__version__", "UNKNOWN")),
            "driver_version": driver,
            "hardware_verification": HW_UNTESTED,
        }

    def score_batch(self, records: Sequence[FeatureRecord]) -> list[float]:
        assert self._encoder is not None  # prepare() guarantees it
        result = self._compiled(self._encoder(records))
        first = next(iter(result.values()))
        return [float(x) for x in first.reshape(-1).tolist()]


def _optional_property(core: Any, device: str, name: str) -> str | None:
    try:
        return str(core.get_property(device, name))
    except Exception:  # noqa: BLE001
        return None


# --------------------------------------------------------------------------- #
# dataset
# --------------------------------------------------------------------------- #
def _synthetic_dataset(seed: int, count: int) -> tuple[list[LabeledExample], dict[str, Any]]:
    examples = generate_examples(seed=seed + _HELD_OUT_SEED_OFFSET, count=count)
    digest = hashlib.sha256("\n".join(e.model_dump_json() for e in examples).encode()).hexdigest()
    return examples, {
        "kind": "SYNTHETIC",
        "record_count": len(examples),
        "digest_sha256": digest,
        "seed": seed + _HELD_OUT_SEED_OFFSET,
    }


def _jsonl_dataset(path: Path, attested: bool) -> tuple[list[LabeledExample], dict[str, Any]]:
    if not attested:
        raise DatasetError(
            "owner datasets need --attest-deidentified (already de-identified, consented; "
            "no real private mail before Phase 6)"
        )
    try:
        raw = path.read_bytes()
    except OSError:
        raise DatasetError("dataset file unreadable") from None
    examples: list[LabeledExample] = []
    for number, line in enumerate(raw.decode("utf-8", errors="replace").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            examples.append(LabeledExample.model_validate_json(line))
        except ValidationError:
            raise DatasetError(f"line {number} is not a valid LabeledExample") from None
    if not examples:
        raise DatasetError("dataset has no records")
    return examples, {
        "kind": "OWNER_SUPPLIED_UNVERIFIED",
        "record_count": len(examples),
        "digest_sha256": hashlib.sha256(raw).hexdigest(),
        "seed": None,
    }


# --------------------------------------------------------------------------- #
# measurement
# --------------------------------------------------------------------------- #
def peak_rss_bytes() -> int:
    """Process high-water resident memory (includes dataset and training)."""
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(peak if sys.platform == "darwin" else peak * 1024)


def _chunks(items: Sequence[FeatureRecord], size: int) -> list[Sequence[FeatureRecord]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def _measure(
    backend: Backend,
    records: Sequence[FeatureRecord],
    size: int,
    warmup_runs: int,
    clock: Callable[[], float],
) -> dict[str, Any]:
    batches = _chunks(records, size)
    for i in range(warmup_runs):  # untimed
        backend.score_batch(batches[i % len(batches)])
    durations: list[float] = []
    start = clock()
    for batch in batches:
        t0 = clock()
        backend.score_batch(batch)
        durations.append((clock() - t0) * 1000.0)
    total = clock() - start
    return {
        "batch_size": size,
        "record_count": len(records),
        "batch_count": len(batches),
        "total_seconds": total,
        "throughput_per_second": (len(records) / total) if total > 0 else None,
        "latency_ms": {
            "scope": "per batch call",
            "p50": percentile(durations, 50),
            "p95": percentile(durations, 95),
            "p99": percentile(durations, 99),
        },
    }


def _environment() -> dict[str, Any]:
    return {
        "python_version": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "processor": platform.processor() or None,
    }


def _base_report(
    name: str, device: str | None, seed: int, batch_sizes: Sequence[int]
) -> dict[str, Any]:
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "status": "OK",
        "reason_codes": [],
        "backend": {
            "name": name,
            "device_requested": device,
            "device_selected": None,
            "model_id": None,
            "artifact_sha256": None,
            "runtime_version": None,
            "driver_version": None,
            "category": CATEGORY,
            "calibration_id": None,
            "score_semantics": "RAW_SCORE",
        },
        "hardware_verification": HW_UNTESTED,
        "environment": _environment(),
        "seed": seed,
        "dataset": None,
        "batch_sizes": list(batch_sizes),
        "warmup_runs": None,
        "cold_start": None,
        "measurements": None,
        "peak_rss_bytes": None,
        "quality": None,
        "real_world_claim": False,
        "caveats": [
            f"Scores: {SCORE_NOTICE}",
            "Synthetic quality is not real-world detection quality; no real-world claim is made.",
            "Timings describe only the host that ran them; peak RSS is a process-wide "
            "high-water mark including dataset and training.",
            "Classifier output is never an authority (I-08).",
        ],
    }


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #
def run_harness(
    backend_name: str,
    *,
    seed: int = 1,
    train_count: int = 2000,
    test_count: int = 1000,
    batch_sizes: Sequence[int] = DEFAULT_BATCH_SIZES,
    warmup_runs: int = DEFAULT_WARMUP_RUNS,
    threshold: float = DEFAULT_THRESHOLD,
    dataset_path: Path | None = None,
    attest_deidentified: bool = False,
    artifact_path: Path | None = None,
    expected_artifact_sha256: str | None = None,
    device: str = "GPU",
    claim_real_world: bool = False,
    backend_override: Backend | None = None,
    clock: Callable[[], float] = time.perf_counter,
) -> dict[str, Any]:
    """Run one backend over a dataset and return a JSON-serializable report."""
    if backend_name not in ("cpu_baseline", "openvino"):
        raise HarnessError("unknown backend")
    if not batch_sizes or any((not isinstance(b, int)) or b < 1 for b in batch_sizes):
        raise HarnessError("batch sizes must be positive integers")
    if warmup_runs < 0:
        raise HarnessError("warmup runs must be >= 0")

    if dataset_path is not None:
        examples, dataset = _jsonl_dataset(dataset_path, attest_deidentified)
        scope = "OWNER_SUPPLIED_PROVENANCE_UNVERIFIED"
    else:
        examples, dataset = _synthetic_dataset(seed, test_count)
        scope = "SYNTHETIC_ONLY_NOT_REAL_WORLD"
        if claim_real_world:
            raise RealWorldClaimError("synthetic data cannot support a real-world claim")
    if not examples:
        raise DatasetError("dataset has no records")

    # Pin check happens before any backend is prepared or run (T-27).
    if backend_name == "openvino" and artifact_path is not None and artifact_path.is_file():
        verify_artifact(artifact_path, expected_artifact_sha256)

    device_requested = device if backend_name == "openvino" else "cpu"
    report = _base_report(backend_name, device_requested, seed, batch_sizes)
    report["dataset"] = dataset
    backend: Backend
    if backend_override is not None:
        backend = backend_override
    elif backend_name == "cpu_baseline":
        backend = CpuBaselineBackend(seed, train_count, expected_artifact_sha256)
    else:
        backend = OpenVinoBackend(artifact_path, device)

    report["human_steps"] = list(HUMAN_STEPS) if backend_name == "openvino" else []
    setup_start = clock()
    try:
        identity = backend.prepare()
    except BackendNotAvailable as exc:
        report["status"] = "NOT_AVAILABLE"
        report["reason_codes"] = [r for r in exc.reasons if r in REASON_CODES]
        return report
    setup_seconds = clock() - setup_start

    report["hardware_verification"] = identity.pop("hardware_verification", HW_UNTESTED)
    report["backend"].update(
        {k: v for k, v in identity.items() if k in report["backend"] or k == "device_name"}
    )
    report["backend"]["artifact_sha256"] = (
        verify_artifact(artifact_path, expected_artifact_sha256)
        if backend_name == "openvino" and artifact_path is not None
        else identity.get("artifact_sha256")
    )

    records = [e.features for e in examples]
    labels = [1 if e.label is Label.MALICIOUS else 0 for e in examples]
    first = _chunks(records, min(batch_sizes))[0]
    t0 = clock()
    backend.score_batch(first)
    cold_ms = (clock() - t0) * 1000.0
    report["cold_start"] = {
        "latency_ms": cold_ms,
        "batch_size": len(first),
        "setup_seconds": setup_seconds,
        "note": "first scoring call after prepare(); excluded from warm measurements",
    }
    report["warmup_runs"] = warmup_runs
    report["measurements"] = [
        _measure(backend, records, size, warmup_runs, clock) for size in batch_sizes
    ]
    scores = backend.score_batch(records)
    c = confusion_at_threshold(labels, scores, threshold)
    report["quality"] = {
        "scope": scope,
        "threshold": threshold,
        "threshold_note": "fixed a priori on the raw score; not tuned",
        "positives": sum(labels),
        "record_count": len(labels),
        "tp": c.tp,
        "fp": c.fp,
        "tn": c.tn,
        "fn": c.fn,
        "precision": c.precision,
        "recall": c.recall,
        "false_positive_rate": c.false_positive_rate,
        "pr_auc": pr_auc(labels, scores),
    }
    report["peak_rss_bytes"] = peak_rss_bytes()
    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="NerveShield classifier benchmark harness")
    parser.add_argument(
        "--backend", choices=("cpu_baseline", "openvino", "arcforge_http"), required=True
    )
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--train", type=int)
    parser.add_argument("--test", type=int)
    parser.add_argument("--batch-sizes")
    parser.add_argument("--warmup", type=int)
    parser.add_argument("--dataset-jsonl", type=Path)
    parser.add_argument("--attest-deidentified", action="store_true")
    parser.add_argument("--artifact", type=Path)
    parser.add_argument("--artifact-sha256")
    parser.add_argument("--device", default="GPU")
    parser.add_argument("--output", type=Path)
    # arcforge_http only (ignored by the other backends). The service is a live, shared,
    # UNAUTHENTICATED plain-http LAN service: nothing is sent without both of these.
    parser.add_argument("--target", action="append", help="http://IP:PORT, repeatable")
    parser.add_argument("--i-understand-service-is-unauthenticated", action="store_true")
    parser.add_argument("--include-target-addresses", action="store_true")
    parser.add_argument("--concurrencies")
    parser.add_argument("--requests-per-cell", type=int)
    parser.add_argument("--max-total-requests", type=int)
    parser.add_argument("--max-wall-seconds", type=float)
    parser.add_argument("--max-rps", type=float)
    parser.add_argument("--p95-ceiling-ms", type=float)
    parser.add_argument("--error-rate-threshold", type=float)
    args = parser.parse_args(argv)
    try:
        if args.backend == "arcforge_http":
            from backend.mail_security.classifier import arcforge_bench

            report = arcforge_bench.run_from_args(args)
        else:
            report = run_harness(
                args.backend,
                seed=args.seed,
                train_count=2000 if args.train is None else args.train,
                test_count=1000 if args.test is None else args.test,
                batch_sizes=(
                    tuple(int(b) for b in args.batch_sizes.split(","))
                    if args.batch_sizes
                    else DEFAULT_BATCH_SIZES
                ),
                warmup_runs=DEFAULT_WARMUP_RUNS if args.warmup is None else args.warmup,
                dataset_path=args.dataset_jsonl,
                attest_deidentified=args.attest_deidentified,
                artifact_path=args.artifact,
                expected_artifact_sha256=args.artifact_sha256,
                device=args.device,
            )
    except (HarnessError, ValueError) as exc:
        code = exc.code if isinstance(exc, HarnessError) else "INVALID_ARGUMENT"
        sys.stderr.write(f"REJECTED {code}\n")
        return EXIT_REJECTED
    text = json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n"
    if args.output is not None:
        args.output.write_text(text)
    else:
        sys.stdout.write(text)
    if report["status"] == "OK":
        return EXIT_OK
    return EXIT_ABORTED if report["status"] == "ABORTED" else EXIT_NOT_AVAILABLE


if __name__ == "__main__":
    raise SystemExit(main())
