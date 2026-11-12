# Phase 6 task 6.5: model registry, staged deployment, rollback

Status: implemented as a pure library (`backend/mail_security/model_registry.py`), unwired; no model is deployed. Decision NS-ADR-052.

## Design
- Stages: REGISTERED -> SHADOW -> CANARY -> ACTIVE, plus ROLLED_BACK, RETIRED, BLOCKED.
- **No auto-promote.** `approve_transition` (human approver ref + approval ref) is the only forward path; stages cannot be skipped; SHADOW requires a gate decision of ELIGIBLE_FOR_HUMAN_REVIEW; CANARY and ACTIVE each need a passing observation window *in the current stage*. Fifty passing windows in a row change nothing.
- **Automatic rollback only.** `record_window` checks policy `mail-model-deploy-policy@1` (FPR <= 1% on reviewed benign mail, FPR not +0.2 pt worse than ACTIVE, recall >= 97% of ACTIVE, error rate <= 0.5%, p95 latency <= 500 ms, ECE <= 0.05, window >= 500 messages). Any failure rolls the model back and, if it was ACTIVE, restores the last known-good model. Too-small windows change nothing.
- **Lineage** (immutable record): artifact sha256s, dataset manifest sha256 **and its signing key id (must be a real SSH/file key id — unsigned manifests are refused)**, benchmark and privacy report sha256s, training config, base model, code commit, gate decision.
- **Tamper-evident log**: append-only, hash-chained events; `verify_log` detects edits, deletions and reordering; with the stored `head` hash it also detects a truncated tail.
- **Routing**: deterministic hash of the message key puts ~5% (policy) on the CANARY model; SHADOW models only observe.

## Done criterion
`test_regression_in_canary_prevents_release_and_rolls_back`: m1 ACTIVE; m2 promoted with approvals to CANARY; a window with FPR 2% rolls m2 back, m1 stays ACTIVE and keeps deciding, and approving m2 to ACTIVE is refused. Also tested: rollback after activation restores the retired previous model; every regression kind (FPR, error rate, latency, calibration drift, recall vs active, FPR vs active).

36 registry tests. Full suite in the G6 summary.

## Real candidates
B and C cannot be registered yet: their training dataset manifest is built (`~/.cache/nervecenter/datasets/spamassassin-bench-v2.unsigned.json`, sha256 `ee2265ff…8d3f`, 5,263 entries) but **unsigned**. Signing asserts the owner's review, so it is left to the owner (`backend/owner_tools/sign_dataset.py sign`). Once signed they would register as BLOCKED (gate: PROXY_DATA_ONLY) and could not enter SHADOW.
