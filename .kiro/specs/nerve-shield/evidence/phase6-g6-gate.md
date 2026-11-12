# Phase 6 — G6 Gate summary (owner-facing) — STOP, decision requested

**Status: STOPPED AT G6. Phase 7 has NOT been started. No model is registered, shadowed, canaried or deployed.**
**Gate text (tasks.md 6.6):** "Present approved datasets, privacy report and model comparisons; owner decides whether any model may enter shadow/canary."
**Scope run:** tasks 6.1–6.5 plus gap closure. Training only on the M5 Max (unified memory); the 5090 / 4070 Ti Super were not used. Data: SpamAssassin public corpus (owner choice A) and synthetic canaries; **no owner mail was used for training**.

## 1. Datasets
| Dataset | Status | Detail |
| --- | --- | --- |
| Owner mail (Takeout) | **None selected** | 6.1 selects nothing without consent grants and approved feedback labels; none exist. The real export was only dry-run (counts). |
| `spamassassin-bench-v2` (public, spam/ham proxy) | **Built, unsigned — awaiting owner review** | 5,263 entries; TRAIN 2,886 (1,217 spam), EVAL 2,377 (272 spam); leakage-safe split (thread + campaign groups, cutoff 2002-09-01, 7-day embargo, time from topmost `Received`). File `~/.cache/nervecenter/datasets/spamassassin-bench-v2.unsigned.json`, sha256 `ee2265ff1b51766125b71258857e82eae8250414d4a28e520f23081d04de8d3f`. Labels are corpus labels, recorded as `PUBLIC_CORPUS_LABEL`. |

So: **no approved dataset exists yet.** Approving the benchmark set means signing it (command below).

## 2. Model comparison (`phase6-6.3-benchmark-v2.json`, sha256 `20bc4e3b…f4f6`)
| Candidate | ROC AUC [95% CI] | Recall @1% FPR | Precision / recall at chosen threshold | ECE | Seeds (AUC) | Arc iGPU |
| --- | --- | --- | --- | --- | --- | --- |
| A hashed n-grams + logistic regression | 0.985 [0.977, 0.991] | 0.81 | 0.78 / 0.92 | 0.065 | 1 | not needed (CPU) |
| B DistilRoBERTa fine-tune | 0.996 [0.993, 0.998] | 0.94 | 0.94 / 0.91 | 0.031 | 0.9946 ± 0.0013 | 6.9 ms, 77/s, matches fp32 |
| C Qwen3-0.6B LoRA (merged) | **0.997** [0.994, 0.999] | **0.97** | **0.97 / 0.92** | **0.018** | 0.9973 ± 0.0012 | 147 ms, 15/s, matches fp32 |
| D frozen Qwen3-Embedding (ArcForge) + head | 0.981 [0.973, 0.988] | 0.72 | 0.74 / 0.92 | 0.054 | 1 | already served |

B and C are better than A on all three ranking metrics (paired bootstrap CIs above 0); D is not distinguishable from A. Arc evidence: `phase6-arc-runtime.json` (sha256 `d6f424dd…a6`): fp16 on the Arc physical function reproduces CPU fp32 AUC exactly, 100% sign agreement. Artifacts (public-corpus weights): B `model.bin` sha256 `22a6de9a…7045`, C `model.bin` sha256 `3699bb5a…0213`, in `~/.cache/nervecenter/models/arc-export` (0700/0600).

Practical trade-off: C is the most accurate and best calibrated, but ~21x slower than B on the Arc (15 vs 77 messages/s per host). B is nearly as good and cheap enough for bulk backfills.

## 3. Privacy report (`phase6-6.4-privacy-v2.json`, sha256 `ecf20248…dc8d`)
| Candidate | Median canary exposure 1x / 4x / 16x (bits; chance p99 2.37) | MIA AUC | Gate `mail-model-promotion-gate@2` |
| --- | --- | --- | --- |
| A | 1.40 / 1.00 / **3.49** | 0.529 | BLOCKED: canary exposure, not better than itself, calibration, proxy data, not on target runtime |
| B | 1.03 / 1.68 / 0.70 | 0.533 | **BLOCKED: PROXY_DATA_ONLY** |
| C | 0.77 / 1.16 / 1.58 | 0.518 | **BLOCKED: PROXY_DATA_ONLY** |

Redaction regression suite: 0 failures. No candidate output contains text; no candidate has a retrieval index; artifacts private and outside git.

Residual risks (documented): strings the 6.1 detectors cannot see (names, addresses) reach training data, and only memorisation testing catches their leakage; the n-gram baseline memorises repeated strings; one corpus, synthetic canaries, a weak membership attack; trained weights cannot be unlearned without retraining.

## 4. Registry and deployment controls (6.5)
Registry exists (no auto-promote; automatic rollback on regression; signed-dataset lineage required; hash-chained log). Done criterion met in tests. No model is registered: the benchmark manifest is unsigned, and once signed B/C would register as BLOCKED by the gate.

## 5. Defects found and fixed in Phase 6
- MIME parser crashed on `Message-ID: <>` (found by the corpus); fixed, regression-tested, **deployed** (`e671255`).
- `Date` header unusable for time splits (spam forges it); splits use the topmost `Received` header.
- Own-design errors caught and recorded: an empty-split leakage test, a noisy canary rule (3-max), a lax repeated-canary threshold (gate @1), a 17-spam validation slice that distorted thresholds and the B-vs-A conclusion, and a gate input reading calibration from the canary test model.

## 6. What G6 cannot show
Real phishing performance, behaviour on the owner's mail, adversarial robustness, long-run drift. Every number above is a spam/ham proxy on 2001–2002 mail.

## Decisions requested
1. **Datasets:** sign (approve) `spamassassin-bench-v2` as a benchmark dataset? Command (you run it; it uses your SSH key):
   `uv run python -m backend.owner_tools.sign_dataset sign --body ~/.cache/nervecenter/datasets/spamassassin-bench-v2.unsigned.json --reviewer <you> --key ~/.ssh/id_ed25519 --out ~/.cache/nervecenter/datasets/spamassassin-bench-v2.signed.json`
2. **PROXY_DATA_ONLY:** keep the gate strict (no model may enter shadow until it is trained/evaluated on real phishing or consented owner data), or allow proxy data for a **shadow-only** trial (observe, never decide)?
3. **Shadow/canary:** may any model (B or C) enter SHADOW? Recommendation: **no canary or active use yet.** If you want early signal, allow **C in SHADOW only** after (1) and an explicit proxy-data exception, on the Arc, observing live mail without affecting any verdict.
4. **Next phase:** Phase 7 (safe actions and review UX) needs a G6 decision first.

## Owner decision (to be completed by owner)
```
Decision:
Date:
Notes:
```
