# Per-account content campaigns, SHADOW (NS-ADR-069, G8 item 6)

At G8 the owner said "6. you choose". It was deferred until full capture (item 1) was live, which it now is, and the owner then said "carry on". Commits: `d0cde9bc` (feature) and `9b0e01fa` (queue-ordering fix). Both are deployed; migration head is `022_mail_campaigns`.

## What was built (spike 8.1 recommendation, per-account only)

- **Signature.** The 6.2 keyed word-3-shingle MinHash (`split_keys.minhash`, 64 hashes) over the message's normalized plain text (HTML text if there is no plain part). Texts under 12 words aren't fingerprinted.
- **LSH.** 16 bands × 4 rows; each band is stored only as an HMAC `band_key`. A message joins a campaign when a band collides **and** the Jaccard estimate is ≥ 0.6 (the 6.2 setting).
- **Keys.** Derived from `NERVE_MAIL_SECURITY_LEDGER_KEY` under the separate label `campaigns/v1/<scope>`. Signatures never correlate across accounts, or with ledger fingerprints. Without the key the feature is inert.
- **Retention.** 30-day TTL. Expired rows are pruned on write and ignored on read. Signatures are derived from content, so they stay inside the account and are never released. The structural "same kit" fingerprints from 8.1 and any cross-account sharing were **not** built; sharing is not approved.
- **Campaign ids** are assigned on insert (the first matching member's campaign, otherwise a new one) and never merged. That can split a campaign but never joins unrelated mail.
- **Tables (022).**
  - `mail_campaign_members`: scope, source key, opaque body ref, campaign id, signature, expiry.
  - `mail_campaign_bands`: scope, band key, member id, expiry.
- **Recording.** The security worker records membership after the verdict commits. It is best-effort and can never change the verdict.
- **Output (SHADOW only).** Review queue `mail-review-queue@3` adds `CAMPAIGN_OF_FLAGGED` (+2, MEDIUM) to a message whose campaign contains **another** REVIEW/RESTRICTED message. A flagged message never counts itself. Dispositions, gate decisions and attention items are unchanged. The UI label is "Looks like another message flagged for review".
- **Seed.** Owner tool `seed_campaigns` processes stored mail only (the objects the worker scanned, through the same parser), with no Gmail access and no new verdicts. It is idempotent.

## Live results

**Seed:**

| Account | Recorded | Joined an existing campaign | Too short | Campaigns with ≥ 2 members | Size histogram |
|---|---|---|---|---|---|
| 53b03939 | 292 | 104 | 5 | 37 | 2:18, 3:11, 4:1, 6:1, 8:1, 9:2, 10:1, 12:1, 14:1 |
| fc4e3c57 | 44 | 4 | 1 | 3 | 2:2, 3:1 |

**Regression found live and fixed.** The 53b03939 queue gained 24 campaign siblings of REVIEW messages. Two were newer than some REVIEW items and took visible slots, pushing **4 REVIEW items into the digest**. A SHADOW-only signal must never displace a disposition-queued item. The fix: within a tier, disposition-queued items sort first. A regression test reproduces the live ordering; it fails without the fix and passes with it.

**After the fix (53b03939):**
- 10 REVIEW visible (the cap) and 2 REVIEW in the digest, the same as before this feature.
- 23 campaign siblings in the digest.
- 1 campaign sibling visible, because it is protected (important sender or linked commitments), and protected items are never digested.

The fc4e3c57 queue is empty.

**Interpretation.** Most siblings belong to the larger campaigns (sizes 8–14), which look like recurring newsletter or notification series where one issue was assessed REVIEW. If that REVIEW is a false positive, its siblings are noise too. That is why they sit in the digest.

## Tests

`test_mail_security_campaigns.py` (7):
- near-duplicates match and unrelated texts don't;
- per-account keys;
- short texts are skipped;
- a flagged message never flags itself;
- repo join, isolation, replay and 30-day expiry;
- end to end: a LOW_PRIORITY sibling of a REVIEW message reaches the queue with `CAMPAIGN_OF_FLAGGED` and its disposition unchanged;
- inert without the key;
- the displacement regression.

Full suite: 8573 passed, plus the 1 known unrelated failure. e2e: 104 passed.

## Open

- Whether to keep `CAMPAIGN_OF_FLAGGED` at +2 or drop it to +1 (digest-only unless protected) if the digest gets noisy. This is a tuning call for the owner after some use.
