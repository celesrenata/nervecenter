"""``arcforge_http`` benchmark backend (NerveShield task 3.3, measurement half).

Measures the EXISTING ArcForge embedding service (through :mod:`arcforge_client`) with an
``arcforge_embed_head`` logistic head on top, against the ``cpu_baseline`` on the SAME
held-out synthetic split. It reuses the harness report schema (extended compatibly under
the ``arcforge`` key) and the harness NOT_AVAILABLE / typed-error conventions.

THE TARGET IS A LIVE, SHARED, UNAUTHENTICATED PLAIN-HTTP SERVICE. Therefore:

* Nothing is sent unless the caller passes explicit ``--target http://IP:PORT`` values
  AND ``--i-understand-service-is-unauthenticated``. Otherwise the backend returns
  ``NOT_AVAILABLE`` with a safe code and attempts ZERO connections.
* Hard safety limits (all configurable downward; hard ceilings are enforced):
  conservative grid (batch 1/8/16 x total concurrency 1/2/4), fixed timed requests per
  cell, separated warmup excluded from statistics, a hard cap on total requests, a hard
  cap on wall time, a global request-rate cap, per-host concurrency never above the
  pool cap, ``/ready`` polled before every cell, and automatic ``ABORTED`` (partial
  results kept and labeled) when a host is wedged, readiness flips, the error rate or
  the p95 ceiling is exceeded, or a cap is reached.
* Only de-identified :class:`FeatureRecord` feature strings are ever sent. The dataset
  is the seeded synthetic set or an owner-attested, already de-identified local JSONL.
  Real private mail must NOT be used before the Phase 6 consent controls exist.
* Reports never contain feature text, response bodies or IP addresses (targets appear
  as ``host1``... unless ``include_target_addresses`` is set).

What this does NOT establish (also written into every report under ``honesty``): quality
numbers are synthetic-only; latency is client wall-clock over the LAN; scores are
uncalibrated; device facts are as reported by the service; ``cpu_baseline`` is a
different algorithm and NOT a CPU run of the embedding model, so no Arc-acceleration
claim is made.

Standard library only. Not wired into the worker, policy or pipeline (I-08).
"""

from __future__ import annotations

import math
import re
import threading
import time
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from backend.mail_security.classifier import arcforge_client as C
from backend.mail_security.classifier.arcforge_head import (
    HEAD_MODEL_ID,
    HEAD_NOTICE,
    train_head,
)
from backend.mail_security.classifier.baseline import SCORE_NOTICE, train_baseline
from backend.mail_security.classifier.features import FeatureRecord, Label, LabeledExample
from backend.mail_security.classifier.harness import (
    _SHA256_RE,
    DEFAULT_THRESHOLD,
    DatasetError,
    HarnessError,
    _base_report,
    _jsonl_dataset,
    _synthetic_dataset,
    peak_rss_bytes,
)
from backend.mail_security.classifier.metrics import (
    confusion_at_threshold,
    percentile,
    pr_auc,
)
from backend.mail_security.classifier.synthetic import generate_examples

BACKEND_NAME = "arcforge_http"
ACK_FLAG = "--i-understand-service-is-unauthenticated"
NOT_RECORDED = "NOT_RECORDED"
DEFAULT_BATCH_SIZES = (1, 8, 16)
DEFAULT_CONCURRENCIES = (1, 2, 4)
DEFAULT_TRAIN_COUNT = 200
DEFAULT_TEST_COUNT = 100

# hard ceilings: configuration cannot raise a limit above these
MAX_TOTAL_REQUESTS_CEILING = 2000
MAX_WALL_SECONDS_CEILING = 3600.0
MAX_RPS_CEILING = 100.0
MAX_CONCURRENCY_CEILING = 8
MAX_REQUESTS_PER_CELL_CEILING = 200

