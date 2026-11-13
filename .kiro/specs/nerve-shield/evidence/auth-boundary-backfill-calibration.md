# Trusted Gmail boundary, full-capture backfill and live calibration (NS-ADR-070/071/072)

Owner: "do em" (2026-11-13), for (1) trusting Gmail's own authentication results, (2) a full-capture backfill of stored mail, and (3) the semantic observer, which is tracked separately. Commits: `6b5f44bd` (1, 2) and `4fce27e4` (calibration), plus two style commits.

## 1. Topmost-only trusted receiver (NS-ADR-070)

**The risk.** Anyone can write `Authentication-Results: mx.google.com; dkim=pass` into their own message. Gmail prepends its real result **above** every header the sender wrote.

**The rule.**
- `TrustedReceiverBoundary(topmost_only=True)` attests only the **first** `Authentication-Results` header, and only if its authserv-id is configured. A lower header naming the same server stays `UNTRUSTED_HEADER` and cannot override the real result (trusted evidence wins per axis).
- `NERVE_MAIL_SECURITY_TRUSTED_AUTHSERV_IDS=mx.google.com` is set in the ConfigMap. Unset or malformed means the empty boundary, which attests nothing.
- Results become `PROVIDER_REPORTED`, never `INDEPENDENT`, so `SAFE_TO_PROCESS` remains unreachable.
- New finding `AUTH_PROVIDER_REPORTED_DMARC_FAIL` (MEDIUM): the receiver itself reported DMARC=fail.

**Checked on real mail.** All 27 raw messages captured live before the backfill carry exactly one Authentication-Results header, and it is `mx.google.com`. This matches the assumption.

**Tests.** `test_mail_security_trusted_boundary.py` (9). The key case: a forged lower mx.google.com "pass" under a real "fail" stays untrusted and does not upgrade the result.

## 2. Full-capture backfill as revision 2 (NS-ADR-071)

Verdicts are immutable per revision (a complete verdict is never overwritten), so re-acquisition creates a **new revision**:
- The worker takes `source_revision` from trusted job meta (an integer 1–100, default 1).
- Owner tool `backfill_raw_capture` targets body-only, incoming, DONE Gmail jobs. It fetches `format=raw` with the same 32 MiB cap and rate-limit backoff, and stores the encrypted `raw/<conn>/<digest>` object. It then re-queues the same job with `GMAIL_RAW`, a `raw_ref` and `source_revision=2`.
- `content_ref`, the 7.1 and 7.4 join key, is unchanged.
- The gate, safety card, queue loader and `latest_assessment_for_source` all take the newest revision. Ops distributions count every revision.

**Run.** The dry run found 40 + 275 = 315 eligible messages. The apply re-queued 315 with no Gmail errors, and all 342 jobs were DONE afterwards.

**Results.**
- DMARC: `PROVIDER_REPORTED`/PASS 312; `UNTRUSTED_HEADER`/PASS 26 (revision-1 verdicts written before the boundary existed); UNKNOWN 4.
- ClamAV scanned real attachments for the first time: `ENGINE_CLEAN_VERDICT_NOT_SAFETY` 58, `SCAN_NOT_RUN` 13.

**Tests.** `test_owner_backfill_raw_capture.py` (9): bounded revision parsing; revision 1 BODY_ONLY plus revision 2 ORIGINAL_RFC822 side by side; every reader sees revision 2; idempotent; a Gmail 404 leaves the job untouched.

## 3. Calibration on live data (NS-ADR-072, policy@4)

With headers and HTML visible, REVIEW went from about 12 to **102 of 342** (30%). Aggregated, in-pod and codes and domains only, the drivers were:

| Driver | Cause | Fix |
|---|---|---|
| `REPLY_TO_MISMATCH` on 70 messages | Compared **full hostnames**. Only 21 had a Reply-To on a different organization: 17 bulk and 4 not, **0 free-mail**. The rest were `news.brand.example` vs `brand.example`. | Compare registrable domains (Return-Path too). This is a bug fix. |
| The 21 real cross-organization Reply-Tos | Ticketing and ESP replies on senders Gmail reports as DMARC pass | Policy@4: such a mismatch doesn't count as impersonation **when the receiver reports DMARC pass and the Reply-To is not free-mail**. It is still recorded, with reason `REPLY_TO_MISMATCH_DMARC_ALIGNED`. |
| `ASCII_BRAND_TYPOSQUAT` (all hits) | `media-amazon.com` (17), `ssl-images-amazon.com` (4), `amazon.work` (4): Amazon's own image CDNs and its A to Z portal | Added to the first-party allow-list, which is owner-reviewable data (NS-ADR-026). `amazon-login.com`, `amaz0n.com` and `amazon-security.work` still flag. |
| `DISPLAY_HOST_DIFFERS_FROM_TARGET` (17) | The real deceptive-link shape, made noisy by marketing click-tracking | **Unchanged.** |

Cases that keep REVIEW (tested): a free-mail Reply-To on an authenticated From (the BEC shape), no attested receiver result, and a provider-reported DMARC fail.

**Re-assessment.** Owner tool `reassess_flagged` re-queued the 102 flagged messages as the next revision under policy@4.

**After:**

| Account | LOW_PRIORITY | REVIEW | INCOMPLETE |
|---|---|---|---|
| 53b03939 | 279 | 17 | 1 |
| fc4e3c57 | 44 | 1 | 0 |

Total REVIEW is **18 (5%)**. Remaining drivers: display-host differs 17, a Reply-To mismatch outside the safe conditions 5, IDNA confusable 1 (HIGH). Gate holds: 5 RELEASED, 0 held.

**Tests.** `test_mail_security_calibration_072.py` (5). The policy version test moved to @4. Full suite: 8596 passed, plus the 1 known unrelated failure.

## Open

- `DISPLAY_HOST_DIFFERS_FROM_TARGET` on marketing click-tracking. Calibrating it would need a list of tracking redirect domains, or a DMARC-aligned plus bulk rule. Left for the owner after seeing the queue.
- The thread ledger's AUTH_DOWNGRADE signal is now possible, because provider-reported DMARC exists, but it is not built: the ledger records at ingest time, before the security verdict.
