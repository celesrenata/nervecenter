# NerveCenter P1 — Production Activation & Real-Life Ingestion

**Status:** Ready for Spec Kit decomposition (not implemented by this specification)  
**Baseline:** `fb774d27a957cdfc25744b9bb086fe6b1f4d21c2`  
**Scope:** Real connector onboarding → continuous ingestion → durable attention → Today/iPhone/Watch  
**Primary build host:** M5 Max; deployment: existing NerveCenter K3s stack

## Problem and user outcome

The P0 personal-operations lift introduced a Connection Center, encrypted vault, mail workers, Apple Messages relay, bill/calendar/routine/wellness services, and a Today view. However its acceptance report explicitly did **not** validate live Postgres, a real IMAP account, a real Mac with Full Disk Access, real iPhone/Watch delivery, or production OmniRoute enforcement. The UI still instructs IMAP users to save a password using a backend CLI.

**User story:** Without a terminal, connect two independently authenticated real email accounts; pair the M5 Max Messages relay; have actual incoming items deduplicated, classified locally, converted into evidence-backed commitments/deadlines, and surfaced in Today and mobile. Tasks, cursors, and acknowledgements survive restart. Never claim an integration is connected merely because its login probe passed.

## Mandatory invariants

1. Never display, log, serialize into events, or return a plaintext credential. Passwords may traverse an authenticated HTTPS request once for immediate encrypted storage, then must be discarded from application state. Do not store browser credentials in localStorage, URLs, traces, or persistent client state.
2. Production personal content remains LOCAL_ONLY unless the user explicitly configures a narrower, auditable exception. Wellness stays outside autonomous agent context.
3. No synthetic/demo data in production. No account or success badge is fabricated when DB, object store, vault, ingestion worker, or provider is unavailable.
4. Delivery is **at least once**, processing **idempotent** by stable source identifiers. Cursor advances only after durable storage and downstream event/attention writes succeed.
5. Full message text is held in an encrypted/user-controlled object store. Database stores metadata and stable content references. Strict per-account authorization protects all reads, writes, and imports.
6. All external messages are draft-only by default. No automatic outgoing messages, payments, or destructive actions.
7. Use production NerveCenter components, not a second generic connector framework. Do not replace OmniRoute, Zoo, or the established policy/data model.
8. Database schema changes require reversible migrations or documented safe forward-only migration constraints with backups. Zero unapproved loss of existing personal data.

## In scope

### P1-A — Deployment preflight and operability

- Validate `NERVE_DB_URL` and apply migrations 006–010 against a disposable *PostgreSQL* first, then staging; verify existing deployed migration head rather than assuming it.
- Validate `NERVE_SECRET_KEY` encryption/decryption, persistent object-store settings, `NERVE_INGEST_ENABLED`, `NERVE_CALENDAR_SYNC_ENABLED`, API authentication, and pod rollout behavior. Never print secret values.
- Separate liveliness from readiness. Missing critical dependencies must be surfaced as *degraded/not-ready*, not silently accepted as a successful integration.
- Add `/api/personal/health` or extend existing diagnostics with **redacted** per-subsystem readiness: db, migrations, vault, object store, mail workers, calendar sync, relay, attention projection, notifications.
- Per-account status: never configured / auth required / authenticated-no-sync / syncing / healthy / degraded / error, with accurate timestamps and next recovery action.

### P1-B — IMAP setup and OAuth closure

- Add an authenticated, user-scoped, rate-limited, CSRF-safe credential submission action to the Connection Center for IMAP/app passwords. Accept the password once; encrypt directly in the existing Fernet vault using `ConnectionControlPlane.store_secret`. Provide an explicit `secret_set` signal but never expose stored plaintext or tokens.
- Verify actual IMAP credentials using the production `ImapImapClient` adapter; do **not** advance the wizard past verification when the credential is missing.
- Check actual Gmail OAuth scopes, consent URL, callback state validation, PKCE/credential handling, token refresh, and exchange registration against Google's current requirements. Do not assume `backend/connectors/mail/oauth.py` is already correct.
- Check Microsoft Graph account/provider routing separately from IMAP. Do not mark an unsupported provider healthy.
- Support two accounts simultaneously, independent cursors and per-account errors, no cross-account mixing.

### P1-C — Operational mail ingestion

