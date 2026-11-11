# Phase 6 task 6.2: signed manifest and leakage-safe splits

Status: implemented, unwired, synthetic data only; nothing trained. Decision NS-ADR-045.

Done criterion:
- Leakage: 20 seeded corpora (300 examples, short threads, time-local campaigns crossing the cutoff) with a 14-day embargo: no thread key and no campaign key appears in both splits, every train example is before cutoff minus embargo, every eval example is at/after the cutoff, every example is assigned or dropped exactly once. The test also asserts it is not vacuous (> 100 train, > 40 eval, some groups dropped), and a guard test shows a naive per-message time split leaks campaigns on at least 10 of those 20 corpora.
- Reproducibility: shuffled inputs give an identical canonical body and identical signed digest.

Also tested: whole-group drop for a thread/campaign straddling the cutoff; embargo drop; missing, naive or malformed metadata dropped; manifest holds digests, not text; weak provider hints marked non-label; counts plus drops cover every example; signing refuses a missing, rejecting or malformed review and does not mutate the caller's body; double signing refused; every tamper (split, label, removed entry, cutoff, reviewer, counts) fails; a recomputed digest still fails the signature; an attacker re-signature fails against the trusted key; an unreviewed body does not verify.

Found while building: my first synthetic corpus linked everything into one straddling group, so the leakage test passed with empty splits. The corpus now has a realistic shape and the test asserts non-empty splits.

Full suite: 8093 passed, 1 known unrelated failure.

Open: signing key custody (where the Ed25519 private key lives) is an owner decision; campaign keys must come from a real campaign clusterer (not built); thread keys from References/In-Reply-To digests.

## Gaps closed (2026-11-11, NS-ADR-046)
- Thread keys from Message-ID / In-Reply-To / References: a whole thread shares one key; keys are opaque and per account; messages without an id fall back to their own key.
- Campaign clusterer: 12 copies of an ~75-word phish with 2 per-recipient edits form one campaign; 30 unrelated messages stay 30 campaigns; deterministic, order-independent, per account. End to end with real thread and campaign keys: a campaign whose copies straddle the cutoff is dropped from both splits while unrelated mail splits normally. First attempt (5-shingles, 30-word test text with 7% edits) did not cluster; switched to 3-shingles and a realistic body, and kept a test that documents the limit for short, heavily edited copies. Speed: 2,000 docs x 150 words in 2.7 s.
- Key custody: explicit creation only, refuses overwrite, `0600`/`0700`, owner check, symlink and loose-permission refusal, refusal inside a git work tree, default path outside the repo; manifests carry a signed key id; verification against pinned keys refuses unpinned keys and tampered ids.

Full suite: 8113 passed, 1 known unrelated failure.

Still open (owner): whether to generate the signing key now on this Mac, or hold custody elsewhere.
