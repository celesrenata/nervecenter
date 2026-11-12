# Phase 6: gap closure after 6.3 / 6.4 (2026-11-11)

Owner request: "close the gaps". Decision NS-ADR-051. Everything uses the public corpus; no owner mail.

## 1. Deployed parser bug (NS-ADR-048) — closed
Redeployed `api` and `web` at `e671255` with `deployments/harbor/deploy.sh` (`SKIP_DOCKER_LOGIN=1`). Before applying, the rendered kustomize was diffed against the cluster: only image tags changed, and the mail-security flags (`NERVE_MAIL_SECURITY_ENABLED=1`, `..._API_SCOPE_MODE=single_tenant`) are in the repo ConfigMap. After the rollout, the live pod parses `Message-ID: <>` as `MALFORMED_HEADER` instead of raising; flags intact. `mcp` unchanged (`fc0bb30`).

## 2. Unreliable thresholds (val held 17 spam) — closed
VAL is now 1 in 6 thread/campaign groups spread across the training period (group-disjoint from FIT) instead of the latest 15% by time: 445 messages, 199 spam. Report: `phase6-6.3-benchmark-v2.json`.

| Candidate | ROC AUC [95% CI] | Recall @1% FPR | Precision / recall at val threshold | FPR | ECE |
| --- | --- | --- | --- | --- | --- |
| A n-gram baseline | 0.985 [0.977, 0.991] | 0.81 | 0.78 / 0.92 | 3.3% | 0.065 |
| B DistilRoBERTa | 0.996 [0.993, 0.998] | 0.94 | 0.94 / 0.91 | 0.8% | 0.031 |
| C Qwen3-0.6B LoRA | 0.997 [0.994, 0.999] | 0.97 | 0.97 / 0.92 | 0.3% | 0.018 |
| D frozen Arc embedding + head | 0.981 [0.973, 0.988] | 0.72 | 0.74 / 0.92 | 4.2% | 0.054 |

Paired differences vs A: B and C better on all three metrics (CIs above 0); D not distinguishable.
**Correction to the first 6.3 run:** "B is not distinguishable from the baseline" was an artifact of that split (FIT ended earlier and VAL was tiny); with this split B is clearly better than A. The earlier low recall at the chosen thresholds was also a VAL artifact.

## 3. One seed — closed
Seeds 0, 1, 2 for B and C: ROC AUC B 0.9946 ± 0.0013, C 0.9973 ± 0.0012; recall @1% FPR B 0.911 ± 0.032, C 0.966 ± 0.009. Ranking is stable across seeds; C is less seed-sensitive than B on the operating metric.

## 4. Not run on the target runtime — closed for B and C
`backend/owner_tools/arc_export.py` retrains seed-0 B and C, merges the LoRA, converts to OpenVINO IR (fp16) on the Mac (network blocked in-process; `openvino==2026.4.0` added to the pinned `ml` extra), and pre-tokenises the eval set. `backend/owner_tools/arc_host_run.py` ran on gremlin-4 in a sandboxed `systemd-run` (only `/dev/dri/renderD128`, no network, read-only system, 16 GB cap, low priority), using the host's ArcForge OpenVINO 2026.4.0 environment. Device: the Arc physical function (GPU.0, shared with ArcForge/OVMS); the SR-IOV VFs are allocated to a Plex worker and a VM and were not touched. Temp files deleted; ArcForge stayed ready/not wedged. Evidence: `phase6-arc-runtime.json`.

| | Latency batch 1 p50 / p95 | Throughput batch 16 | AUC Arc vs CPU fp32 | Sign agreement | Max logit diff |
| --- | --- | --- | --- | --- | --- |
| B | 6.9 / 7.4 ms | 77 /s | 0.9961 / 0.9961 | 100% | 0.10 |
| C | 147 / 160 ms | 15 /s | 0.9966 / 0.9966 | 100% | 0.18 |

fp16 on the Arc reproduces the fp32 results. C is ~21x slower than B on the Arc; at 15/s a single host keeps up with ordinary mail volume, not with a bulk Takeout backfill (92k messages ≈ 1.7 h).

## 5. Gate re-evaluated with the new evidence
Privacy evaluation re-run on the new split (`phase6-6.4-privacy-v2.json`): redaction 0 failures; chance p99 2.37 bits.

| Candidate | Median exposure 1x/4x/16x | MIA AUC | Gate @2 |
| --- | --- | --- | --- |
| A | 1.40 / 1.00 / **3.49** | 0.529 | BLOCKED: CANARY_EXPOSURE_HIGH, NOT_BETTER_THAN_BASELINE, POORLY_CALIBRATED, PROXY_DATA_ONLY, NOT_RUN_ON_TARGET_RUNTIME |
| B | 1.03 / 1.68 / 0.70 | 0.533 | BLOCKED: PROXY_DATA_ONLY |
| C | 0.77 / 1.16 / 1.58 | 0.518 | BLOCKED: PROXY_DATA_ONLY |

Gate-input fix recorded: the first re-run read ECE from the canary-planted model trained inside the privacy evaluation (B 0.054, C 0.073) and so also blocked B and C as POORLY_CALIBRATED. The gate now reads ECE from the clean benchmarked model, which is the promotion candidate; the canary model is a test artifact that is never deployed. The saved inputs were re-evaluated (gate is pure); both values and the earlier decision are kept in the JSON. Note for later: calibration moved noticeably when the training set changed (80% subset + canaries), so recalibration must be part of any retraining.

## Still open (needs the owner)
- **PROXY_DATA_ONLY**: real phishing data, or the owner's mail with consent grants and approved labels. Spam vs ham remains a proxy.
- Exported model files (public-corpus weights only) are kept in `~/.cache/nervecenter/models/arc-export` (0700) for 6.5; delete when no longer needed.