NOT_AVAILABLE_CODES = frozenset(
    {
        "ARCFORGE_TARGET_NOT_PROVIDED",
        "ARCFORGE_ACK_MISSING",
        "ARCFORGE_TARGET_REFUSED",
        "ARCFORGE_NO_HEALTHY_HOST",
    }
)
ABORT_CODES = frozenset(
    {
        "ABORT_HOST_WEDGED",
        "ABORT_READY_FLIPPED",
        "ABORT_READY_CHECK_FAILED",
        "ABORT_HOST_QUARANTINED",
        "ABORT_ERROR_RATE",
        "ABORT_P95_CEILING",
        "ABORT_REQUEST_CAP",
        "ABORT_WALL_TIME_CAP",
        "ABORT_WARMUP_FAILED",
        "ABORT_QUALITY_EMBED_FAILED",
        "ABORT_INTERNAL_ERROR",
    }
)

HW_ARCFORGE = (
    "DEVICE AS REPORTED BY THE SERVICE; NOT INDEPENDENTLY VERIFIED BY THIS CLIENT; "
    "MEASURED OVER THE LAN FROM THE STATED CLIENT"
)
RUNTIME_NOTE = (
    "The client cannot see the OpenVINO runtime or GPU driver versions. Only the host "
    "operator can record them (run the read-only inventory kit, task 3.1, on the Arc host)."
)
HONESTY = (
    "Quality numbers are SYNTHETIC-ONLY (template data, trivially separable classes) and mean "
    "nothing about real mail.",
    "Latency was measured from the stated client over the LAN and includes network transfer and "
    "JSON encoding of the embedding vectors; it is not GPU time. Server-side time is "
    "usage.arcforge.infer_s as reported by the service.",
    "Scores are UNCALIBRATED raw scores; no calibration was performed and no probability is "
    "claimed.",
    "The device and model identity are AS REPORTED by the service (measured on the service's GPU "
    "as reported); this client did not independently verify them.",
    "cpu_baseline is a DIFFERENT algorithm (hashed n-gram logistic regression); it is NOT a CPU "
    "run of the same embedding model. A same-model CPU comparison (OpenVINO CPU device on the "
    "host) is an open item the owner will run by hand. No Arc-acceleration claim is made.",
    "The service is shared, unauthenticated and plain http on the LAN; only de-identified "
    "FeatureRecord strings were sent. Prometheus counter deltas include any other consumer's "
    "traffic during the run.",
    "Throughput is bounded by the configured request-rate cap, concurrency and safety limits; "
    "it is not a capacity measurement.",
)

HUMAN_STEPS = (
    "Owner: run the same-model CPU comparison by hand (OpenVINO CPU device on the Arc host) "
    "before any acceleration claim.",
    "Owner: record the OpenVINO runtime and GPU driver versions on the host and pass the "
    "model artifact digest via --artifact-sha256.",
    "Owner: decide on an authenticated/mTLS endpoint before any non-synthetic data is sent.",
)

_REQ_RE = re.compile(r'^arcforge_embedding_requests_total\{status="([A-Za-z_]+)"\}$')


