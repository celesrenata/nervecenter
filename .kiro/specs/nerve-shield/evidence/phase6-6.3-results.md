# Phase 6 task 6.3: baseline vs fine-tuned encoder vs LoRA

Status: benchmark run on the owner's M5 Max (40-core GPU, 128 GB unified memory, macOS 26.6.2, Python 3.11.16, torch 2.15.0 on MPS). GPUs on the 5090 / 4070 Ti Super hosts were not touched (owner: training stays in system RAM). No model was deployed or registered. Raw metrics: `phase6-6.3-benchmark.json` (metrics only, no text). Decision NS-ADR-049.

## Data and protocol
- Corpus: SpamAssassin public corpus, 6,046 messages (owner choice A). Spam vs ham is a **proxy** for malicious vs benign, not phishing; these numbers say nothing about real phishing or the owner's mail. Corpus kept in `~/.cache/nervecenter/corpora/spamassassin` (0700), never in the repo.
- Same pipeline as production: bounded MIME parser, 6.1 pseudonymisation, real thread keys and campaign keys (5,139 campaigns, 4,789 threads), 6.2 split (cutoff 2002-09-01, 7-day embargo).
- Message time is the topmost `Received` header (5,911) with `Date` only as fallback (135): spam forges `Date` (years 0102, 1980, 2028 seen).
- Split: fit 2,453 (1,200 spam), val 433 (17 spam, latest 15% of the training period), eval 2,377 (272 spam), dropped 783 (straddling groups).
- Platt calibration and the cost threshold (FP weighted 5x) chosen on val only; everything below is on eval. Paired bootstrap, 1,000 resamples.
- Reproducible: a second full run gave identical metrics for all four candidates.

## Results (eval, 95% CI)
| Candidate | ROC AUC | PR AUC | Recall @ 1% FPR | ECE (calibrated / raw) | Train | Infer / msg | Trainable params | Mem |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| A hashed n-grams + logistic regression (CPU) | 0.984 [0.976, 0.991] | 0.932 [0.908, 0.952] | 0.805 [0.736, 0.867] | 0.020 / 0.143 | 3.7 s | 0.44 ms | 2x2^20 features | small |
| B DistilRoBERTa fine-tune (MPS) | 0.990 [0.985, 0.994] | 0.950 [0.931, 0.967] | 0.890 [0.785, 0.927] | 0.018 / 0.078 | 42 s | 2.9 ms | 82 M | 4.4 GB |
| C Qwen3-0.6B LoRA r16 (MPS, bf16) | **0.993** [0.990, 0.996] | **0.960** [0.943, 0.975] | **0.904** [0.836, 0.938] | 0.028 / 0.174 | 346 s | 17.5 ms | 4.6 M | 10.2 GB |
| D frozen Qwen3-Embedding-0.6B + logistic head | 0.979 [0.972, 0.986] | 0.901 [0.872, 0.926] | 0.673 [0.588, 0.757] | 0.016 / 0.128 | 83 s* | 33.9 ms | 1,025 | — |

\* mostly embedding the training set; the head fits in seconds.

Difference vs A (paired, 95% CI):
- B: AUC [-0.002, +0.014], PR AUC [-0.005, +0.043], R@1%FPR [-0.027, +0.142] -> **not distinguishable from the baseline**.
- C: AUC [+0.002, +0.018], PR AUC [+0.007, +0.051], R@1%FPR [+0.024, +0.159] -> **better than the baseline on all three**.
- D: AUC [-0.015, +0.005], PR AUC [-0.060, -0.001], R@1%FPR [-0.229, -0.047] -> **worse than the baseline**.

At the val-chosen thresholds all candidates had low recall (A 0.44, C 0.62): val held only 17 spam, so those thresholds are unreliable. The threshold-free metrics above are the comparison; thresholds need a larger, labelled val set.

## MLX vs PyTorch (training throughput only)
Same Qwen3-0.6B, LoRA r8 on q/k/v/o, batch 8, 256 tokens, causal-LM objective, 30 steps: PyTorch MPS 2,636 tokens/s (12.8 GB); MLX (`mlx-lm` 0.32.2) about 6,000 tokens/s, so **~2.3x faster**. MLX's LoRA trainer is generative, not a classifier, so its accuracy was not compared.

## What this does and does not show
- On this proxy task, a LoRA on a 0.6B model ranks messages measurably better than the classical baseline, at ~40x the inference cost and ~95x the training time. The fine-tuned encoder is not clearly better than the baseline. **LoRA is not shown to be best in general**: one corpus, one seed, a proxy label, old (2001–2002) mail.
- **GPU runtime compatibility:** only D runs on the Arc iGPUs today (ArcForge serves this exact embedding, measured in Phase 3), and D is the weakest. A, B and C have **not** been run on Arc: A needs no GPU; B and C would need an OpenVINO export, which has not been tried. C at 17.5 ms per message on the M5 Max is fast enough for the Mac but untested on Arc.
- **Calibration:** Platt scaling cut ECE from 0.08–0.17 to 0.016–0.028 for every candidate; raw scores should never be read as probabilities.

## Defect found by the benchmark (fixed separately, NS-ADR-048)
The production MIME parser crashed on `Message-ID: <>` (one corpus message). Fixed in `3524d4f` with regression tests; the deployed image still has it until redeployed.

## Not done
Real phishing data, the owner's mail (needs consent and labels), OpenVINO export of B/C, more seeds and corpora, a larger val set for thresholds, adversarial robustness. 6.4 (privacy evaluation) must run on any candidate before 6.5/6.6.
