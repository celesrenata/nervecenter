# Phase 3 / Task 3.2 — CPU baseline + dataset-free harness (Deliverable B)

**Reqs:** R-008, R-021, R-007.2, I-08, I-09. **Experiment:** EXP-02 (CPU variant only).

## Read this first (honesty rules)

1. **Quality numbers are on SYNTHETIC template data. They say nothing about real-world
   detection.** The classes are trivially separable by construction; perfect scores are
   expected and meaningless.
2. **Latency/throughput were measured on this Apple arm64 development machine.** They are NOT
   representative of the GTi14 CPU (Core Ultra 9 185H) or the Intel Arc GPU. No Arc/GPU/OpenVINO
   code ran; nothing here is a GPU benchmark and nothing is estimated from TOPS.
3. **Scores are UNCALIBRATED raw scores (logit margin), not probabilities** (R-007.2). No
   calibration was performed. The classifier is not wired into worker/policy/API/startup and is
   not an authority (I-08).
4. Task 3.2's "measured quality on held-out labels" is satisfied only in the narrow sense that a
   held-out *synthetic* set (different seed, same templates) was scored. There is no real or
   authorized-public labeled corpus yet; per design §6, no historical mail is used until opt-in
   corpus controls exist. **Do not mark the task as evidence of detection capability.**

## What was built (`backend/mail_security/classifier/`)

| Module | Purpose |
|---|---|
| `features.py` | `FeatureRecord`: strict (`extra=forbid`), frozen, bounded schema. Only URL-domain char n-grams (≤128 × ≤8 chars), finding rule ids + counts (≤32), 5 structural counts (0..10000), sender-domain label tokens (≤8 × ≤32). Charset-restricted patterns reject `@`, whitespace and free text. No subject/body/address/path/filename fields; label lives in `LabeledExample`, not the record. |
| `synthetic.py` | Seeded template generator (below). |
| `baseline.py` | Hashed (`crc32`, 4096 dims) logistic regression, SGD + L2, 8 epochs, shuffle from `random.Random(seed)`. Weights rounded to 10 decimals; JSON with unkeyed SHA-256 digest (detects corruption, does not authenticate origin). `predict`, `predict_batch`. Prediction carries `ScoreSemantics.RAW_SCORE`, `calibrated=False`, and no probability field. |
| `metrics.py` | From scratch: confusion/precision/recall/FPR, PR-AUC (average precision, ties collapsed; undefined → `None`), nearest-rank p50/p95/p99, throughput with injectable clock. |
| `bench.py` | Dataset-free harness + report renderer; every line carries its caveat. |

Tests: `tests/backend/test_mail_security_classifier_bench.py` (28 tests): generator determinism,
schema strictness, metrics vs hand-computed values (PR-AUC worked example, ties, empty and
single-class), latency with injected clock, training determinism/digest/tamper detection,
uncalibrated labeling, report caveats, static import check (no network/subprocess/third-party;
only stdlib + pydantic + `backend`), and a check that nothing outside the package imports it.

## Generator design and known limits

Fixed before running anything; **not tuned** after seeing results.

- Invented names only. MALICIOUS: credential phish / payment fraud / malware lure → brand + lure
  word + cheap TLD domains (sometimes digit-substituted), 1–2 URL domains, malicious rule-id pool.
  BENIGN: newsletter / institution / receipt → plain organisation domains, benign rule-id pool.
- Deliberate overlap: tracking-pixel, remote-image and shortened-link rules appear on both classes
  with different probabilities.
- Limits: templates are trivially separable (the TLD/lure n-grams alone suffice); held-out data
  shares templates with training, so it measures template memorisation, not generalisation; no
  adversarial/evasive samples, no drift, 50% base rate; `random.Random` output is stable per
  CPython minor version, not guaranteed across versions.

## Environment

- Platform: Darwin arm64, CPython 3.11.16 (`uv run python -V`), stdlib-only code.
- Operator-side hardware: not applicable (no GTi14 / Arc run was performed).

## Commands and observed numbers

```
uv run python -m backend.mail_security.classifier.bench --seed 1 --train 2000 --test 1000
```

Seed 1, train 2000, held-out 1000 (497 positive), threshold 0.0 (fixed a priori), model digest
`282ca66ed34c000500dbdb32ae191736b9b3b737e5a6ced50f5c7ab6857346df` (identical on repeat run):

| Metric | Value | Caveat |
|---|---|---|
| tp / fp / tn / fn | 497 / 0 / 503 / 0 | SYNTHETIC; not real-world |
| precision / recall / FPR | 1.0000 / 1.0000 / 0.0000 | SYNTHETIC; trivially separable |
| PR-AUC (average precision) | 1.0000 | SYNTHETIC; expected and meaningless |
| per-message latency p50 / p95 / p99 | 0.0055 / 0.0087 / 0.0099 ms (run 1); 0.0055 / 0.0085 / 0.0096 ms (run 2) | Mac arm64 only |
| single-call throughput | ~171.6k / ~172.8k msg/s (runs 1, 2) | Mac arm64 only |
| batch throughput (n=1000) | ~179.2k / ~175.2k msg/s (runs 1, 2) | Mac arm64 only |

Seed 2 (default sizes) also gave tp=506 fp=0 tn=494 fn=0, precision=recall=PR-AUC=1.0000
(SYNTHETIC; not real-world). Timings vary run to run; quality numbers and digest are deterministic.
Latency covers `predict` on an already-built `FeatureRecord` only: no feature extraction, parsing,
IPC, or queueing, so it is not an end-to-end per-message figure.

## What remains before 3.2 could count as real evidence

- An opt-in, de-identified, reviewed labeled corpus (or licensed public data) with a versioned
  held-out split, and realistic base rates; then re-measure precision/recall/PR-AUC/FPR.
- Calibration study (R-021.1) before any probability is shown.
- The same harness run on the GTi14 CPU hosts (task 3.1 inventory first) for representative
  latency; Arc/OpenVINO comparison belongs to 3.3+.
- Wiring (3.4–3.6) is intentionally not started.
