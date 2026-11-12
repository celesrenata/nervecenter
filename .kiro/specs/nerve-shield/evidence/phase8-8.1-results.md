# Spike 8.1 — Threat-campaign clustering from privacy-safe fingerprints (research)

Date: 2026-11-12. Status: offline prototype only, in `backend/mail_security/research/`. Nothing in the runtime imports it, and nothing is wired. Quality data is the **public SpamAssassin corpus** (6,046 messages: 1,896 spam, 4,150 ham), not owner mail.

## Question

Can campaigns be grouped **without content** (no words, addresses or full URLs), with fingerprints that are time-limited and shareable only as aggregates?

## Prototype (`campaign_clustering.py`)

**Structural tokens** (the only inputs):
- the registrable domain of the sender and of each link host;
- URL path and subject *shape* (character classes with runs collapsed, e.g. `A: Aa #9 a!`; no words);
- subject length, capitalization, digit and `!`/`$` buckets;
- attachment extensions;
- MIME part count, depth and text subtypes;
- link and host counts;
- presence of a fixed set of headers (values are never read).

**Keying:** every token is an HMAC under `epoch_key(secret, floor(t / 7 days))`. Signatures are a 64-hash MinHash with LSH (16×4), and clustering uses union-find.

**`CampaignStore`:** it keeps only a signature, a size, first/last seen and an expiry (TTL 30 days, extended on new members). It releases aggregates only for clusters of at least **k = 3**, without cluster ids or tokens.

## Evaluation (`backend/owner_tools/campaign_cluster_eval.py`; raw: `phase8-8.1-eval.json`)

The reference is the existing *content* MinHash campaigns (`split_keys.campaign_keys`, 6.2): 320 spam campaigns of size 2 or more, covering 1,024 spam messages and 1,895 pairs. Pair precision and recall below are measured on spam pairs against that reference.

**v1** emitted presence tokens for both true and false. **v2** emits them only for headers that are present and drops near-constant flags. v2 was the only iteration; further tuning would have been fitting to the reference.

| Threshold | v1 P / R | v2 P / R | v2 clusters ≥3 | v2 largest | v2 label-pure share | v2 ham in spam-majority clusters |
|---|---|---|---|---|---|---|
| 0.6 | 0.001 / 0.99 | 0.002 / 0.94 | 37 | 5,149 | 94.6% | 0 |
| 0.7 | 0.002 / 0.88 | 0.005 / 0.72 | 77 | 4,077 | 92.2% | 3 |
| 0.8 | 0.049 / 0.56 | 0.163 / 0.36 | 151 | 2,132 | 96.0% | 6 |
| 0.9 | 0.609 / 0.22 | **0.831 / 0.16** | 229 | 191 | **98.7%** | **0** |

Privacy measurements (v2):

| Measure | Value |
|---|---|
| Distinct structural vocabulary | 7,008 |
| Messages with a **unique** keyed fingerprint set within an epoch | **77.5%** |
| Keyed tokens shared across adjacent epochs (500 sampled) | **0** |
| Mean signature similarity across epochs | **0.0** |
| Reference campaigns spanning more than one 7-day epoch | **60.3%** |
| Store clusters / releasable k≥3 aggregates (at 0.8) | 2,409 / 151 |

## Findings

1. **Content-free fingerprints give high precision but low recall.** At 0.9, 83% of structural spam pairs are real content campaigns and ham contamination is 0, but only 16% of campaign pairs are found. Lower thresholds don't trade off gracefully: single-link union-find chains clusters into blobs of up to 5,149 messages. Structure alone is too low in entropy, because many unrelated messages share the same layout.
2. **Purity is high (94–99%) at every threshold.** Spam and ham structures differ, so clusters rarely mix labels. That's useful as a "same kit" signal, but it isn't campaign identity.
3. **Raw fingerprints are quasi-identifiers.** 77.5% of messages have a unique keyed fingerprint set within an epoch, so sharing raw fingerprints across accounts would allow linking individual messages. Only k≥3 aggregates are safe to release. A cross-account version would also need k-anonymity across *accounts*, not just messages.
4. **The time limit works but costs recall.** Epoch keys make fingerprints fully unlinkable across weeks (0 shared tokens), but 60% of real campaigns run longer than a week and get split across epochs. Longer epochs mean more linkability and better recall.
5. **A useful product would combine signals.** That means structural fingerprints together with intel indicator matches (2.2) and per-account content MinHash kept inside the account. That combination is a design for a follow-on spec, not something built here.

## Limits

- **The reference is itself an approximation.** Content MinHash at 0.6 is not ground-truth campaign labels, and SpamAssassin spam is from 2002–2005.
- **Single-link clustering.** Average-link or a graph community method might reduce chaining. That wasn't tried, to avoid tuning to the reference.
- **No adversarial test.** An attacker can randomize structure cheaply; this spike didn't measure robustness to that.
- **Minimal PSL.** Registrable domains use the minimal built-in PSL.

## Proposed follow-on (needs a separate G8 approval)

1. Per-account content campaigns inside the account boundary.
2. Structural clusters as a secondary "same kit" signal.
3. Releasing only k-anonymous (k≥3 accounts) aggregates with a TTL, and only after an explicit privacy review.

## Tests

`tests/backend/test_mail_security_research_8_1.py`: **5 passed**. They cover:
- tokens carry no words, addresses, URLs, filenames or header values;
- the subject-shape function;
- one campaign clusters while newsletters stay apart;
- epoch and secret unlinkability, with a stable signature inside an epoch;
- the store's TTL expiry and k-only releasable aggregates (no cluster ids);
- empty evidence is handled safely.

The 8.2 guard test (no runtime import of `research`) also covers this module.
