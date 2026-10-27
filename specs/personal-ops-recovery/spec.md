# Personal Operations Recovery — Shared Contract Freeze

**Status:** Normative P0 shared contract. It controls where audits differ; where it is silent, defer to the five reports indexed in [audit README](audit/README.md:1).

## 1. Problem

The completed personal reasoning layer has Person, Communication, Conversation, detection, and attention seams, but its singletons are in-memory. A user currently cannot connect personal accounts or configure assistance without source edits or REST calls. This lift extends existing components, preserves completed functionality, and makes this outcome durable: **Open NerveCenter → Connect accounts → Import personal information → Receive a useful Today briefing → Take action → Have NerveCenter remember what happened.** Communication bodies remain behind references, as confirmed by [communications audit](audit/backend-communications.md:127).

## 2. Success criteria

1. Connect two email accounts entirely through UI → A+B+C.
2. Config survives API restart → F+B restart test.
3. New emails appear automatically → C.
4. Email with deadline creates actionable item → C+K (detection already exists in `backend/communications/detection/`).
5. Mac relay guided setup → D+A.
6. New messages appear → D+C-style ingest.
7. Calendar appointment appears on Today → E+L.
8. Bill reminder escalates near due date → G+K.
9. Commitment survives restart + completable → F+J.
10. Important item reaches iPhone/Watch → M+K.
11. Snooze/complete/dismiss → K+L+M.
12. Privacy policy prevents accidental external disclosure → §15 policy + N tests.
13. Failed connector → actionable diagnostics → B+A.
14. Whole flow works without sample/demo records → N strips demo seeds from the e2e path.
15. UI distinguishes Connected/Syncing/Degraded/Authentication Required/Not Configured → A.

## 3. Connection Center

Every mail account, relay device, calendar account, and other integration is a `connections` row. The two orthogonal axes are `auth_status`: `unconfigured`, `pairing_pending`, `authenticated`, `auth_expired`, `auth_failed`; and `operational_status`: `never_synced`, `syncing`, `healthy`, `degraded`, `error`. Fields are `last_success_at`, `last_attempt_at`, stable `last_error_code`, redacted `last_error_message`, JSONB `sync_cursor`, JSONB `import_progress`, `enabled`, and non-secret JSONB `config`.

A successful login probe with no ingested data is `never_synced` and must never be labeled healthy/operational. Render auth separately from operational status: **Connected** is authenticated plus healthy; **Syncing** is syncing; **Degraded** is authenticated plus degraded or never-synced after an error; **Authentication Required** is expired/failed; **Not Configured** is unconfigured/absent. Failed connectors expose an error code and plain-language recovery action.

## 4. Email

C provides one worker per enabled mail connection: IMAP IDLE with timeout polling fallback; Gmail history-resource incremental sync; explicit initial-history import recorded in `import_jobs`; deduplication by account and message-id/uid; durable `ingest_cursors`; reconnect/backoff; and the existing [communication ingestion path](../../backend/communications/registry.py:1). Workers restart from database state, never memory, and never log or emit plaintext credentials.

## 5. Apple Messages

D keeps the existing Swift Personal Relay engine. Add `install`/`uninstall` CLI subcommands which generate a LaunchAgent plist; `pair`, provisioning through `POST /api/relay/pair` and the existing NerveAuth CredentialStore; in-app and [INSTALL guidance](../../apple/Relay/INSTALL.md:1) for Full Disk Access; health beacon; explicit-consent initial history import; incremental sync; durable outbox; and signed release binary through existing Distribution tooling. Unsupported iOS inbox scraping is prohibited.

## 6. Persistence

Migration 006 creates `people`, `person_identities`, `communications`, `conversations`, `commitments`, `followups`, `attention_items`, `snoozes`, `notification_log`, `preferences`, `connections`, `secrets`, `ingest_cursors`, `import_jobs`. Migration 007 creates `bills` and `bill_evidence`; 008 creates `routines` and `routine_check_ins`; 009 creates `wellness_entries`; 010 creates `calendar_accounts` and `calendar_events`.

Use plural snake_case names, string UUID ids via uuid4, JSONB, and `DateTime(timezone=True)` under [base model conventions](../../backend/persistence/models/base.py:11); UUID database columns are forbidden. `people` includes id/display name/vip/default sensitivity/JSONB notes/boundaries/timestamps. Communications have person FK, `email/imessage/sms/voicemail/calendar/other` channel, direction, subject, non-null ref-only `content_ref`, existing sensitivity enum, received time, and JSONB flags. Conversations contain person/channel/last-message/unread count. Commitments/followups contain person/text/due/state and evidence/linked references.

Attention items include title/body/importance 1–10/`low|normal|high|critical` urgency/deadline/source refs/recommended action/escalation policy/`active|snoozed|resolved|dismissed` state/snooze/resolution fields. Bills include payee/cents/currency/due/one-time-weekly-monthly-yearly-other recurrence plus interval/`pending|confirmed_paid|disputed|stale` status/evidence confidence/self duplicate FK/timestamps. `confirmed_paid` is only explicit user action. Routines contain listed kind, JSONB schedule/timezone, enabled/completed, and wake fallback. Wellness contains time, nullable mood/sleep, consistency, energy/stress, journal, observations. Preferences are unique key/JSONB value. Calendar accounts/events, import jobs, and ingest cursors have every normative field supplied to this freeze; cursors are unique by `(connection_id, kind)` for `imap_seq`, `gmail_history`, and `relay_seq`.

