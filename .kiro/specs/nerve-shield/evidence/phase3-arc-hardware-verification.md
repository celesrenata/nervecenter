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

Interpretation: the embedding model runs on the real Arc PF and returns correct 1024-dim L2-normalized vectors under concurrency; latency is dominated by transfer, not GPU compute. A capacity benchmark should run ON a host (loopback) to remove LAN/JSON overhead, and the p95 ceiling should be raised for a deliberate stress run.

## Fix applied during verification
The client's target validator accepted `https://` though the service is plain HTTP; an https target could only fail to connect. Tightened to `http`-only at construction (and the test that asserted https-accepted now asserts it is refused). Full suite 6346 passed / 0 failed.

## Open (G3 not fully closeable here)
- **Same-model CPU comparison** (OpenVINO CPU device on a host) — NOT RUN; owner runs it by hand per the ArcForge README's transient-unit method. Needed for a real Arc-vs-CPU acceleration claim.
- **On-host (loopback) benchmark** to measure capacity without LAN/JSON overhead, and a stress run with a raised p95 ceiling.
- **Calibration** of the embed-head scores on real labeled data (currently UNCALIBRATED; synthetic quality is meaningless for real mail).
- **Authentication** for the ArcForge/Granite endpoints before any private-content inference is routed to them; and the OmniRoute `hybrid/reader` resolution (Phase 4) to prove local-only routing.
- Runtime/driver/artifact digests in the report are `NOT_RECORDED` (the client cannot see them; a host-side probe can).
- Nothing here is wired into the assessment pipeline; the embed-head is not a security verdict (I-08).
