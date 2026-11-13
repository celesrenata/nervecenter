# Link calibration with a detection fix (NS-ADR-077) and "This was expected" feedback (NS-ADR-078)

Owner: "approved, let's keep going" (2026-11-13), covering the open items:
- link-text calibration;
- expectedness feedback with UI.

Channel grants (G8 item 4) stay deferred: nothing besides email is read, so there is nothing to grant.

Commits: `f1e7afba` (calibration and detection fix) and `d99d4658` (feedback). Migration head is `025_mail_expect_feedback`.

## 1. Display-host mismatch (NS-ADR-077, policy@5)

**A detection gap found while testing.** Link text that is a **bare domain**, such as `paypal.com` pointing to `evil.example`, raised **no finding at all**. `_url_host` only recognized text with a scheme, `//` or `www.`, so the most common deceptive-link shape was invisible.

The fix is a separate `_display_host`, used for link **text** only (`_url_host` is unchanged). It also accepts a whole-text bare domain: one token, letter/digit/hyphen labels, and an alphabetic TLD of 2–24 letters.
- **Not matched:** ordinary text such as "e.g.", "U.S.", "Click here", "v1.2.3" and "a.b".
- **Not matched:** filenames whose extension is not a TLD, such as `report.pdf` and `photo.jpeg`.
- **Still matched:** `.zip` and `.mov` stay domains, because they are real, abused TLDs.

**Calibration.** When the receiver reports DMARC pass for the From domain, a mismatched link whose **shown text or target is that same authenticated domain** no longer drives the disposition. That covers an ESP click-tracking redirect shown under the brand's own domain, or a link that lands on the authenticated sender. The finding is still recorded, with reason `DISPLAY_HOST_DMARC_ALIGNED`.

These cases keep REVIEW (all tested):
- a mismatch between two **other** domains;
- the aligned shape without an attested DMARC pass;
- a forged lower "pass" under a real fail.

**Live re-assessment.** `reassess_flagged --bare-domain-links` re-queued the 18 REVIEW messages and the **28 messages with a newly detected bare-domain mismatch**, 40 in total, as new revisions.
- **REVIEW: 18 → 9 (2.6% of 342).** Remaining drivers: display host differs (8, genuinely other-domain), Reply-To outside the safe conditions (4), IDNA confusable (1).
- **Calibration reasons now on file:** `DISPLAY_HOST_DMARC_ALIGNED` 35, `REPLY_TO_MISMATCH_DMARC_ALIGNED` 21.

## 2. Expectedness feedback (NS-ADR-078)

**API**, in `backend/api/endpoints/mail_security_expectedness.py`. It mirrors 7.2: resolved under the caller's own scopes (other accounts get a 404), human reviewers only (devices get a 403), an opaque reviewer hash, and the body accepts only an idempotency key.
- `POST /api/mail-security/assessments/{id}/expected`: one feedback per UNEXPECTED high-impact ask in that message. Returns 409 when there is none.
- `DELETE /api/mail-security/expectedness-feedback/{id}`: undo, by setting `revoked_at`; the row is kept for audit.
- `GET /api/mail-security/expectedness-feedback`: recent feedback, codes only.

**Storage.** `mail_expectedness_feedback` (025) holds the keyed sender, request type, weight 2, a 180-day expiry, the reviewer reference and `revoked_at`. Observations now also store the HMAC `expectedness_sender_key`. `seed_expectedness --forget` was re-run, so 158 observations carry it.

**Effect.**
- A live feedback adds pseudo-counts for that sender and type. It **never counts as history**, so a sparse sender stays ABSTAIN.
- The marked message leaves the review queue. Undo, or expiry, restores both the queue item and the score.
- It never marks anything safe and never changes a disposition.

**UI.** The Security review row for an `UNEXPECTED_HIGH_IMPACT_ASK` item gets a "This was expected" button, then "Undo", with a polite live status message. No other row gets the control.

**Tests.**
- `test_mail_security_expectedness_api.py` (8): round trip and undo, cross-account 404 for both mark and undo, 409 when nothing is unexpected, body limited to the idempotency key, devices refused, scoring shifts without history, expiry.
- e2e SR-4: the button appears only on unusual asks; mark sends POST and undo sends DELETE; status messages are announced.

**Checks.** Full suite: 8633 passed, plus the 1 known unrelated failure. e2e: 105 passed. Live, all three routes respond 401 when unauthenticated. There are currently 0 UNEXPECTED high-impact asks, so the button is not showing on any live item yet.