`secrets` has id/kind/owner kind and id/encrypted-byte JSONB payload/timestamps. Fernet via `cryptography` reads `NERVE_SECRET_KEY` or a Kubernetes Secret. `connections.config.secret_ref` may reference its id but config never holds a secret. Redaction must match [redact_url](../../backend/persistence/connection.py:146).

## 7. Bills

Bills supply extraction evidence plus extracted-field confidence, duplicate handling, upcoming/monthly calendar views, and escalation hooks. NerveCenter never assumes payment, transfers money, or purchases.

## 8. Calendar

Calendar supports Google, CalDAV, and Apple-authorized providers, accounts, event sync, conflict lookup, and Today projections. Calendar event fields include account/ical UID/start/end/all-day/summary/location/description/timezone/change/appt flag.

## 9. Routines and sleep

Routines include wake, workday preparation, bedtime, morning briefing, appointment preparation, medication, and check-in. A missed routine is a gentle one-time note only: no guilt/streak UI. Missing Apple Health data never proves asleep or awake; wake workflows require deterministic clock-based fallback alarms.

## 10. Wellness

Wellness is private and user-controlled: create/list/update/delete entries, trends, explicit clinician export, easy pause/disable/delete. No psychiatric diagnosis, automatic disclosure, external sharing, or model path is permitted. DELETE wipes entries.

## 11. Relationships

Relationships provide rich people data (birthdays, commitments, notes, unanswered), editable notes/boundaries/VIP, unanswered/social plans, and reminders. Assistance is reflective and requested; no secret good/bad scoring replaces user judgment.

## 12. Attention v2

Aggregate mail, messages, voicemail, calendar appointments/deadlines, bill proximity, commitments, routines, existing incidents, and existing agent approvals. Each candidate carries importance, urgency, deadline, source evidence, recommended action, and escalation policy; candidates are deduplicated. `notification_log` allows only one notification per `(item, escalation level)` and escalation only at the time thresholds in its policy—not repeated arbitrary nagging. Support snooze, resolve, and dismiss.

## 13. Today and Low Energy Mode

`/` is the default landing page. **Needs You** shows a few top actionable items; **Coming Up** shows appointments, bills, deadlines, commitments for the next N hours/days; **Handled** shows completed/suppressed/resolved. One-tap persisted Low Energy Mode presents at most one next action, reduced complexity, preserved deadlines, and Done/Later/Help Me actions without guilt/streak UI.

## 14. iOS and Watch

Reuse `NerveAPI` and `NervePersistence` for top priorities, briefings, reminders, actions, pending approvals, notification preferences, reliable cache, and explicit stale-data indicator. Critical items must work without first opening the web dashboard. Preserve the frozen Event integrity hash.

## 15. OmniRoute and routes

Local-first uses OmniRoute through `NERVE_OMNIROUTE_URL` for classification, extraction, summaries, replies, search, and briefings. Add PDP `INFER_ROUTE`, default `local`; cloud requires an explicit user-created authorization policy row. Sensitive health/wellness and financial detail never go cloud without per-category authorization; wellness never has an external-model path.

Frozen routes are: Connections `GET/POST /api/connections`, `GET/PATCH/DELETE /api/connections/{id}`, `POST /api/connections/{id}/auth`, `/verify`, `/sync`, `/import`, `/reconnect`, `GET /diagnostics`, `GET/PATCH /permissions`; Relay `POST /api/relay/pair`, `/register`, `/messages`, `GET /health`, `POST /import-request`; Today `GET /api/today` returning `{needs_you[], coming_up[], handled[], low_energy: bool}` and `PATCH /api/today/mode`; Bills `GET/POST /api/bills`, `GET /upcoming`, `GET /calendar`, `POST /{id}/confirm-paid`, `PATCH /{id}`; Routines `GET/POST /api/routines`, `PATCH /{id}`, `POST /{id}/checkin`, `/complete`, `GET /today`; Wellness entries `GET/POST`, `PATCH/DELETE /{id}`, `GET /trends`, `/export`; Relationships/people `GET/PATCH /api/people/{id}`, `GET /api/relationships/unanswered`, `/social`, `POST /api/people/{id}/reminders`; Calendar `GET/POST /api/calendars`, `DELETE /{id}`, `GET /{id}/events?from&to`, `POST /{id}/sync`, `GET /conflicts?at&duration`; Attention `GET /api/attention`, `POST /{id}/snooze`, `/resolve`, `/dismiss`. Preserve current endpoint routes.

## 16. Runtime, exclusions, and definition of done

New `backend/ingest/` contains `supervisor.py` and `mail_worker.py`. Start supervisor in app lifespan when `NERVE_INGEST_ENABLED=1`, default on once durable storage is wired; persist liveness, reap/restart crashes, and run one task per enabled connection.

Do not touch [event models](../../backend/events/models.py:1), [persistence connection](../../backend/persistence/connection.py:1), [MCP catalogue](../../mcp/tools_module.py:1), [CLI helpers](../../cli/_helpers.py:1), [Apple Event](../../apple/Shared/Sources/NerveModels/Event.swift:1), or the Postgres `subPath` in [StatefulSet](../../deployments/kubernetes/base/postgres-statefulset.yaml:1). Rationale is audited at [contracts evidence §5](audit/contracts-evidence.md:237).

Definition of done remains: **Open NerveCenter → Connect accounts → Import personal information → Receive a useful Today briefing → Take action → Have NerveCenter remember what happened.**
