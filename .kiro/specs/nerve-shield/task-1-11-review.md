# Task 1.11 Review — NerveShield read-only UI security indicator

Verdict: **APPROVED**

## Scope reviewed
`git diff e9d18e5 -- web/`. Four source files changed (plus npm-internal
`node_modules/.package-lock.json` and two `.pyc` bytecode caches — not source):

- `web/src/api/mailSecurity.ts` (new thin client)
- `web/src/components/MailSecurityIndicator.tsx` (new component)
- `web/src/views/EmailView.tsx` (one edited view)
- `web/tests/e2e/mail-security-indicator.spec.ts` (new Playwright spec)

## Invariant checks (all pass)

**Safe rendering (R-020.3, T-05, T-11).** No `dangerouslySetInnerHTML`
anywhere — grep over the three code files returns matches only inside doc
comments, never in code. No `<img>`, `<iframe>`, `<script>`, `url(`,
`background-image`, `@font-face`, link prefetch, or `new Image`/`innerHTML` in
the component. All assessment-sourced strings (disposition, reason codes,
`checks_not_run`/`missing`, provenance) render as inert React-escaped children
(`{style.label}`, `{code}`, `{style.description}`). The injection-indicator
code `PROMPT_INJECTION_SUSPECTED` is displayed literally as a chip, not
interpreted (the e2e asserts it renders as text). The only network call the
component can cause is `apiFetch` to the same-origin Task 1.10
`/api/mail-security/assessments…` route; the e2e asserts zero
remote/third-party-origin requests and zero page errors.

**Reading != authorizing (R-020.4).** The component renders no action control:
no move/mark/delete/approve/trust button, no "trust this sender" control. It
adds an explicit "Observation only — reviewing this does not approve or
authorize anything" line. The pre-existing "Mark as read" / "Draft reply"
buttons in EmailView are untouched email actions, not security-authorization
controls, and are out of scope.

**SHADOW + graceful degrade.** Component states SHADOW posture explicitly (badge
+ accessible-name suffix "observation only, not enforced"). The client fails
closed: `listAssessments` returns an empty list and `getAssessment` returns
`null` on 401/403/404, and both mock implementations return empty. EmailView
wraps the lookup in its own try/catch and treats a missing assessment as
`null`, rendering the neutral "Not assessed" (UNKNOWN) state with no error
banner. The conversation render is independent of the security lookup, so a
security failure never breaks the view. The e2e's fail-closed test confirms the
thread still renders, the indicator is neutral, and no `role="alert"` appears.

**Client/auth.** `mailSecurity.ts` routes every call through the shared
`apiFetch` from `client.ts` (same transport as `communications.ts`/`today.ts`),
reuses `apiErrorFromResponse` and `useMockMode`, and introduces no new auth path
or credential destination. Only the Task 1.10 `/api/mail-security` endpoints are
called. GET only — no writes.

**Accessibility.** `role="status"` + `aria-live="polite"` + a descriptive
`aria-label` carrying the disposition label and SHADOW qualifier. State is
conveyed by text + an inert `aria-hidden` glyph, never color-only; the glyph is
marked decorative so the accessible name is authoritative.

**Scope.** Footprint is minimal: one small component + one thin client + one
view edit + one test. No view redesign. `package.json`/`package-lock.json`
are unchanged (no new declared frontend dependency); the only node_modules
delta is an npm-internal Playwright patch bump 1.63.0→1.64.1 in
`.package-lock.json`. No vitest/jest introduced. No `.py` source changed
(`git diff --name-only e9d18e5 | grep '.py$'` is empty). Task 1.12+ not begun.

**Data preservation (R-017.2, T-14).** Client wire types expose only redacted
metadata and opaque refs (codes, severities, provenance labels, calibration
status, checks-not-run). No raw body/subject/address field exists in the types,
and the UI surfaces none.

## Verification evidence (R-025) — read from the commit message, not re-run
- `npm run build` (tsc + vite): **PASS** (pre-existing chunk-size advisory only).
- `npm run lint`: **NOT RUN**, honestly disclosed. Verified the reason: the
  `lint` script invokes `eslint`, but eslint is not a declared devDependency and
  no `eslint.config.*`/`.eslintrc.*` exists in `web/`. Running it would require
  adding unrequested dependencies — pre-existing toolchain debt, not introduced
  here. In lieu, a code-level safe-rendering grep assertion was recorded.
- Playwright e2e: **RAN** — `mail-security-indicator` 2 passed; `views.spec.ts`
  20 passed (regression incl. /email). This satisfies the Task 1.11 Done
  criterion ("existing views and relevant web tests pass").

The NOT-RUN statement is accurate and verifiable, not a stub or fabrication.
The build + e2e evidence covers the Done criterion.

## Blockers
None.
