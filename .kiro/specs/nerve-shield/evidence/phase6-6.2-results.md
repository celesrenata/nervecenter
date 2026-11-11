# Phase 6 task 6.2: signed manifest and leakage-safe splits

Status: implemented, unwired, synthetic data only; nothing trained. Decision NS-ADR-045.

Done criterion:
- Leakage: 20 seeded corpora (300 examples, short threads, time-local campaigns crossing the cutoff) with a 14-day embargo: no thread key and no campaign key appears in both splits, every train example is before cutoff minus embargo, every eval example is at/after the cutoff, every example is assigned or dropped exactly once. The test also asserts it is not vacuous (> 100 train, > 40 eval, some groups dropped), and a guard test shows a naive per-message time split leaks campaigns on at least 10 of those 20 corpora.
- Reproducibility: shuffled inputs give an identical canonical body and identical signed digest.

Also tested: whole-group drop for a thread/campaign straddling the cutoff; embargo drop; missing, naive or malformed metadata dropped; manifest holds digests, not text; weak provider hints marked non-label; counts plus drops cover every example; signing refuses a missing, rejecting or malformed review and does not mutate the caller's body; double signing refused; every tamper (split, label, removed entry, cutoff, reviewer, counts) fails; a recomputed digest still fails the signature; an attacker re-signature fails against the trusted key; an unreviewed body does not verify.

Found while building: my first synthetic corpus linked everything into one straddling group, so the leakage test passed with empty splits. The corpus now has a realistic shape and the test asserts non-empty splits.

Full suite: 8093 passed, 1 known unrelated failure.

Open: signing key custody (where the Ed25519 private key lives) is an owner decision; campaign keys must come from a real campaign clusterer (not built); thread keys from References/In-Reply-To digests.
