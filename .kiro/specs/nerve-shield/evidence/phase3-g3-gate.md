# Phase 3 — G3 Gate summary (owner-facing) — STOP, decision requested

**Status: STOPPED AT G3. Phase 4 has NOT been started and will not start without an explicit owner decision recorded below.**
**Gate text (requirements.md):** "At least one real small classifier benchmarked on an Arc iGPU; CPU fallback verified; exact hardware/runtime evidence recorded; negative effects on privacy and routing tested."
**Legend:** VERIFIED = observed on the real hosts during orchestrator review. PARTIAL = part of the criterion is met. NOT MET = not done.

## Verdict in one paragraph
A real GPU execution on the Intel Arc iGPUs is demonstrated and measured, with a same-model CPU comparison and a numeric equivalence check. But the gate's wording has four parts and **only the hardware/runtime-evidence part is substantially met**. The "classifier" is an embedding model on the GPU plus a pure-Python linear head on CPU trained on synthetic data; "CPU fallback verified" and "privacy and routing tested" are not met. I would not call G3 passed. The measured GPU path itself is solid.

## Checklist
| Criterion | Status | Evidence |
| --- | --- | --- |
| Real GPU execution on an Arc iGPU | **VERIFIED** | Inventory on all four GTi14 hosts (Arc PF `0000:00:02.0`, `i915`, GuC). ArcForge serves Qwen3-Embedding-0.6B int8 on the PF; benchmark ran 180 timed requests on gremlin-4 with 0 errors; service reports `device GPU`, `wedged false`. |
| "Small classifier" benchmarked on Arc | **PARTIAL** | The GPU-resident stage is the embedding model, not a purpose-built classifier. The classifier head (logistic regression) is CPU, pure Python, trained on synthetic data. Quality numbers are synthetic-only and meaningless for real mail. |
| Throughput, latency, memory vs a baseline | **PARTIAL** | Measured GPU latency/throughput (client wall, server time, Prometheus counters agree within 5%). Same-model CPU: 3.2x to 5.4x slower than GPU (8 CPU threads, contended). Memory footprint of the service not measured. |
| CPU comparison | **VERIFIED (one host)** | gremlin-4, identical texts and batch windows, cosine similarity CPU vs GPU >= 0.9994, all vectors finite and unit norm. |
| CPU fallback verified | **NOT MET** | ArcForge has none by design. The NerveShield client returns "not evaluated" on any failure (fake-server tests only); not exercised against a failed real GPU. A documented CPU path now has measured numbers if wanted. |
| Exact hardware/runtime evidence | **PARTIAL, mostly met** | Hardware ids, kernel 7.2.15, OpenVINO 2026.4.0, Python 3.14.8, media driver 26.3.5, identical model artifact sha256 on all four hosts. Missing: intel-compute-runtime / level-zero / IGC versions. |
| Privacy and routing effects tested | **NOT MET** | Only synthetic strings were sent. The ArcForge (8100) and Granite (2701, gremlin-1) endpoints are unauthenticated plain HTTP on `0.0.0.0`. OmniRoute `hybrid/reader` resolution is unproven. |
| Calibration/abstention (task 3.6) | **NOT DONE** | Needs real labeled data; scores are UNCALIBRATED. |
| Wired into assessments | **NO (by design)** | Nothing calls the client from the worker/policy; the embed head is evidence-only at most (I-08). |

## Key findings
1. **GPU is 3 to 5 times faster than 8 contended CPU threads for this model; numerics match.** Absolute throughput is about 14 to 21 items/s per host for short inputs.
2. **Aggregate throughput does not rise with concurrency** on gremlin-4 for short inputs: the per-infer time itself doubles when two requests run at once, so the GPU is the bottleneck. This differs from the ArcForge README's validation note (gremlin-1, input mix unstated).
3. **All four hosts serve identical model artifacts** (sha256 recorded), and run the same kernel and media driver, which supports pinning the artifact hashes (T-27).
4. **Defects found by running on real hardware** (all fixed and committed): labeled Prometheus series read as zero by the harness (the fake server hid it); `https` targets accepted; an incorrect earlier claim that the classifier package is stdlib-only; and an earlier interpretation that latency was transfer-dominated (corrected: only the Mac-to-LAN gap was).

## What I did on the hosts (complete list)
Read-only commands over SSH (hostname, service state, listeners, `/ready`, `/metrics`, file hashes, driver package names), the inventory script, a few small embedding requests, one guarded benchmark pod pinned to gremlin-4 (deleted), one sandboxed transient CPU unit on gremlin-4 (self-removed). **No NixOS config, service, firewall or package was changed.**

## Risks to decide on before any private content goes near these services
- Unauthenticated, LAN-wide endpoints (ArcForge 8100 on all four hosts; Granite 2701 on gremlin-1). Any host on the LAN can query them; for mail content that is a confidentiality exposure even though it is inside your network.
- Phase 4 puts message-derived text in front of an LLM. That needs the fail-closed local-only route guard (task 4.2) and a decision on how those endpoints are authenticated.

## Recommended next step (for your decision, not a plan I am executing)
Before Phase 4, decide how the inference endpoints are protected (a NixOS change on the hosts: bind to the cluster interface only and/or add a shared-secret or mTLS front). Phase 4 can still proceed on fixtures first (schema-locked observer, injection suite, fail-closed resolver) without sending real mail anywhere.

## Owner decision (to be completed by owner)
```
G3 decision:  APPROVE Phase 4 (fixture-first, no real mail to any model) | REQUEST CHANGES | HOLD
Reviewer / date:
Decision on endpoint authentication (none / bind-to-cluster-only / shared secret / mTLS):
Notes / conditions:
```
