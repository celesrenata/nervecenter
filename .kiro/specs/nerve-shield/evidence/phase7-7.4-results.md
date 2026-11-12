# Task 7.4 — Pre-promotion trust gate results

Date: 2026-11-11. Status: done, local commit only, not deployed. Adds migration 019. **Gate is OFF** (flag unset in the ConfigMap); no flag was changed.

## Traced promotion path (current code, not the Nov 5 map)

The mail worker's `_process_batch` runs these steps, in this order:

1. `object_store.put`
2. `ingest_communication` (in-memory)
3. `persist_durable_communication`
4. `register_content`
5. `CommunicationPipeline.ingest`
6. `_schedule_security_job`

Inside `ingest`, the steps run in this order:

1. **`detect`** feeds the **in-memory** `AttentionService` (`/api/attention`).
2. **`_persist_commitments`** writes durable `commitments` `ct-<sha(kind:finding_id)[:12]>`. It is gated into the cursor through `flush_durable`.
3. **`_persist_attention`** writes durable `attention_items` `at-<sha(kind:finding_id)[:12]>`. This is what Today reads. It is best-effort.
4. **Follow-ups** are created only for OUTGOING findings with a due time.

The security verdict arrives later, asynchronously, from the runner. A check *after* promotion would therefore not stop a malicious obligation.

## What changed

### `backend/mail_security/trust_gate.py`

- **Mode:** `REVIEW_GATED` requires both of these; everything else is OFF:
  - `NERVE_MAIL_SECURITY_PROMOTION_GATE` set exactly to `review_gated` (case and whitespace insensitive);
  - mail security ON.
- **Scope:** only `content://mail/<uuid>/<24hex>` refs are gated (scope `conn:<uuid>`).
- **Decisions:**
  - `SAFE_TO_PROCESS` / `LOW_PRIORITY` → RELEASE.
  - `REVIEW` / `RESTRICTED` / `INCOMPLETE` → REVIEW_CANDIDATE.
  - No verdict → WAIT.

### `CommunicationPipeline`

- **New field:** `promotion_gate=None`. With no gate bound, every branch is the pre-7.4 code path.
- **Gated messages:**
  - The durable attention and commitment writes become `gate.hold(...)` calls under the exact deterministic id, with the exact templated columns.
  - The in-memory items detection just fed are suppressed.
  - An `applies()` exception holds (fail closed).
  - A hold failure is logged and never falls back to the actionable write.
- **Startup binding:** `set_promotion_gate` is called only when the mode is `REVIEW_GATED`.

### Durable holds

- **Table:** `mail_security_held_promotions` (Alembic 019, additive, reversible), unique on `(target_kind, target_id)`.
- **Hold:** `HeldPromotionRepository.hold` is get-or-create. A replay never stacks a hold and never re-holds a RELEASED or REVIEW_CANDIDATE item.
- **`release_pass`:**
  - Finds the newest assessment for the job with the same exact ref in the hold's own scope.
  - On RELEASE, it replays the original write with the same id through the real `AttentionRepository` / `CommitmentsRepository` (both get-or-create), then marks the hold RELEASED.
  - On REVIEW_CANDIDATE, it marks the hold and records the disposition and assessment id.
  - On WAIT, it leaves the hold alone.
  - Because the write happens before the mark, a crash between the two is repaired on the next pass without a duplicate.
  - A later reassessment can release a candidate; feedback alone cannot.

### Runner and queue

- The runner's `post_pass` hook runs the release pass after each pass, only when the gate is ON. Failures are counted as `RELEASE_PASS_FAILED` and never affect processing.
- The 7.3 queue loader lists HELD / REVIEW_CANDIDATE holds for a message as `held_attention` / `held_commitment` REVIEW_CANDIDATE refs.

## Bypass paths (enumerated; not gated by design)

| Path | Why not gated | Risk |
|---|---|---|
| Non-mail channels (iMessage/SMS/voicemail/RCS) | no NerveShield source or verdict exists | out of NerveShield scope |
| Authenticated `POST /api/communications/ingest` with a non-mail `content_ref` | owner-authenticated, not email-origin; no verdict would ever arrive | owner-controlled |
| Follow-ups (`_ensure_followup` → follow-up service mirrors) | created only for OUTGOING findings with a due time; the mail worker ingests INCOMING only | none on the mail path; an authenticated caller posting OUTGOING with a mail ref is owner-controlled |
| `DetectionService.sweep_overdue` re-feeding in-memory attention | no production caller (tests only); if wired later it could re-feed a suppressed in-memory item | latent; durable Today is unaffected because durable writes are held |
| Marketing-gated messages | detection yields zero findings, so nothing is promoted | none |

