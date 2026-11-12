# Task 7.1 — Safety card (R-020) results

Date: 2026-11-11. Status: done, local commit only, not deployed.

## What changed

Backend (read-only, GET only):

- `MailSecurityRepository.source_key_for_content_ref(scope, ref)`: the job `source_key` scheduled for one opaque `content_ref`, scope-enforced.
- `MailSecurityRepository.latest_assessment_for_source(scope, source_id)`: the newest assessment for a source (highest revision, then newest row).
- `GET /api/mail-security/assessments/for-communication/{communication_id}`:
  - The id must match `comm-mail-<24 hex>`.
  - The communication's `content_ref` must parse strictly as `content://mail/<uuid>/<24 hex>`, and its digest must equal the id's digest. Both are `sha256(conn:mail_id)[:24]`, which binds the row to its id.
  - `conn:<uuid>` must be one of the caller's scopes (claim, or the `single_tenant` server-resolved set).
  - It then looks up the job for that exact ref, takes the job's `source_key`, and returns the newest assessment for that key.
  - It returns the same redacted `_redact_detail` projection as the detail route.
  - Every miss returns the same `404 {"error_code":"not_found"}`.

Frontend:

- `mailSecurityApi.getForCommunication(id)`: resolves to `null` on 401, 403 or 404; the mock client returns `null`.
- `MailSecurityIndicator` is extended into the safety card. It loads by `assessmentId` or by `communicationId` and shows:
  - Disposition text and glyph, the SHADOW badge, and an evidence-fidelity badge (for example "Body text only").
  - Reason codes.
  - "Checks not run" (`checks_not_run` ∪ `missing`), each with a `NOT_EVALUATED` badge.
  - A native `<details>` "Provenance and explanation" section containing:
    - SPF/DKIM/DMARC/ARC results with provenance labels. `UNKNOWN` or null shows `NOT_EVALUATED`.
    - Per-axis confidence semantics: calibrated %, "uncalibrated estimate (not a probability)", or `NOT_EVALUATED`.
    - Findings: rule, severity, status and explanation code.
    - The R-020.4 statement that review, verifying the sender and approving an action are separate steps, and that nothing here approves anything.
- `CommunicationsView`: email rows get a "Show security card" toggle button (`aria-expanded`/`aria-controls`). The card and its single GET mount only when the toggle is opened, so a results list never fans out N requests.
- `EmailView` defect removed. Task 1.11 called `listAssessments({limit:1})` and showed the first row, which is the oldest assessment in the scope, on every relay thread. Relay threads have no link to a committed mail source, so the card now always shows "Not assessed" there and the view makes no mail-security request.

No flags changed. No model, no network beyond the same-origin GET, and no write.

## Verification

| Check | Result |
|---|---|
| `uv run pytest tests/backend/test_mail_security_api_for_communication.py` (new, 11 tests) | 11 passed; repeated 6× with no flake |
| Existing API tests (`test_mail_security_api.py`, `_scope_mode.py`) | pass (incl. `test_only_get_routes_exposed`) |
| Full suite `uv run pytest tests/ -m "not integration" -q` | 8270 passed, 1 failed (known date-dependent `test_routines::TestWellnessSleepSeam`), 3 skipped |
| `ruff check` on changed Python | clean |
| `npm run build` (tsc + vite) | ok |
| `npx playwright test` full e2e | 96 passed |

The new backend tests cover:

- The newest assessment for the communication's own source is returned. An older assessment of a different source in the same scope is never returned.
- A cross-account communication returns a 404 whose body is identical to the 404 for an absent id.
- A job for the ref that exists only in another scope is never used.
- Malformed ids return 404, including a `../` path attempt.
- A ref/id digest mismatch returns 404.
- An unscheduled or unassessed communication returns 404.
- No scopes returns 404; unauthenticated returns 401.
- A subject canary never appears in the response.

New e2e `web/tests/e2e/mail-safety-card.spec.ts`:

- SC-1 (accessibility):
  - The toggle is keyboard-operated (focus + Enter) with `aria-expanded`/`aria-controls`.
  - The card is a `role=status` region whose accessible name carries the disposition and the observation-only context.
  - `<details>` opens from the keyboard.
  - Non-email rows get no toggle.
  - No mail-security request is made before the card is opened.
- SC-2 (content): every element listed above is asserted, including the `NOT_EVALUATED` badge counts.
- SC-3 (security rendering):
  - `<img src=https://evil.example/... onerror=...>` in a reason code and `<script>` in a finding rule id render as literal text.
  - No `img/script/iframe/object/embed` element exists inside the card.
  - `window.__pwned` is undefined.
  - No request goes to any other origin.
  - The card contains no buttons and no links.
- SC-4 (fail closed): a safe 404 shows "Not assessed", with no details section and no alert banner.

The updated `mail-security-indicator.spec.ts` regression test serves an unrelated assessment in the list. The Email view still shows "Not assessed" and makes no mail-security request.

## Live-data check (read-only, counts only)

These counts were taken in the `api` pod against the durable database. They show the resolution path works on real rows:

| Query | Count |
|---|---|
| jobs | 625 |
| jobs with `meta.content_ref` | 625 |
| jobs joined to `communications.content_ref` | 625 |
| communications reaching ≥1 assessment via job `source_key` | 625 |
| mail communications whose id digest equals their ref digest | 625 / 625 |

## Not verified / limits

- Not deployed. The live endpoint will exist after the next `deploy.sh`; until then the deployed web build has no toggle.
- Accessibility was checked by Playwright role, name, attribute and keyboard assertions only. Full WCAG conformance needs manual testing with assistive technology and an expert review.
- Relay (`/api/email`) threads still have no assessment link by design. Mapping relay threads to committed mail sources is not part of 7.1.
- The dev-server StrictMode double effect causes two identical GETs on open in dev; the first is cancelled. Production builds issue one.
