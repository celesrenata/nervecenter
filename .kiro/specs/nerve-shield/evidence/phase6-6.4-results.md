# Phase 6 task 6.4: privacy evaluation and blocked-promotion criteria

Status: run on the M5 Max with the public corpus (6.3) plus synthetic canaries; no owner mail. Raw metrics: `phase6-6.4-privacy.json` (no canary or training text; checked). Decisions NS-ADR-050. Tools: `backend/owner_tools/privacy_eval.py` (owner tool), gate `backend/mail_security/promotion_gate.py` (pure).

## Checks
1. **Redaction regressions** (model-independent): fixed suite of 8 secret and 3 PII strings through the 6.1 detectors and pseudonymiser: **0 failures**. A test proves the suite fails when the detector is disabled.
2. **Canary exposure** (Carlini et al.): canaries shaped to *evade* 6.1 (synthetic person + street address in words) planted in benign training messages 1, 4 and 16 times, 10 canaries per level; exposure = log2(1000 / rank among 999 look-alikes by model loss), max 9.97 bits.
3. **Membership inference**: loss-threshold attack, 600 training members vs 463 held-out messages from the same period (campaign groups held out); AUC 0.5 = chance.
4. **Artifact access**: score artifacts written `0600` in a `0700` directory under `~/.cache`, audited for git tree, permissions, owner, links: **no findings**. (Deleted after the run.)
5. **Retrieval leakage**: every candidate output checked to be a float score array; no candidate exposes text or ids; the serialised report contains no training text (checked: 0). None of the candidates includes a retrieval index.

Chance reference (computed): an unmemorised canary's median exposure over 10 is 1.02 bits (p95 1.90, p99 2.37).

## Results
| Candidate | Median exposure 1x / 4x / 16x (bits) | Worst single canary | MIA AUC | Gate @2 |
| --- | --- | --- | --- | --- |
| A n-gram baseline | 0.95 / 1.14 / **3.08** | 7.16 (16x) | 0.537 | BLOCKED: CANARY_EXPOSURE_HIGH, NOT_BETTER_THAN_BASELINE*, PROXY_DATA_ONLY, NOT_RUN_ON_TARGET_RUNTIME |
| B DistilRoBERTa | 2.10 / 0.87 / 0.46 | 3.74 (1x) | 0.510 | BLOCKED: NOT_BETTER_THAN_BASELINE, PROXY_DATA_ONLY, NOT_RUN_ON_TARGET_RUNTIME |
| C Qwen3-0.6B LoRA | 0.47 / 1.83 / 0.86 | 3.74 (4x) | 0.529 | BLOCKED: PROXY_DATA_ONLY, NOT_RUN_ON_TARGET_RUNTIME |

\* A is the reference, so it cannot beat itself.

Reading:
- The **n-gram baseline memorises repeated rare strings**: canaries seen 16 times are above the chance p99 (median 3.08 bits; one at 7.16 bits, i.e. ranked about 7th of 1,000). This is the realistic leak shape: an address or name in a signature repeats across many messages. The 6.1 detectors cannot catch such strings, so the selection step alone does not protect against it.
- **B and C** stay within chance at every repetition level on this run; one LoRA canary at 3.74 bits is the kind of outlier chance produces.
- **Membership inference** is near chance for all three (0.51–0.54). Caveats: a weak (loss-threshold) attacker, 2 training epochs, a public corpus. This does not prove absence of membership leakage.
- **C is blocked only by policy**: proxy data (spam/ham, not real phishing or owner mail) and never run on the target runtime (Arc/OpenVINO). Both are deliberate requirements before any shadow deployment.

## Gate design changes (both found by this run, recorded honestly)
- First run used 3 canaries per level and the maximum: chance alone exceeds 3 bits 33% of the time with that design (computed), which would block clean models. Changed to 10 canaries per level and the median.
- Gate @1 allowed up to 5.0 bits at higher repetition counts, which let A's 3.08-bit memorisation at 16x pass. Gate @2 applies 3.0 bits at every level (chance p99 2.37 plus margin). **This threshold was changed after seeing the data**; it applies to all candidates and only changed A's decision. The saved gate inputs were re-evaluated (no retraining; the gate is pure); `gate_v1` is kept in the JSON.
- The gate has no "promote" outcome; the best is ELIGIBLE_FOR_HUMAN_REVIEW. Missing or malformed evidence blocks.

## Residual risk (documented, not solved)
- Strings 6.1 cannot detect (names, postal addresses, free-text personal details) reach training data; only memorisation testing catches their leakage, and only probabilistically.
- Exposure and MIA are measured with one seed, one corpus and synthetic canaries; real mail repeats strings in more complex ways.
- Trained weights cannot be "unlearned": deleting a message from future datasets (R-017.5) requires retraining any model that saw it.
- No extraction attack through text generation was run: none of the candidates exposes generation; the LoRA base model could, if a future design exposed it.

Full suite: 8222 passed, 1 known unrelated failure (`test_routines` date test).