@dataclass(frozen=True)
class BenchConfig:
    """Benchmark grid and HARD safety limits (validated against hard ceilings)."""

    batch_sizes: tuple[int, ...] = DEFAULT_BATCH_SIZES
    concurrencies: tuple[int, ...] = DEFAULT_CONCURRENCIES
    requests_per_cell: int = 12
    warmup_requests_per_batch_size: int = 2
    max_total_requests: int = 300
    max_wall_seconds: float = 300.0
    max_requests_per_second: float = 20.0
    error_rate_threshold: float = 0.2
    min_samples_for_error_rate: int = 4
    p95_ceiling_ms: float = 2000.0
    quality_batch_size: int = C.DEFAULT_MAX_ITEMS

    def __post_init__(self) -> None:
        if not self.batch_sizes or any(
            (not isinstance(b, int)) or isinstance(b, bool) or not 1 <= b <= C.SERVICE_MAX_BATCH
            for b in self.batch_sizes
        ):
            raise HarnessError("batch sizes must be integers within 1..32")
        if not self.concurrencies or any(
            (not isinstance(c, int)) or isinstance(c, bool) or not 1 <= c <= MAX_CONCURRENCY_CEILING
            for c in self.concurrencies
        ):
            raise HarnessError("concurrencies must be integers within 1..8")
        if not 1 <= self.requests_per_cell <= MAX_REQUESTS_PER_CELL_CEILING:
            raise HarnessError("requests per cell out of range")
        if not 0 <= self.warmup_requests_per_batch_size <= 20:
            raise HarnessError("warmup requests out of range")
        if not 1 <= self.max_total_requests <= MAX_TOTAL_REQUESTS_CEILING:
            raise HarnessError("max total requests out of range")
        if not 0 < self.max_wall_seconds <= MAX_WALL_SECONDS_CEILING:
            raise HarnessError("max wall seconds out of range")
        if not 0 < self.max_requests_per_second <= MAX_RPS_CEILING:
            raise HarnessError("max requests per second out of range")
        if not 0.0 <= self.error_rate_threshold <= 1.0 or self.min_samples_for_error_rate < 1:
            raise HarnessError("error-rate settings out of range")
        if not self.p95_ceiling_ms > 0:
            raise HarnessError("p95 ceiling must be positive")
        if not 1 <= self.quality_batch_size <= C.SERVICE_MAX_BATCH:
            raise HarnessError("quality batch size out of range")

    def as_report(self) -> dict[str, Any]:
        return {
            "batch_sizes": list(self.batch_sizes),
            "concurrencies": list(self.concurrencies),
            "requests_per_cell": self.requests_per_cell,
            "warmup_requests_per_batch_size": self.warmup_requests_per_batch_size,
            "limits": {
                "max_total_requests": self.max_total_requests,
                "max_wall_seconds": self.max_wall_seconds,
                "max_requests_per_second": self.max_requests_per_second,
                "error_rate_threshold": self.error_rate_threshold,
                "min_samples_for_error_rate": self.min_samples_for_error_rate,
                "p95_ceiling_ms": self.p95_ceiling_ms,
            },
        }


class RateLimiter:
    """Global request-rate cap: spaces request slots at least ``1/rps`` seconds apart."""

    def __init__(
        self, rps: float, clock: Callable[[], float], sleep: Callable[[float], None]
    ) -> None:
        self._interval = 1.0 / rps
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._next = 0.0

    def acquire(self) -> None:
        with self._lock:
            now = self._clock()
            slot = max(now, self._next)
            self._next = slot + self._interval
        wait = slot - now
        if wait > 0:
            self._sleep(wait)


# --------------------------------------------------------------------------- #
# Prometheus counter snapshots (server-side view)
# --------------------------------------------------------------------------- #
def _summarize(samples: dict[str, float]) -> dict[str, Any]:
    requests: dict[str, float] = {}
    for key, value in samples.items():
        m = _REQ_RE.match(key)
        if m:
            requests[m.group(1)] = value
    return {
        "inference_seconds_sum": samples.get("arcforge_embedding_inference_seconds_sum", 0.0),
        "inference_seconds_count": samples.get("arcforge_embedding_inference_seconds_count", 0.0),
        "items_total": samples.get("arcforge_embedding_items_total", 0.0),
        "requests": requests,
    }


def _delta(
    before: dict[str, dict[str, Any] | None], after: dict[str, dict[str, Any] | None]
) -> dict[str, Any]:
    scope = "sum over hosts of the service's own counters; includes any other consumer's traffic"
    total_sum = total_count = total_items = 0.0
    by_status: dict[str, float] = {}
    for label, b in before.items():
        a = after.get(label)
        if b is None or a is None:
            return {"available": False, "code": "METRICS_UNAVAILABLE", "scope": scope}
        d_sum = a["inference_seconds_sum"] - b["inference_seconds_sum"]
        d_count = a["inference_seconds_count"] - b["inference_seconds_count"]
        d_items = a["items_total"] - b["items_total"]
        if d_sum < 0 or d_count < 0 or d_items < 0:
            return {"available": False, "code": "COUNTER_RESET", "scope": scope}
        total_sum += d_sum
        total_count += d_count
        total_items += d_items
        for status in set(a["requests"]) | set(b["requests"]):
            d = a["requests"].get(status, 0.0) - b["requests"].get(status, 0.0)
            if d < 0:
                return {"available": False, "code": "COUNTER_RESET", "scope": scope}
            by_status[status] = by_status.get(status, 0.0) + d
    return {
        "available": True,
        "inference_seconds_sum": total_sum,
        "inference_seconds_count": total_count,
        "items_total": total_items,
        "requests_by_status": dict(sorted(by_status.items())),
        "scope": scope,
    }


