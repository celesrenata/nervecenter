# Phase 3 — Intel Arc hardware verification (tasks 3.1, 3.3, 3.5)

**Date:** 2026-11-06. **Hosts:** gremlin-1..4 (10.1.1.12..15), BeeLink GTi14, Core Ultra 9 185H, 96 GiB, Intel Arc iGPU `8086:7d55`, NixOS 26.11. All four run the cluster's existing **ArcForge** host-native PF inference service (not built by NerveShield). This is a REAL GPU execution, satisfying the G3 gate's core requirement; the remaining G3 checklist items are listed as open below.

## 3.1 Inventory (read-only, VERIFIED on all four hosts)
Ran `tools/nerve_shield/inventory_arc_host.sh` over SSH on each host; redacted JSON saved under `evidence/phase3-inventory/`. Key facts (identical across hosts unless noted):
- Arc iGPU present: PF `0000:00:02.0` + 7 SR-IOV VFs (`i915`, `enable_guc=3`, `max_vfs=7`, `force_probe=7d55`, `xe` blacklisted). SR-IOV is the live config, matching ADR-007's note that VF use was the fragile path; ArcForge deliberately serves on the **PF** instead.
- `/dev/dri` present; `libze_intel_gpu` / `libze_loader` resolve under the NixOS graphics-drivers store path; `iHD` VA-API driver loads (VA-API 1.24).
- k3s v1.36.5; Intel GPU device plugin sockets present (`gpu.intel.com-i915.sock`).
- gremlin-1 additionally has an RTX 4070 Ti Super (`10de:2689`) running Granite 4.2 (`services.granite4-rtx`, port 2701).
- Probe gap: root PATH has no `python3`, so OpenVINO/onnxruntime version probes are UNAVAILABLE in the inventory; the versions are inside the ArcForge service's own env (not read).

## ArcForge service (observed, read-only)
- `arcforge-embeddings` + `arcforge-rerank` active on all four; embeddings on port **8100**, plain HTTP, **no authentication**, bound `0.0.0.0` (LAN-reachable). Model `qwen3-embedding-0.6b` int8, 1024-dim, LAST pooling, L2-normalized, 4096-token ceiling, 4 GPU worker streams, 60 s infer timeout, circuit breaker + systemd watchdog, no CPU fallback (fails loudly). `/ready`, `/v1/models`, `/v1/embeddings`, `/metrics` confirmed. Model compile ~3.8 s; `arcforge_device_wedged=0`.
- Security note recorded for NerveShield: this endpoint (and Granite:2701) are unauthenticated on the LAN. The NerveShield client therefore refuses non-private-IP targets, requires an explicit `--i-understand-service-is-unauthenticated` flag, and sends only de-identified FeatureRecord strings. It is NOT wired into the assessment pipeline (NS-ADR-027).

## 3.5 / 3.3 Client + benchmark against real hardware (VERIFIED)
Built `arcforge_client` (client + health + circuit breaker + pool) and the `arcforge_http` benchmark backend, tested against a local fake server (160+ tests). Then ran the guarded benchmark from this Mac against **gremlin-4** only (`--target http://10.1.1.15:8100`), conservative defaults (batch 1/8, concurrency 1/2/4, 12 requests/cell, 20 rps cap, 300 s / 300 req caps, p95 ceiling 2000 ms). `/ready` was ok before and after; 0 errors in every cell; the service stayed healthy.

Measured (client wall-clock over the LAN from macOS / server GPU time as reported by the service):
| batch | conc | wall p50 | wall p95 | server p50 | items/s |
|--|--|--|--|--|--|
| 1 | 1 | 102 ms | 182 ms | 66 ms | 8.9 |
| 1 | 2 | 164 ms | 256 ms | 90 ms | 10.8 |
| 1 | 4 | 193 ms | 858 ms | 100 ms | 11.3 |
| 8 | 1 | 513 ms | 625 ms | 405 ms | 15.1 |
| 8 | 2 | 848 ms | 1118 ms | 755 ms | 17.7 |
| 8 | 4 | 1060 ms | **3843 ms** | 807 ms | 16.8 |

**Run status: ABORTED on `ABORT_P95_CEILING`** at batch 8 / concurrency 4 (wall p95 3843 ms > 2000 ms). This is the safety limit working as designed, not a fault: 0 errors, device never wedged. The wall-vs-server gap is LAN round-trip + JSON transfer of 1024-float vectors from a macOS client; server-side GPU time stayed ≤ ~807 ms. Throughput (~17 items/s) is bounded by the rate cap and is NOT a capacity measurement.

