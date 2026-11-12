# Task 7.3 — Security review queue results

Date: 2026-11-11. Status: done, local commit only, not deployed. No migration, no flag change.

## What changed

### Queue engine

`backend/mail_security/review_queue.py` is a pure, deterministic engine (`mail-review-queue@1`). It does no writes, no notifications and no attention minting.

- **Entry:** only REVIEW, RESTRICTED or INCOMPLETE verdicts that have no feedback yet. Reviewed items are counted in `excluded_reviewed`.
- **Impact score:** a transparent additive score, built from:
  - a disposition base: RESTRICTED 3, REVIEW 2, INCOMPLETE 1;
  - +2 for payment or credential verification advice;
  - +1 for an active HIGH or CRITICAL finding (NOT_EVALUATED findings don't count);
  - +1 when the sender is a VIP person (R-013.5 VIP semantics kept);
  - +1 when the message has linked commitments.
- **Tiers:** a score of 6 or more is CRITICAL, 4 or more is HIGH, 2 or more is MEDIUM, and anything lower is LOW.
- **Attention budget:**
  - Only CRITICAL items that arrived today can be `INTERRUPT`. At most `daily_interrupt_cap` (default 1) qualify per day, earliest arrival first.
  - Older CRITICAL items are marked `NOT_NOVEL`; CRITICAL items beyond the cap are marked `INTERRUPT_BUDGET_EXHAUSTED`.
  - During an active local quiet period, nothing interrupts. Those items stay queued with `deliver_after` set to the end of the quiet period.
- **Digest:**
  - LOW items go into an aggregated digest (counts by primary reason code).
  - Beyond `visible_cap` (default 10), unprotected MEDIUM items overflow into the digest.
  - CRITICAL, HIGH, important-human and commitment-carrying items always stay visible.
- **Commitments behind restricted mail:** open commitments (`source_communication_id`), open follow-ups and active attention items whose refs name the communication are kept on the queue item as `REVIEW_CANDIDATE` ids. They are never promoted or acted on.

### Loader

`backend/persistence/repositories/mail_security_review_repo.py` is read-only. It reads ids, codes, flags and times only; never subject, body, address or commitment text. It works like this:

- Every join is bound to the scope.
- It keeps only the newest revision per source.
- The job's `content_ref` must sit under the scope's own connection.
- The `vip` flag comes from the communication's person.

### API

`GET /api/mail-security/review-queue` sits on the read-only router.

- It uses the caller's scopes only; no scopes means a 404.
- Quiet hours come from the existing local `AttentionPolicyService` quiet periods.
- It returns the visible items, the digest counts, `excluded_reviewed`, `quiet_until` and the policy version.

### UI

`/security-review` ("Security review" in the nav) shows:

- tier, disposition and plain-language reason labels;
- a "New and high impact" badge only for `INTERRUPT` items;
- "Held until …" during quiet hours, with a quiet-hours banner ("nothing is dropped");
- commitments as "Kept for review, not acted on: N commitment(s), N followup(s)";
- a grouped digest;
- a per-row security-card disclosure.

The only buttons are Refresh and the disclosure toggles. A safe 404 shows "Nothing needs review".

## Verification

| Check | Result |
|---|---|
| `tests/backend/test_mail_security_review_queue.py` (new) | 20 passed |
| Full suite `uv run pytest tests/ -m "not integration" -q` | 8317 passed, 1 failed (known date-dependent `test_routines`), 3 skipped |
| `ruff` on new files | clean |
| `npm run build` | ok |
| Playwright full e2e | 100 passed + SR-1 fixed and re-run (3/3 in the new spec) |

### Quiet-hour tests

- An active quiet period defers the interrupt and keeps every item visible.
- An expired period does not defer.
- A VIP CRITICAL item during quiet hours is queued, not interrupting, and gets `deliver_after`.
- API: adding a sleep period through `get_attention_policy()` flips the same item from INTERRUPT to QUEUE with `INTERRUPT_DEFERRED_QUIET_PERIOD`.

### Important-human tests

- A VIP INCOMPLETE message (the weakest verdict) buried in 40 noise items with `visible_cap=0` stays visible while all 40 go to the digest.
- A VIP RESTRICTED message ranks above an unknown sender's.
- API: a VIP join from durable `people.vip`.

### Other tests

- Only unreviewed flagged verdicts enter the queue.
- Tier scoring is correct.
- NOT_EVALUATED findings add no impact.
- Order is deterministic.
- The interrupt budget picks the earliest arrival first, and a cap of 0 disables interrupts.
- An old CRITICAL item never interrupts.
- Commitments are preserved as REVIEW_CANDIDATE and protected from the digest.
- Overflow only affects unprotected MEDIUM items.
- A naive clock is rejected.
- The API joins commitments and follow-ups by communication.
- The API keeps scope separation: no SAFE item and no other account's item appears.
- Subject, commitment and follow-up text canaries never appear in the response.
- Reviewed items leave the queue.
- No scopes returns 404.

### E2E tests

- **SR-1:** order, labels, commitment counts, digest, only non-acting buttons, keyboard disclosure, no remote requests.
- **SR-2:** quiet banner, held items, no interrupt badge, nothing dropped.
- **SR-3:** fail closed.

## Live dry run (read-only, counts only)

The new modules were loaded from `/tmp` in the `api` pod (image `d900467`) against the production DB:

| Measure | Value |
|---|---|
| scopes | 2 |
| queue candidates | 4 (all REVIEW, MEDIUM, QUEUE) |
| linked to a communication | 4 / 4 |
| VIP senders / with commitments | 0 / 0 |
| load time | 58 ms |
| global tables scanned | follow-ups 0, attention items 108, commitments 0 |

The temp files were removed afterwards.

## Not verified / limits

- **Interrupts are advisory.** `INTERRUPT` sends nothing: under SHADOW, no notification path is wired, and that would need an explicit NOTIFY mode change (R-012.2).
- **Quiet periods aren't durable.** They come from the in-process `AttentionPolicyService` and are lost on pod restart. A durable quiet-hours setting was not part of 7.3.
- **Full-table matching.** Follow-up and attention ref matching scans open or active rows in Python. That's fine at today's sizes (108 rows) but would need a ref index if those tables grow large.
- **"Important human" means `people.vip` only.** A frequent-correspondent heuristic was not added.
- **Accessibility** was checked through Playwright role, aria and keyboard assertions only. Full WCAG conformance needs manual assistive-technology testing and an expert review.