# --------------------------------------------------------------------------- #
# the run
# --------------------------------------------------------------------------- #
class _Run:
    """One benchmark run against a pool. Thread-safe where workers touch it."""

    def __init__(
        self,
        pool: C.ArcForgePool,
        cfg: BenchConfig,
        clock: Callable[[], float],
        sleep: Callable[[float], None],
    ) -> None:
        self.pool = pool
        self.cfg = cfg
        self.clock = clock
        self.limiter = RateLimiter(cfg.max_requests_per_second, clock, sleep)
        self.lock = threading.Lock()
        self.t0 = clock()
        self.issued = 0
        self.abort: str | None = None

    # ---- limits --------------------------------------------------------- #
    def set_abort(self, code: str) -> None:
        with self.lock:
            if self.abort is None:
                self.abort = code

    def reserve(self) -> bool:
        """Reserve one request against the hard caps. False => aborted/capped."""
        with self.lock:
            if self.abort is not None:
                return False
            if self.issued >= self.cfg.max_total_requests:
                self.abort = "ABORT_REQUEST_CAP"
                return False
            if self.clock() - self.t0 >= self.cfg.max_wall_seconds:
                self.abort = "ABORT_WALL_TIME_CAP"
                return False
            self.issued += 1
        self.limiter.acquire()
        return True

    def remaining_requests(self) -> int:
        with self.lock:
            return self.cfg.max_total_requests - self.issued

    def wall_exceeded(self) -> bool:
        return self.clock() - self.t0 >= self.cfg.max_wall_seconds

    # ---- readiness poll between cells ----------------------------------- #
    def poll(self) -> None:
        for _label, res in self.pool.check_all():
            if res.ok:
                continue
            if res.outcome is C.Outcome.WEDGED:
                self.set_abort("ABORT_HOST_WEDGED")
            elif res.outcome is C.Outcome.NOT_READY:
                self.set_abort("ABORT_READY_FLIPPED")
            elif res.code in ("QUARANTINED", "IDENTITY_MISMATCH"):
                self.set_abort("ABORT_HOST_QUARANTINED")
            else:
                self.set_abort("ABORT_READY_CHECK_FAILED")

    def snapshot(self) -> dict[str, dict[str, Any] | None]:
        out: dict[str, dict[str, Any] | None] = {}
        for t in self.pool.targets():
            res = self.pool.client_for(t.label).metrics()
            out[t.label] = _summarize(res.samples) if res.ok else None
        return out

    # ---- one request ---------------------------------------------------- #
    def one_request(self, records: Sequence[FeatureRecord]) -> tuple[C.EmbedResult, float]:
        t0 = self.clock()
        res = self.pool.embed(records)
        return res, (self.clock() - t0) * 1000.0

    def warmup(self, records: Sequence[FeatureRecord]) -> None:
        for size in self.cfg.batch_sizes:
            for k in range(self.cfg.warmup_requests_per_batch_size):
                if not self.reserve():
                    return
                res, _ = self.one_request(_cyclic(records, k, size))
                if not res.ok:
                    self.set_abort("ABORT_WARMUP_FAILED")
                    return

    def cell(self, records: Sequence[FeatureRecord], size: int, conc: int) -> dict[str, Any]:
        n = self.cfg.requests_per_cell
        cfg = self.cfg
        state = {"taken": 0}
        wall_ms: list[float] = []
        infer_ms: list[float] = []
        outcomes: Counter[str] = Counter()
        attempts_extra = 0
        errors = 0

        def worker() -> None:
            nonlocal attempts_extra, errors
            try:
                while True:
                    with self.lock:
                        if self.abort is not None or state["taken"] >= n:
                            return
                        state["taken"] += 1
                        k = state["taken"] - 1
                    if not self.reserve():
                        return
                    res, ms = self.one_request(_cyclic(records, k, size))
                    with self.lock:
                        outcomes[res.outcome.value] += 1
                        if res.attempts > 1:
                            attempts_extra += 1
                        if res.ok:
                            wall_ms.append(ms)
                            if res.infer_s is not None:
                                infer_ms.append(res.infer_s * 1000.0)
                        else:
                            errors += 1
                        done = sum(outcomes.values())
                        if (
                            self.abort is None
                            and done >= cfg.min_samples_for_error_rate
                            and errors / done > cfg.error_rate_threshold
                        ):
                            self.abort = "ABORT_ERROR_RATE"
            except Exception:  # noqa: BLE001 - never leak error text into reports
                self.set_abort("ABORT_INTERNAL_ERROR")

        before = self.snapshot()
        start = self.clock()
        threads = [threading.Thread(target=worker, daemon=True) for _ in range(conc)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        elapsed = self.clock() - start
        after = self.snapshot()

        done = sum(outcomes.values())
        ok = len(wall_ms)
        p95 = percentile(wall_ms, 95)
        if p95 is not None and p95 > cfg.p95_ceiling_ms:
            self.set_abort("ABORT_P95_CEILING")
        return {
            "batch_size": size,
            "concurrency": conc,
            "complete": done == n,
            "requests_planned": n,
            "requests_completed": done,
            "ok": ok,
            "errors": errors,
            "error_rate": (errors / done) if done else None,
            "retried_requests": attempts_extra,
            "outcomes": dict(sorted(outcomes.items())),
            "record_count": ok * size,
            "batch_count": ok,
            "total_seconds": elapsed,
            "throughput_per_second": (ok * size / elapsed) if elapsed > 0 else None,
            "items_per_second": (ok * size / elapsed) if elapsed > 0 else None,
            "requests_per_second": (ok / elapsed) if elapsed > 0 else None,
            "latency_ms": {
                "scope": "client wall-clock per request over the LAN, including JSON transfer "
                "and pool queue wait",
                "p50": percentile(wall_ms, 50),
                "p95": p95,
                "p99": percentile(wall_ms, 99),
            },
            "server_infer_ms": {
                "scope": "usage.arcforge.infer_s per request, as reported by the service",
                "samples": len(infer_ms),
                "p50": percentile(infer_ms, 50),
                "p95": percentile(infer_ms, 95),
                "p99": percentile(infer_ms, 99),
            },
            "server_counter_delta": _delta(before, after),
        }

    def embed_all(
        self, records: Sequence[FeatureRecord], batch_size: int
    ) -> list[tuple[float, ...]] | None:
        out: list[tuple[float, ...]] = []
        for i in range(0, len(records), batch_size):
            if not self.reserve():
                return None
            res, _ = self.one_request(records[i : i + batch_size])
            if not res.ok:
                self.set_abort("ABORT_QUALITY_EMBED_FAILED")
                return None
            out.extend(res.embeddings)
        return out


def _cyclic(records: Sequence[FeatureRecord], k: int, size: int) -> list[FeatureRecord]:
    return [records[(k * size + j) % len(records)] for j in range(size)]


def _metrics_block(
    labels: Sequence[int], scores: Sequence[float], threshold: float
) -> dict[str, Any]:
    c = confusion_at_threshold(labels, scores, threshold)
    return {
        "tp": c.tp,
        "fp": c.fp,
        "tn": c.tn,
        "fn": c.fn,
        "precision": c.precision,
        "recall": c.recall,
        "false_positive_rate": c.false_positive_rate,
        "pr_auc": pr_auc(labels, scores),
    }


# --------------------------------------------------------------------------- #
# report assembly
# --------------------------------------------------------------------------- #
def _new_report(
    seed: int, cfg: BenchConfig, artifact_sha256: str, labels: Sequence[str] | None = None
) -> dict[str, Any]:
    report = _base_report(BACKEND_NAME, None, seed, cfg.batch_sizes)
    report["hardware_verification"] = HW_ARCFORGE
    report["backend"].update(
        {
            "artifact_sha256": artifact_sha256,
            "runtime_version": NOT_RECORDED,
            "driver_version": NOT_RECORDED,
        }
    )
    report["status"] = "NOT_AVAILABLE"
    report["warmup_runs"] = cfg.warmup_requests_per_batch_size
    report["acceleration_claim"] = False
    report["same_model_cpu_comparison"] = (
        "NOT_RUN: open item; the owner runs the OpenVINO CPU device on the host by hand"
    )
    report["human_steps"] = list(HUMAN_STEPS)
    report["honesty"] = list(HONESTY)
    report["caveats"] = [*report["caveats"], *HONESTY]
    report["runtime_note"] = RUNTIME_NOTE
    report["arcforge"] = {
        "targets": [{"label": lab, "address": None} for lab in (labels or [])],
        "grid": cfg.as_report(),
        "service_model": None,
        "hosts": [],
        "requests": None,
        "abort": None,
        "partial": False,
        "head": None,
        "server_counter_delta_total": None,
        "unauthenticated_service_acknowledged": False,
    }
    return report


def _not_available(report: dict[str, Any], reasons: Sequence[str]) -> dict[str, Any]:
    report["status"] = "NOT_AVAILABLE"
    report["reason_codes"] = sorted(set(reasons))
    report["measurements"] = None
    report["quality"] = None
    return report


def _load_dataset(
    seed: int,
    train_count: int,
    test_count: int,
    dataset_path: Path | None,
    attested: bool,
) -> tuple[list[LabeledExample], list[LabeledExample], dict[str, Any], str]:
    if dataset_path is not None:
        examples, dataset = _jsonl_dataset(dataset_path, attested)
        cut = max(1, (len(examples) * 7) // 10)
        train, test = examples[:cut], examples[cut:]
        dataset["split"] = "first 70% train / last 30% held-out (file order, no shuffle)"
        scope = "OWNER_SUPPLIED_PROVENANCE_UNVERIFIED"
    else:
        test, dataset = _synthetic_dataset(seed, test_count)
        train = generate_examples(seed=seed, count=train_count)
        dataset["train_count"] = len(train)
        scope = "SYNTHETIC_ONLY_NOT_REAL_WORLD"
    if not train or not test:
        raise DatasetError("dataset needs both a training and a held-out split")
    if {e.label for e in train} != {Label.BENIGN, Label.MALICIOUS}:
        raise DatasetError("training split needs both classes")
    return train, test, dataset, scope


def run_arcforge_http(
    *,
    targets: Sequence[str] = (),
    acknowledged: bool = False,
    seed: int = 1,
    train_count: int = DEFAULT_TRAIN_COUNT,
    test_count: int = DEFAULT_TEST_COUNT,
    config: BenchConfig | None = None,
    dataset_path: Path | None = None,
    attest_deidentified: bool = False,
    artifact_sha256: str | None = None,
    include_target_addresses: bool = False,
    threshold: float = DEFAULT_THRESHOLD,
    pool_config: C.PoolConfig | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    pool_clock: Callable[[], float] | None = None,
    pool_sleep: Callable[[float], None] | None = None,
    after_cell: Callable[[int], None] | None = None,
) -> dict[str, Any]:
    """Run the guarded benchmark and return a JSON-serializable report.

    ``after_cell`` is a test seam called with the cell index after each cell.
    """
    cfg = config or BenchConfig()
    if artifact_sha256 is not None and not _SHA256_RE.match(artifact_sha256):
        raise HarnessError("artifact sha256 must be 64 hex characters")
    recorded = artifact_sha256.lower() if artifact_sha256 else NOT_RECORDED
    report = _new_report(seed, cfg, recorded)
    report["arcforge"]["unauthenticated_service_acknowledged"] = bool(acknowledged)

    # Gate 1: nothing is contacted, parsed against the network or even loaded without both.
    reasons: list[str] = []
    if not targets:
        reasons.append("ARCFORGE_TARGET_NOT_PROVIDED")
    if not acknowledged:
        reasons.append("ARCFORGE_ACK_MISSING")
    if reasons:
        return _not_available(report, reasons)

    train, test, dataset, scope = _load_dataset(
        seed, train_count, test_count, dataset_path, attest_deidentified
    )
    report["dataset"] = dataset

    pool_cfg = pool_config or C.PoolConfig()
    try:
        pool = C.ArcForgePool(
            list(targets),
            pool_cfg,
            clock=pool_clock or time.monotonic,
            sleep=pool_sleep or time.sleep,
        )
    except C.TargetError:
        return _not_available(report, ["ARCFORGE_TARGET_REFUSED"])
    if len(pool.labels) > 8:
        raise HarnessError("too many targets")
    report["arcforge"]["targets"] = [
        {
            "label": t.label,
            "address": (f"{t.host}:{t.port}" if include_target_addresses else None),
        }
        for t in pool.targets()
    ]

    run = _Run(pool, cfg, clock, sleep)
    run.poll()  # gating poll: /ready + /v1/models for every host, nothing else is sent
    ident = pool.identity
    healthy = [s for s in pool.status() if s.state is C.HostState.CLOSED]
    report["arcforge"]["hosts"] = _hosts_block(pool)
    if ident is None or not healthy:
        return _not_available(report, ["ARCFORGE_NO_HEALTHY_HOST"])
    report["arcforge"]["service_model"] = {
        "id": ident.model_id,
        "version": ident.version,
        "dim": ident.dim,
        "device": ident.device,
        "source": "AS REPORTED BY THE SERVICE (GET /v1/models); not independently verified",
    }
    report["backend"].update(
        {"device_selected": ident.device, "model_id": ident.model_id, "version": ident.version}
    )

    measurements: list[dict[str, Any]] = []
    report["measurements"] = measurements
    report["quality"] = None
    report["arcforge"]["quality_status"] = "NOT_RUN"
    counters_before = run.snapshot()

    if run.abort is None:
        run.warmup([e.features for e in test])
    if run.abort is None:
        _run_quality(run, pool, train, test, seed, scope, threshold, report)
    cells_planned = len(cfg.batch_sizes) * len(cfg.concurrencies)
    bench_records = [e.features for e in test]
    index = 0
    for size in cfg.batch_sizes:
        for conc in cfg.concurrencies:
            if run.abort is None:
                run.poll()
            if run.abort is None and run.wall_exceeded():
                run.set_abort("ABORT_WALL_TIME_CAP")
            if run.abort is None and run.remaining_requests() < cfg.requests_per_cell:
                run.set_abort("ABORT_REQUEST_CAP")
            if run.abort is not None:
                break
            measurements.append(run.cell(bench_records, size, conc))
            if after_cell is not None:
                after_cell(index)
            index += 1
        if run.abort is not None:
            break

    counters_after = run.snapshot()
    report["arcforge"]["server_counter_delta_total"] = _delta(counters_before, counters_after)
    report["arcforge"]["hosts"] = _hosts_block(pool)
    report["arcforge"]["requests"] = {
        "issued": run.issued,
        "cap": cfg.max_total_requests,
        "wall_seconds": run.clock() - run.t0,
        "wall_cap_seconds": cfg.max_wall_seconds,
        "cells_planned": cells_planned,
        "cells_started": len(measurements),
        "per_host_concurrency_cap": pool_cfg.per_host_concurrency,
    }
    report["peak_rss_bytes"] = peak_rss_bytes()
    if run.abort is not None:
        report["status"] = "ABORTED"
        report["reason_codes"] = [run.abort]
        report["arcforge"]["partial"] = True
        report["arcforge"]["abort"] = {
            "code": run.abort,
            "note": "run stopped by a safety limit; partial results are kept and labeled",
        }
    else:
        report["status"] = "OK"
    return report


def _hosts_block(pool: C.ArcForgePool) -> list[dict[str, Any]]:
    return [
        {
            "label": s.label,
            "state": s.state.value,
            "last_code": s.last_code,
            "max_outstanding_observed": s.max_outstanding,
            "consecutive_failures": s.consecutive_failures,
        }
        for s in pool.status()
    ]


def _run_quality(
    run: _Run,
    pool: C.ArcForgePool,
    train: list[LabeledExample],
    test: list[LabeledExample],
    seed: int,
    scope: str,
    threshold: float,
    report: dict[str, Any],
) -> None:
    cfg = run.cfg
    qbs = cfg.quality_batch_size
    needed = math.ceil(len(train) / qbs) + math.ceil(len(test) / qbs)
    if run.remaining_requests() < needed + cfg.requests_per_cell:
        report["arcforge"]["quality_status"] = "SKIPPED_REQUEST_CAP"
        return
    ident = pool.identity
    assert ident is not None
    train_emb = run.embed_all([e.features for e in train], qbs)
    test_emb = run.embed_all([e.features for e in test], qbs) if train_emb is not None else None
    if train_emb is None or test_emb is None:
        report["arcforge"]["quality_status"] = "NOT_EVALUATED"
        return
    train_labels = [1 if e.label is Label.MALICIOUS else 0 for e in train]
    test_labels = [1 if e.label is Label.MALICIOUS else 0 for e in test]
    head = train_head(
        train_emb,
        train_labels,
        seed,
        embedder_model_id=ident.model_id,
        embedder_version=ident.version,
    )
    head_scores = [p.raw_score for p in head.predict_batch(test_emb)]
    baseline = train_baseline(train, seed)
    base_scores = [p.raw_score for p in baseline.predict_batch([e.features for e in test])]
    head_block = _metrics_block(test_labels, head_scores, threshold)
    base_block = _metrics_block(test_labels, base_scores, threshold)
    report["arcforge"]["quality_status"] = "EVALUATED"
    report["arcforge"]["head"] = {
        "model_id": HEAD_MODEL_ID,
        "digest_sha256": head.digest,
        "dim": head.dim,
        "score_semantics": "RAW_SCORE",
        "calibrated": False,
        "score_notice": HEAD_NOTICE,
        "embedder": {"model_id": ident.model_id, "version": ident.version},
    }
    report["quality"] = {
        "scope": scope,
        "model": HEAD_MODEL_ID,
        "threshold": threshold,
        "threshold_note": "fixed a priori on the raw score; not tuned",
        "positives": sum(test_labels),
        "record_count": len(test_labels),
        "train_count": len(train),
        **head_block,
        "comparison": {
            "cpu_baseline": {
                "model_id": "nervshield.cpu_baseline.v1",
                "model_digest_sha256": baseline.digest,
                "algorithm": "hashed n-gram logistic regression; a DIFFERENT algorithm, NOT "
                "a CPU run of the embedding model",
                "score_notice": SCORE_NOTICE,
                "same_held_out_split": True,
                **base_block,
            }
        },
    }


# --------------------------------------------------------------------------- #
# CLI glue (called from harness.main; harness imports this module lazily)
# --------------------------------------------------------------------------- #
def run_from_args(args: Any) -> dict[str, Any]:
    """Build the run from parsed harness arguments (see ``harness.main``)."""
    defaults = BenchConfig()
    cfg = BenchConfig(
        batch_sizes=_ints(args.batch_sizes) if args.batch_sizes else defaults.batch_sizes,
        concurrencies=_ints(args.concurrencies) if args.concurrencies else defaults.concurrencies,
        requests_per_cell=_pick(args.requests_per_cell, defaults.requests_per_cell),
        warmup_requests_per_batch_size=_pick(args.warmup, defaults.warmup_requests_per_batch_size),
        max_total_requests=_pick(args.max_total_requests, defaults.max_total_requests),
        max_wall_seconds=_pick(args.max_wall_seconds, defaults.max_wall_seconds),
        max_requests_per_second=_pick(args.max_rps, defaults.max_requests_per_second),
        error_rate_threshold=_pick(args.error_rate_threshold, defaults.error_rate_threshold),
        p95_ceiling_ms=_pick(args.p95_ceiling_ms, defaults.p95_ceiling_ms),
    )
    return run_arcforge_http(
        targets=list(args.target or []),
        acknowledged=bool(args.i_understand_service_is_unauthenticated),
        seed=args.seed,
        train_count=_pick(args.train, DEFAULT_TRAIN_COUNT),
        test_count=_pick(args.test, DEFAULT_TEST_COUNT),
        config=cfg,
        dataset_path=args.dataset_jsonl,
        attest_deidentified=args.attest_deidentified,
        artifact_sha256=args.artifact_sha256,
        include_target_addresses=bool(args.include_target_addresses),
    )


def _ints(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in text.split(","))


def _pick(value: Any, default: Any) -> Any:
    return default if value is None else value