## Verification

### Results

| Check | Result |
|---|---|
| `tests/backend/test_mail_security_trust_gate.py` (new) | 22 passed |
| Mutation check: hold branch disabled | 10 of those tests fail (teeth confirmed), then restored |
| Existing `test_attention_durable.py`, `test_detection.py`, pipeline wiring (closed failure-code set extended by `RELEASE_PASS_FAILED`), repo/intel head tests (now `019`), schema table registry | pass |
| Full suite `uv run pytest tests/ -m "not integration" -q` | 8338 passed, 1 failed (known date-dependent `test_routines`) + the closed-set test that was then fixed and re-run (89/89) |
| `ruff` | new files clean; touched files have the same findings as HEAD |
| Playwright full e2e | 101 passed |

### Done-criterion tests

**Malicious email cannot auto-mint an actionable obligation:**
- With a RESTRICTED verdict, ingest still detects findings, but there are 0 `attention_items`.
- The in-memory items are all SUPPRESSED.
- The hold becomes REVIEW_CANDIDATE.
- `/api/today` `needs_you` is empty.

REVIEW and INCOMPLETE behave the same way.

**Safe mail still can:**
- SAFE_TO_PROCESS and LOW_PRIORITY release.
- The released row's id and columns are identical to the same message ingested with the gate OFF in a separate database.

**No duplicate or replayed tasks:**
- Replays in-process and after a simulated restart keep 1 hold and 0 rows (malicious) or 1 row (safe).
- A second release pass has nothing to do or reports `unchanged`.

### Other tests

- `gate_mode` is OFF for unset, `review_gated` without mail security, `enforce`, and `1`.
- `get_pipeline()` has no gate by default.
- With the gate OFF, the row is written at ingest.
- Non-mail refs are not gated.
- No verdict keeps the hold.
- Another scope's SAFE verdict never releases it.
- A reassessment from REVIEW to SAFE releases.
- A hold failure means no actionable write and no crash.
- An unsure gate holds.
- A commitment from OUTGOING mail is held and released with the exact `ct-` id.
- The runner hook is called, and its failure is counted.
- Migration 019 up/down works, including the unique index.
- Holds show in the 7.3 queue as `held_attention` REVIEW_CANDIDATE.

## Not verified / limits

- **Gate not exercised live.** It stays OFF until the owner explicitly opts in (7.8 canary, gated by G7). Migration 019 will run on deploy and create an empty table.
- **The `/api/attention` in-memory item stays suppressed after release.** The durable Today row is the released artifact. The in-memory queue is non-durable and rebuilt on restart.
- **Held items wait while the security pipeline is down.** With the gate ON, mail-derived items wait until a verdict exists. That is the intended fail-closed cost. They are visible as holds, but there is no dedicated "held, no verdict yet" count in the UI.
- **Release latency is one runner interval** after the verdict commits.
- **Held payloads have the same sensitivity as the target tables:** templated titles that include the sender string, and no body text.

## Deploy incident (fixed forward)

- **What failed:** the first deploy of `eeb544a` failed in the migrate Job with `value too long for type character varying(32)`. The revision id `019_mail_security_held_promotions` is 33 characters, while Postgres `alembic_version.version_num` is VARCHAR(32).
- **Why tests missed it:** SQLite does not enforce VARCHAR lengths, and the local up/down test used a 40-wide column.
- **Database state:** Postgres DDL is transactional, so the database stayed at `018_mail_security_feedback` with no partial table.
- **Impact:** `api` and `web` had already rolled to `eeb544a`. For about 10 minutes, `GET /api/mail-security/review-queue` failed because the 7.3 loader queries the missing table. With the gate OFF, nothing else touched it.
- **Fix (`de7ab2d`):**
  - Renamed the revision to `019_mail_held_promotions` (never applied anywhere, so the rename is safe).
  - Added `test_every_revision_id_fits_alembic_version_column`, which guards every revision id at 32 characters or fewer.
- **Verified after the redeploy (counts only, read in the pod):**
  - images `de7ab2d`;
  - `alembic_version` = `019_mail_held_promotions`;
  - `mail_security_held_promotions` has 0 rows;
  - the review-queue loader returns 4 items;
  - `gate_mode()` is OFF and `promotion_gate_bound` is never logged;
  - no failed migrate pods remain.
