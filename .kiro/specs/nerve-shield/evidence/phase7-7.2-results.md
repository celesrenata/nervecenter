# Task 7.2 — Reviewer feedback and independent-verification advice results

Date: 2026-11-11. Status: done, local commit only, not deployed (adds migration 018).

## What changed

**Verdicts.** There are four verdicts, covering both spec wordings: R-020.2 says safe/suspicious/misclassified/not relevant, and task 7.2 says safe/suspicious/unimportant/wrong intent. `backend/mail_security/review_feedback.py` (pure) maps each verdict onto a 5.5 ledger kind using the disposition the reviewer saw:

| Verdict | Flagged (REVIEW/RESTRICTED/INCOMPLETE) | Not flagged |
|---|---|---|
| SAFE | FALSE_POSITIVE | CONFIRMED_SAFE (audit only) |
| SUSPICIOUS | CONFIRMED_THREAT | FALSE_NEGATIVE |
| NOT_RELEVANT | NOT_RELEVANT (audit only) | NOT_RELEVANT |
| WRONG_INTENT | CATEGORY_CORRECTION | CATEGORY_CORRECTION |

`CONFIRMED_SAFE` and `NOT_RELEVANT` are new ledger kinds. They route to no store.

**Storage.** `mail_security_feedback` (Alembic `018_mail_security_feedback`, additive and reversible) is an append-only table.
- It is unique by `(account_scope, feedback_id)`.
- It holds codes and opaque refs only: verdict, kind, source, a hashed `reviewer_ref` (`rev:` + sha256), the disposition seen, and the routed proposals.
- There is no free text.

**API.** `backend/api/endpoints/mail_security_feedback.py` is a separate router, so the read router stays GET-only.
- `POST /api/mail-security/assessments/{id}/feedback`:
  - The assessment is resolved under the caller's scopes only. Another account's id gets the same safe 404 as an absent id.
  - `account_scope` is copied from the assessment row.
  - The body is `{verdict, idempotency_key?}` with `extra="forbid"`, so an account, note, consent ref or indicator refs is rejected with 422 and nothing is written.
  - Only human access tokens can submit; device tokens get `403 reviewer_not_eligible`.
  - Routing goes through the 5.5 ledger rules. The proposals are recorded with `applied: false`; nothing is applied.
  - A replay with the same key returns 200 and the same record (no duplicate); a new record returns 201.
  - Every submission logs the `mail.security.feedback.recorded` audit event with codes only.
- `GET` on the same path lists feedback for that assessment in the caller's scope. Each entry has `mine` instead of a reviewer id.

**No implicit trust allowlist.**
- `SAFE` on a flagged verdict yields at most a `PERSONAL_MEMORY / ACCOUNT / PENDING_REVIEW` proposal.
- No intel override is written, and the verdict is unchanged.
- The API never accepts indicator refs, so client feedback never creates an intel proposal.

**Separation from training.**
- The API never accepts a consent ref, so every TRAINING proposal is `BLOCKED_NO_CONSENT`.
- 6.1 selection still needs an *APPROVED* proposal plus a consent grant. Nothing approves proposals yet.
- The feedback path imports no intel, trust-index, policy, model, training-selection, manifest or registry module, and an AST test enforces this.

**Advice (R-013.4).** The detail now carries `verification_advice` codes. They are derived from active (OBSERVED/SUSPECTED) finding categories and policy reason codes only:

| Signal | Advice |
|---|---|
| PAYMENT_FRAUD | VERIFY_PAYMENT_REQUEST |
| PHISHING | VERIFY_CREDENTIAL_REQUEST |
| SOCIAL_ENGINEERING or CREDENTIAL_OR_PAYMENT_ASK, with neither of the above | VERIFY_HIGH_IMPACT_REQUEST |
| SENDER_IMPERSONATION | VERIFY_SENDER_IDENTITY |

The advice is shown even when every authentication result passes.

**UI.**
- The card shows a "Verify independently" box with fixed text per code, for example "Do not use contact details from this message".
- A new `MailSecurityFeedback` component is a `role=group` labelled "Your review", rendered *outside* the card's `role=status` region, so the card stays display-only.
- It has four `aria-pressed` buttons and no text input.
- It states that feedback does not trust the sender, move mail or approve anything.
- After a submission it says "Nothing was trusted or changed". A failure is announced in a `role=alert` and the previous choice is kept.
- It is shown only when an assessment exists.

## Verification

| Check | Result |
|---|---|
| `tests/backend/test_mail_security_feedback_api.py` (new) | 26 passed |
| Store-boundary tests (allow-list widened to exactly the two 7.2 files, plus a new no-store/no-training import test) | pass |
| Migration head tests updated to `018_mail_security_feedback`; 018 up/down on SQLite incl. unique index | pass |
| Full suite `uv run pytest tests/ -m "not integration" -q` | 8297 passed, 1 failed (known date-dependent `test_routines`), 3 skipped |
| `ruff` on new files | clean; touched files have the same pre-existing findings as HEAD |
| `npm run build` | ok |
| Full Playwright e2e | 98 passed |

The new backend tests cover:
- Cross-account POST returns 404 and writes nothing.
- The same assessment id existing in two accounts stays separated.
- Cross-account GET returns 404.
- Body extras (account, note, consent, indicator refs) are 422.
- Unknown verdict and bad idempotency key are 422.
- Device token is 403; unauthenticated is 401.
- `SAFE` on RESTRICTED: `FALSE_POSITIVE`, a PENDING memory proposal, BLOCKED training, 0 overrides, disposition unchanged.
- `SUSPICIOUS` on SAFE: `FALSE_NEGATIVE`, with no intel proposal.
- `NOT_RELEVANT` routes nowhere.
- Replays are idempotent.
- The reviewer is stored hashed.
- The import boundary holds.
- The verdict→kind table and the advice rules.
- Payment advice appears when every auth result passes.

The new e2e tests:
- **SC-5:** advice text with all auth passing, the feedback group outside the card (the card has 0 buttons, the group has 4, and there is no text input), keyboard submit, a POST body of exactly `{idempotency_key: 32 hex, verdict}`, `aria-pressed`, the "Nothing was trusted" status, and an unchanged disposition.
- **SC-6:** existing feedback is pressed on load; a 500 on submit is announced and the previous choice stays.
- **SC-4** now also asserts no feedback controls appear without an assessment.

## Not verified / limits

- Migration 018 has not run on PostgreSQL yet. It is a single additive table, and the deploy's migrate Job runs `alembic upgrade head`. Rollback is `alembic downgrade 017_mail_intel`, and an older image ignores the table.
- Nothing consumes or approves feedback proposals. That executor, and any UI for approving them, is out of scope for 7.2.
- Advice uses deterministic finding categories and reason codes, not the Granite request-type observation, so a payment ask that produced no PAYMENT_FRAUD finding gets no advice.
- Accessibility was checked through Playwright role, name, aria and keyboard assertions only. Full WCAG conformance needs manual assistive-technology testing and an expert review.