Interpretation (as first written, **partly WRONG, corrected below**): "latency is dominated by transfer, not GPU compute." Only the Mac-vs-server gap was transfer. The on-node run below shows GPU compute time is the dominant and throughput-limiting term.

## On-node capacity run (VERIFIED, gremlin-4, 2026-11-09)
The package is not stdlib-only in practice (`features.py` needs pydantic; `backend.mail_security.__init__` pulls in the worker, SQLAlchemy and structlog), so it cannot run under a host's Nix Python without installing packages. Instead the real guarded harness ran as a one-off pod (`api` image `bench-b2d66ac`, non-root, caps dropped, cpu limit 1, memory limit 512Mi) **pinned to gremlin-4**, targeting that node's own address, then was deleted. Grid: batch 1/8/16 x concurrency 1/2, 30 requests per cell after warmup, rate cap 50 rps, p95 ceiling 3000 ms. Result: **status OK, 0 errors in 180 timed requests, no abort**, `/ready` ok afterwards, no IPs in the report.

| batch | conc | wall p50 | wall p95 | server p50 | server p95 | items/s | Prometheus mean infer ms/request |
|--|--|--|--|--|--|--|--|
| 1 | 1 | 70 | 98 | 67 | 95 | 14.4 | 65 |
| 1 | 2 | 115 | 170 | 110 | 167 | 15.1 | 117 |
| 8 | 1 | 447 | 569 | 430 | 556 | 17.7 | 437 |
| 8 | 2 | 862 | 1078 | 842 | 1066 | 18.0 | 859 |
| 16 | 1 | 970 | 1031 | 945 | 1009 | 16.9 | 916 |
| 16 | 2 | 1840 | 1918 | 1804 | 1880 | 17.4 | 1796 |

