# G7 gate — owner decision required (STOP)

Date: 2026-11-12. Phase 7 tasks 7.1–7.8 are complete. Per R-025 and task 7.9, the owner must explicitly **approve or reject** each non-shadow activation below. Nothing further is activated until then.

## Current production state

**Images:** `api` / `web` at `f230bb13`.

**Schema:** at `020_mail_confirmations`.

**ConfigMap:**

| Key | Value |
|---|---|
| `NERVE_MAIL_SECURITY_ENABLED` | `1` |
| `NERVE_MAIL_SECURITY_API_SCOPE_MODE` | `single_tenant` |
| `NERVE_MAIL_SECURITY_ARCFORGE_URL` | 2 hosts, read-only status |
| `NERVE_MAIL_SECURITY_PROMOTION_GATE` | `review_gated` (**canary ON**, owner opt-in in 7.8) |
| `NERVE_MAIL_SECURITY_PROMOTION_GATE_SCOPES` | both Gmail accounts |

**Assessment mode:** `SHADOW` for every verdict. No provider mail is changed.

**Models:** no ML model is in SHADOW, CANARY or ACTIVE (G6). PROXY_DATA_ONLY stays strict.

## What Phase 7 delivered

| Task | Result | Evidence |
|---|---|---|
| 7.1 | Safety card on mail rows (provenance, fidelity, NOT_EVALUATED, inert rendering); fixed the Email-view wrong-assessment defect | `phase7-7.1-results.md`, NS-ADR-053 |
| 7.2 | Scoped reviewer feedback (codes only, no trust allowlist, training blocked); verification advice | `phase7-7.2-results.md`, NS-ADR-054 |
| 7.3 | Security review queue (impact score, interrupt budget, quiet hours, commitments preserved) | `phase7-7.3-results.md`, NS-ADR-055 |
| 7.4 | Pre-promotion trust gate (hold → release / review candidate, exact ids, no duplicates); deploy incident fixed | `phase7-7.4-results.md`, NS-ADR-056 |
| 7.5 | Capability-scoped task proposals + existing-PDP adapter | `phase7-7.5-results.md`, NS-ADR-057 |
| 7.6 | Explicit confirmations; high risk needs a ≤10-min step-up plus sender attestation; exact single-use approvals | `phase7-7.6-results.md`, NS-ADR-058 |
| 7.7 | Operations dashboard (health, queue, Arc service, versions, model runs, FP rate, holds, confirmations) | `phase7-7.7-results.md`, NS-ADR-059 |
| 7.8 | Adversarial review, rollback rehearsal on an in-cluster prod copy, per-account canary live on both accounts | `phase7-7.8-results.md`, NS-ADR-060 |

## Decisions for the owner

1. **Keep the REVIEW_GATED canary ON** for both accounts?
   - What it does: Today items and commitments derived from mail wait for a verdict. Safe and low-priority items are released with the same id; flagged items stay in the review queue.
   - Observed live: 2 messages so far, both released correctly in about 29 s, with 0 duplicates and 0 failures. The flagged path has **no live event yet**; it is tested only.
   - Alternatives: keep observing longer, narrow it to one account, or turn it off (rollback runbook in 7.8).
2. **Approve any activation beyond the canary?** None is proposed or built:
   - `NOTIFY` / `ENFORCE` assessment modes stay unreachable in code (policy `_MODE = SHADOW`).
   - No ML model enters SHADOW/CANARY/ACTIVE (G6 still stands).
   - No proposal producer, executor or confirmation UI is wired. Payments, sending and external disclosure stay locked.
3. **Open owner items carried forward** (these block a wider activation, not the canary):
   - ArcForge, Granite and OmniRoute endpoint authentication.
   - Object-store encryption (raw capture stays OFF).
   - OmniRoute memory-write absence, which is still unproven.
   - The live Keycloak step-up check (`max_age` → `id_token.auth_time`).
   - PROXY_DATA_ONLY, which stays strict.
   - Optional: send a test email that should be flagged, to see the review-candidate path live.

## Verification summary

**Tests:**
- Full suite: 8450 passed, with 1 known date-dependent failure.
- NerveShield release gate: 6682 passed.
- Real-Postgres concurrency: 7/7.
- Playwright: 104/104.

**Rehearsals:**
- Migrations 020 → 017 → 020 on a production copy kept all 10 core tables identical.
- The old image reads the new schema. Its migrate fails until the schema is downgraded, which led to runbook A/B.

**Limits:**
- WCAG conformance was not assessed beyond automated role, aria and keyboard checks.

## Owner decision (recorded 2026-11-12)

The owner wrote: "didn't we already agree to keep the canaries on". This is recorded as **APPROVED: keep the REVIEW_GATED canary ON for both accounts**. It confirms the 7.8 opt-in ("2. yes").

**No other activation is approved.** These stay as they are:
- Assessment mode: SHADOW.
- No ML model is activated (G6 stands).
- No proposal producer, executor or confirmation UI is wired.
- Payments, sending and disclosure stay locked.

The open owner items listed above carry forward.
