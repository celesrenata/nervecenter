# P1 Tasks — Zoo Parallel Execution Pack

Baseline SHA: `fb774d27a957cdfc25744b9bb086fe6b1f4d21c2`. Work begins after contracts are frozen. **Do not mark items complete for tests you did not run.** Primary task is real production activation, not more mock implementations.

## Wave 0 — Read-only production readiness audit

- [ ] **P1-001** Check deployed image SHAs, ingress/SSO, existing account count, database migration head, NATS, object storage, vault availability, ingest/calendar flags, actual mobile binary versions; redact all credentials. Record safe baseline and rollback plan. No deployment modifications.
- [ ] **P1-002** Inventory *real* API paths used by the Connection wizard, relay, Today and backend workers. Write a gap matrix for working / disconnected / only-synthetic / unsafe.
- [ ] **P1-003** Freeze the credential, per-user ownership, sync-trigger, worker lifecycle, durable ingest, and health contracts. Include threat model for personal-data leakage/CSRF.
- [ ] **P1-004** Obtain explicit operator approval before running live account imports, touching user mail, configuring Full Disk Access, or testing physical notifications. Test fixtures must never require real personal content.

## Wave 1 — Independent code changes after contract freeze

- [ ] **P1-101** Implement authenticated HTTPS `POST /api/connections/{id}/credentials` for generic IMAP only; validate owner + source kind; one-use password, Fernet-only storage, never echo; reject if vault unavailable. Add negative tests for wrong owner/kind, absent key, no plaintext anywhere.
- [ ] **P1-102** Wire an IMAP password form into `AddConnectionWizard` with transient component state, masked input, visibility toggle and no local persistence. Verify from UI; remove backend-CLI instructions.
- [ ] **P1-103** Fix production control-plane construction to register `ImapImapClient`, real sync triggers and provider-specific OAuth flow (where supported). Align owner/CSRF validation; eliminate wildcard credentialed CORS.
- [ ] **P1-104** Share one Fernet-backed secret repository between control plane and mail worker; ensure vault absence fails closed for any secret-dependent path. Add encrypted-secret round-trip integration test against PostgreSQL.
- [ ] **P1-105** Add `ensure_worker`/`stop_worker` reconciliation for create/enable/disable/delete, and wire Sync Now/Import to actual incremental processing without API restart; no parallel duplicate worker for same account.
- [ ] **P1-106** Harden Gmail OAuth scopes, PKCE, callback-state verification, refresh and token expiry. Validate a Google account via a live, consented staging test. Ensure Graph provider is not represented as healthy while unsupported.
- [ ] **P1-107** Add source-to-durable-communication/commitment/attention transaction or reliable outbox boundary. Fail before cursor commit if downstream persistence failed. Add crash-point/replay/dedup tests.
- [ ] **P1-108** Implement redacted per-connector health surface: `last_attempt`, `last_success`, imported count, cursor age, worker state, diagnostic recovery action. Separate auth from operational state.
- [ ] **P1-109** Confirm initial history imports paginate, require consent, expose progress, handle Gmail history expiry/IMAP UIDVALIDITY; hold auto-import to selected safe scope on initial connection.
- [ ] **P1-110** Add smoke tests for two independent mailboxes, restart, mid-import failure, cross-account isolation, and no plaintext leakage in logs/API/traces.

## Wave 2 — Mac / mobile / personal UX (parallel after contracts)

- [ ] **P1-201** Validate signed `nerve-personal-relay` build on M5 Max; guided install, Full Disk Access, LaunchAgent, secure device-token storage, service health and uninstall.
- [ ] **P1-202** Fix relay wizard to call explicit-consent `/api/relay/import-request` with device ID rather than generic mail Import; show token once and hide it after pairing.
- [ ] **P1-203** Prove one real new Messages event and a consented historical event reach a durable conversation and attention item; replay dedup + reboot checks. Record only counts/traces, not content.
- [ ] **P1-204** Wire *durable* Today projection from actual inbox, calendar and bills; test evidence source links and deadline dedup. Verify one top action in Low Energy Mode and its synchronization.
- [ ] **P1-205** Test real iPhone/Watch delivery, background refresh and acknowledgment convergence on physical devices. Distinguish delivered vs observed vs merely queued.
- [ ] **P1-206** Add operational status on Today: source freshness, count of unhealthy connectors, stale warnings. Never display “You're clear” when source data is stale or disconnected.

## Wave 3 — Production verification & promotion

- [ ] **P1-301** Run complete backend, narrowed new code Ruff, web build, Playwright, Apple unit suites; separately report existing repository-wide Ruff debt.
- [ ] **P1-302** Run Alembic migrations 006–010 against real disposable PostgreSQL and an approved staging instance; test restart persistence and backup/rollback.
- [ ] **P1-303** Connect two consented controlled IMAP/Gmail mailboxes through UI, perform incremental+history import, verify independent cursors, auth expiry, recovery.
- [ ] **P1-304** Verify Mac relay + controlled Messages inbox on the actual build Mac.
- [ ] **P1-305** Inject a real controlled deadline message; prove one durable attention item in Today and iPhone, acknowledge, restart, and confirm no recurrence/duplicate.
- [ ] **P1-306** Force staging outages (DB / S3 / provider), confirm no false healthy status, loss, or cursor advancement; recover and replay.
- [ ] **P1-307** Require policy/PII/privacy inspection: deny unauthorized owner, CSRF failure, remote OmniRoute denial for LOCAL_ONLY, safe logs and redacted traces.
- [ ] **P1-308** Publish sanitized results under `evidence/verification.md` with PASS LIVE/PASS SYNTHETIC/UNVERIFIED, blockers, rollback, and signoff. Only then mark phase Production Activated.

## Zoo kickoff instruction

```text
Take specs/personal-ops-production-activation/{spec.md,plan.md,tasks.md}
as the authoritative next implementation phase.

Review the deployed repository and test status; do not repeat the P0
personal-ops implementation. Start with Wave 0 and freeze auth/worker/durable
ingest contracts before code changes.

Spawn parallel agents with the file ownership in plan.md. Maximize independent
work; never let multiple agents silently edit app.py, wiring.py, registry.py
or AddConnectionWizard.tsx.

Do not claim an account is healthy after a login probe. Do not mark a task
complete solely from mocks, SQLite, or a degraded-mode browser test.

Use local OmniRoute where personal data is allowed, never cloud by default.
Never print user secrets, import real private data without consent, send an
outgoing message, or mutate production mailboxes as part of a test.

Stop the release if identity, CSRF, vault, owner isolation, encryption, durable
cursor handling, or rollback is unsafe.

Deliver one working source → inference/extraction → durable attention →
Today/iPhone vertical slice, proven against real consenting test accounts.
```