The two server-side measurement paths (`usage.arcforge.infer_s` per request vs the service's own Prometheus counters) agree within about 5 percent, and counters match the work sent (30 requests per cell, items = 30 x batch size).

Findings (observations on ONE host with SHORT synthetic feature strings; not a capacity guarantee):
- Wall time is within about 3 ms of server time on-node, so the earlier Mac-measured gap was LAN plus JSON transfer.
- **Aggregate throughput is flat at roughly 14-18 items/s.** Batching gives about 20 percent over batch 1 and plateaus at 16. Doubling concurrency doubles per-request server time and adds no throughput: GPU work appears effectively serialized for this input shape (about 55-65 ms of GPU time per item).
- This differs from the ArcForge README's validation note that aggregate throughput scales with concurrency (measured on gremlin-1, input mix not stated). Possible reasons not separated here: short inputs, different host, service worker configuration, my pool's per-host cap of 2.
- The first on-node attempt (concurrency 4) aborted on the p95 ceiling because my pool caps each host at 2 in-flight requests, so two of four requests wait client-side; that cell measured queueing, not the GPU. The grid above avoids it.
- Planning arithmetic only (NOT measured): about 17 items/s per host. A backlog of the 72 current messages is a few seconds of GPU time; real message text is longer than these strings and will cost more.

## Defects found by this run and fixed
1. **Server-counter deltas were silently zero** against the real service: it labels every series (`{model="..."}`) and the harness looked up bare names. The fake server emitted bare names, which hid it. Fixed in `b2d66ac` (sum across label sets; fake now emits the real format; regression tests use the literal captured `/metrics` text).
2. The target validator accepted `https://` (fixed earlier).
3. The earlier claim that the classifier package is "standard library only" was wrong about third-party imports (pydantic, and the package `__init__` imports); only the network/subprocess static check held.

## Unrelated failing test (pre-existing, not touched)
`tests/backend/test_routines.py::TestWellnessSleepSeam::test_returns_iso_records_with_source` hard-codes Oct 25-27 2026 sleep records and queries a 14-day window against the wall clock; it began failing when the clock passed Nov 8. Full suite: 6348 passed, 1 failed (this one).

## Fix applied during verification
The client's target validator accepted `https://` though the service is plain HTTP; an https target could only fail to connect. Tightened to `http`-only at construction (and the test that asserted https-accepted now asserts it is refused). Full suite 6346 passed / 0 failed.

## Same-model CPU vs Arc GPU (VERIFIED, gremlin-4, 2026-11-09)
**Method.** A single script was piped over SSH into `systemd-run --pipe` so nothing was written to the host. Containment: CPU plugin only (`DevicePolicy=closed` blocks the GPU), `PrivateNetwork=true`, `ProtectSystem=strict`, `NoNewPrivileges`, no capabilities, `MemoryMax=8G`, `CPUQuota=800%`, `Nice=15`, idle I/O, `RuntimeMaxSec=300`. A smoke run (compile plus one inference) validated the sandbox first. The script replicates the service's tokenization (right-pad to the batch maximum), input order, LAST-token pooling and L2 normalization, with 8 CPU threads. The GPU side sent the **same 32 synthetic texts** (75 to 216 tokens each, median 125) in the **same batch windows and repetition counts** to the live service one request at a time, using the server-side `infer_s`, which covers the same stages (tokenize, infer, pool). No units or files were left on the host; the service stayed `GPU / ready / not wedged` throughout.

| batch | CPU 8 threads total p50 | CPU p95 | CPU items/s | Arc GPU p50 | Arc GPU p95 | GPU items/s | GPU speedup | reps cpu/gpu |
|--|--|--|--|--|--|--|--|--|
| 1 | 208 ms | 413 ms | 4.5 | 65 ms | 114 ms | 15.4 | 3.2x | 12/12 |
| 8 | 2494 ms | 2624 ms | 3.6 | 461 ms | 624 ms | 17.4 | 5.4x | 6/6 |
| 16 | 3638 ms | 4793 ms | 3.9 | 747 ms | 921 ms | 21.4 | 4.9x | 5/5 |

CPU time is essentially all inference (tokenize about 1 ms, pool about 0.2 ms).

**Numerics.** Cosine similarity of CPU vs GPU embeddings for the same text: batch 1 minimum 0.99954 (n=4), batch 16 minimum 0.99941 (n=16). For context, the INT8 model's own batch-size sensitivity is 0.99986 on CPU and 0.99956 on GPU (batch 1 vs batch 16). All vectors finite and unit norm. The GPU path is numerically equivalent to CPU within normal INT8 variation.

**Caveats (read before quoting the speedup).**
- The CPU was deliberately constrained (8 threads, low priority, CPU quota, shared production node). The supported claim is "3 to 5 times faster than 8 contended CPU threads"; the CPU best case (all 22 threads, normal priority) was not measured and could narrow the gap.
- One host, 5 to 12 timed repetitions per cell (p95 from 5 samples is weak), short synthetic inputs, one model. Energy was not measured.
- GPU absolute numbers vary with text length: batch 16 was 21.4 items/s on these texts and about 17 items/s on the harness's different sample.
- Runtime now recorded: **OpenVINO 2026.4.0**, Python 3.14.8 (the service's own environment).

## Artifact identity (VERIFIED, read-only, all four hosts)
Identical on gremlin-1, -2, -3 and -4:
- `openvino_model.xml` sha256 `fc0218b8973d09f43e7f8175760b69d4f2a9c9bab6e354aca8cee639d9b57e15`
- `openvino_model.bin` (616,316,780 bytes) sha256 `b88066b1898f5e74eb31fe87578f10ab3d01709765a09bd8c4ea4fe256f62bb4`
- `tokenizer.json` sha256 `def76fb086971c7867b829c23a26261e38d9d74e02139253b38aeb9df8b4b50a`
- `config.json` sha256 `68f2df7c4bea23d67b321a89503429303aedaef24a3fd646ce14e5c355047a43`
Kernel 7.2.15 and intel-media-driver / vpl-gpu-rt 26.3.5 on all four. The intel-compute-runtime, level-zero and IGC package versions were NOT captured (not found in the store references queried). Supply-chain note (T-27): these hashes can be pinned in NerveShield config so a changed artifact is rejected.

## Open (G3 not fully closeable here)
- **CPU fallback on a failed GPU is not exercised on hardware.** ArcForge fails loudly by design; the NerveShield client treats any non-OK outcome as "not evaluated" (tested against the fake server only). The same-model CPU path now has measured numbers (above), so a documented CPU fallback is a viable option if the owner wants one.
- **Capacity under the service's 4 workers**: aggregate throughput was flat with concurrency on gremlin-4 for short inputs (see on-node run); not explained here.
- **Calibration** of the embed-head scores on real labeled data (UNCALIBRATED; synthetic quality is meaningless for real mail). The head is pure Python on CPU; only the embedding runs on the Arc GPU.
- **Privacy and routing**: the ArcForge/Granite endpoints are unauthenticated plain HTTP on the LAN. No private content was sent. Local-only routing through OmniRoute (`hybrid/reader`) is unproven (Phase 4).
- compute-runtime / level-zero / IGC versions not recorded.
- Nothing here is wired into the assessment pipeline; the embed-head is not a security verdict (I-08).
