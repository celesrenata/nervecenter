# Phase 3 / Task 3.3 (harness half) — Owner runbook for the GTi14

**Reqs:** R-008, R-021, R-022.5, I-08, I-09. **Experiment:** EXP-02. **Threat:** T-27.
**Task 3.3 is NOT complete.** Only the harness exists. No OpenVINO, Arc, or GTi14 measurement has
been taken. Do not tick 3.3 until a human has produced real reports from the GTi14.

## Honesty rules

1. Synthetic quality is not real-world detection quality. The harness refuses a real-world claim
   for synthetic data (`RealWorldClaimError`) and every report carries `real_world_claim: false`.
2. Timings are valid only for the host that produced them. A `cpu_baseline` report
   taken on a Mac is NOT a GTi14 number.
3. The `openvino` backend is **UNTESTED ON HARDWARE**. It was written without being run. Its first
   real run may expose bugs; fix them before trusting any number.
4. Scores are UNCALIBRATED raw scores. Classifier output is never an authority (I-08).
5. Do not use real private mail. An owner dataset must be local JSONL of already de-identified
   `LabeledExample` records (`{"features": {...}, "label": "BENIGN|MALICIOUS"}`) and needs
   `--attest-deidentified`. Real mail stays off-limits until Phase 6 consent controls exist.

## What exists

`backend/mail_security/classifier/harness.py` (stdlib only; `openvino` imported lazily by name
inside the backend only). Not wired into worker, policy, API or startup.

| Backend | Behaviour |
|---|---|
| `cpu_baseline` | Always available (Deliverable B). Trains on seeded SYNTHETIC data, scores the held-out set. Optional `--artifact-sha256` pins the model digest. |
| `openvino` | Needs: `openvino` module, requested device present, artifact file, owner-pinned sha256, and a model-specific encoder. Any missing piece gives `status: NOT_AVAILABLE` with reason codes, `measurements`/`quality`/`cold_start`/`peak_rss_bytes` all `null`. Never numbers, never a CPU substitute. |

Reason codes: `OPENVINO_MODULE_MISSING`, `OPENVINO_DEVICE_MISSING`, `MODEL_ARTIFACT_NOT_PROVIDED`,
`MODEL_ARTIFACT_FILE_MISSING`, `OPENVINO_ENCODER_NOT_PROVIDED`, `OPENVINO_RUNTIME_ERROR`.

Artifact pinning (T-27): the owner supplies the expected sha256. If the file exists and the
digest differs, `ArtifactDigestMismatch` is raised before any backend is prepared or run; a
missing/malformed pin raises `ArtifactNotPinned`. Messages never include paths or digests.
The CLI prints `REJECTED <CODE>` to stderr and exits 3.

Exit codes: 0 OK, 2 NOT_AVAILABLE, 3 rejected (typed error).

Report fields: `backend` (name, device_requested, device_selected, model_id, artifact_sha256,
runtime_version, driver_version, category, calibration_id, score_semantics), `environment`
(python_version, platform, machine), `seed`, `dataset` (kind, record_count, digest_sha256),
`batch_sizes`, `warmup_runs`, `cold_start` (separate from warm runs), `measurements`
(p50/p95/p99 per batch call, throughput, per batch size), `peak_rss_bytes` (process high-water
mark via `resource`, includes dataset and training), `quality` (precision, recall, FPR, PR-AUC at
a fixed a-priori threshold, with `scope`), `caveats`, `hardware_verification`.

## CPU fallback semantics (for later tasks; NOT built)

Fallback to CPU is explicit, never silent. A caller asking for `openvino` that cannot get it gets a
typed unavailable result. Any CPU scoring afterwards is requested by name, reported under its own
identity (`cpu_baseline` model id and artifact digest), and is distinguishable from accelerator
output in every response and metric.

## Steps for the owner on a GTi14

1. **Inventory (task 3.1, read-only):** run the inventory kit; fill in
   `phase3-3.1-inventory-TEMPLATE.md` with OS, kernel, i915/xe driver, Level Zero / compute
   runtime, `/dev/dri`, Python version.
2. **Install decision (below):** record in `decisions.md` which libraries may be installed and at
   what pinned versions. Do not install until decided.
3. **Baseline on the host:**
   `python -m backend.mail_security.classifier.harness --backend cpu_baseline --seed 1 --output cpu.json`
4. **Model choice and conversion:** choose the model family, export/convert it on the host, then
   compute `sha256sum model.onnx` (or the chosen single-file artifact) and record it.
5. **Encoder:** write the feature encoder for that model (FeatureRecord -> model input) and
   construct `OpenVinoBackend(path, device, encoder)` via the Python API, passing it to
   `run_harness(..., backend_override=...)`. The CLI cannot supply an encoder, so it reports
   `OPENVINO_ENCODER_NOT_PROVIDED` by design. Assumed, unverified output convention: first output =
   one raw score per record.
6. **Run:**
   `python -m backend.mail_security.classifier.harness --backend openvino --artifact PATH --artifact-sha256 HEX --device GPU --output ov.json`
   (add `--device CPU` to run OpenVINO on CPU for an apples-to-apples runtime comparison).
7. **Expected shape:** `status: OK`, `backend.device_selected` taken from OpenVINO's device list,
   `runtime_version` and `driver_version` filled (driver may be `null` if the property is not
   exposed), `measurements` per batch size, `cold_start` separate, `hardware_verification` =
   `UNTESTED ON HARDWARE` until you have reviewed and accepted the run.
8. **Verify GPU use independently** (e.g. host GPU utilization tooling) — the report alone does not
   prove the Arc was used (EXP-02 asks for verified GPU utilization).
9. Attach both reports to `evidence/`, compare, and only then consider marking 3.3.

## Open decisions for the owner

1. Which libraries may be installed and pinned (openvino; OVMS; onnxruntime), and versions.
2. Which model families to compare (text/URL encoder, embedding + linear head, lexical + rules) and
   where artifacts come from (licence, provenance, sha256).
3. Which runtime path first: OpenVINO Python API, OVMS, or ONNX Runtime. Design §6 says benchmark
   OpenVINO first and compare the others on actual versions/drivers; do not assume memory or GPU
   availability from the SKU.
4. Single-file artifact packaging (ONNX) versus OpenVINO IR (`.xml` + `.bin`, which the
   one-file pin does not yet cover).
5. Dataset: synthetic only, or an attested de-identified owner set.

## VERIFIED vs NOT VERIFIED ON HARDWARE

| Item | Status |
|---|---|
| Report schema, JSON round-trip, determinism of non-timing fields | VERIFIED (unit tests, this Mac) |
| `cpu_baseline` run and report | VERIFIED on this Mac only; NOT GTi14 |
| `openvino` returns NOT_AVAILABLE without module and emits no numbers | VERIFIED (this Mac, module absent) |
| Digest mismatch / missing pin rejected, nothing runs | VERIFIED (unit tests) |
| Synthetic quality cannot be called real-world | VERIFIED (unit tests) |
| Stdlib-only, no network/subprocess, lazy openvino import, not wired to runtime | VERIFIED (static tests) |
| OpenVINO import, device enumeration, `read_model`/`compile_model`, output convention | NOT VERIFIED ON HARDWARE (never executed) |
| Arc GPU execution, driver version properties, utilization | NOT VERIFIED ON HARDWARE |
| GTi14 CPU and Arc latency, throughput, memory | NOT VERIFIED ON HARDWARE |
| Any model's quality, quantization artifacts, fit in memory | NOT VERIFIED ON HARDWARE |
| OVMS / ONNX Runtime comparison | NOT BUILT; NOT VERIFIED |