- Register real probes, OAuth exchangers and `IngestTrigger` instances when the control plane is constructed. Audit `backend/api/app.py:_install_connection_control_plane`; it currently instantiates `ConnectionControlPlane` without registering those dependencies.
- Ensure ingest supervisor sees new/enabled/deleted connections without a server restart, and that `Sync Now` / `Import` invokes a real worker. Provide bounded deduplicated worker lifecycle, stop/restart, backoff and health reporting.
- Ensure supervisor and control plane use **the same encrypted** repository/secret codec. Review `backend/api/wiring.py` (supervisor installed before the separate Fernet-backed registry in `app.py`). Fail closed on missing encryption key. 
- Import initial history with explicit account-scoped consent, bounded pagination and visible progress. Handle UIDVALIDITY resets, Gmail history expiration/refresh, rate limits, token expiration, and reconnects.
- Trace actual mail bytes → object store → durable communication → extraction → attention → Today. `DefaultIngestSeam` currently delegates to process-wide communication singletons; prove the *durable* downstream path rather than only the in-memory mirror.
- Make failed writes replayable without advancing cursors; store an ingest error and preserve evidence reference. No success if only one of the required persistence steps committed.

### P1-D — Apple Messages relay

- On the M5 Max, provide guided install/pairing, signed binary/build instructions, Full Disk Access steps, LaunchAgent setup, secure credential reference, liveness and test-message flow.
- Verify `POST /api/relay/pair`, `register`, `messages`, `health`, `import-request` using a real Mac after consent.
- Audit that the wizard's generic `startImport` call for `relay_device` does not bypass `import-request` consent or falsely mark completed; make the correct flow explicit.
- Verify new and previously selected historical messages flow through the *durable* communication/attention pipeline. Avoid directly manipulating Messages storage, and treat schema drift/permission denial as a visible degradation.

### P1-E — Today, personal attention and notification delivery

- New incoming email/message with an explicit request/deadline produces one deduplicated evidence-backed AttentionItem through the *production* pipeline.
- Day-start brief: top 1–3 actionable items + upcoming bills/appointments + waiting-ons. No endless alerts. Low Energy Mode is persistent, suppresses clutter, and never hides imminent consequential deadlines.
- Verify notifications to an actual iPhone and paired Watch, not just Swift store/Widget tests. Show staleness and delivery failure honestly; no guaranteed-delivery claims for push.
- User can snooze, complete, dismiss and open source evidence; actions persist and sync back to the same canonical record.

## API and UX acceptance contract

The user can navigate: **Settings → Connections → Add account → Authenticate → Verify → Import → Healthy**, all in browser for IMAP and Gmail (except legitimate provider redirect/pairing on Mac). Every transition is backed by a server-observed event. `Healthy` requires a successful real poll/import, not successful login alone.

**Canonical integration demonstration** (run with consented test accounts; never import private content into CI):

1. Two real accounts connected and visibly differentiated by label.
2. A controlled incoming message to each account arrives without manual curl and is attributed to the right source.
3. One controlled message with a deadline generates exactly one durable commitment/attention item in Today.
4. Restart API + mail worker; confirm connection records, encrypted credential references, cursors, messages and attention survive and no duplicate is generated.
5. Force one bad IMAP password; show `Auth Required` without impacting the other mailbox.
6. M5 Max relay pairs, imports consented history, and ingests a newly received test message; permission denial is visible and recoverable.
7. A high-priority test item is delivered to phone; test Watch when paired. Acknowledge on a client; Today reflects completion.
8. Disconnect Postgres/object store/vault temporarily in staging; no false green state, no plaintext leakage, no silent cursor advancement. Restore and replay successfully.

## Explicit out of scope

No new general-purpose cloud control plane, AWS emulation, massive photo indexing, new AI framework, money transfers, autonomous relationship scoring, automatic clinician contact, or additional speculative UI dashboards in P1. Keep the production activation narrowly focused on daily usability.

## Completion states

- **Implemented:** code, migrations, tests and UI landed.
- **Staging verified:** live PostgreSQL, object store, at least two controlled accounts with actual ingestion.
- **Device verified:** Mac relay + iPhone/Watch physical delivery demonstrated.
- **Production activated:** deployed, configured, monitored, can recover from provider/session restart, personal-data isolation verified.

Do not mark the overall phase *done* until **Production activated**. Mark any untested provider/device case **UNVERIFIED**, never PASS.
