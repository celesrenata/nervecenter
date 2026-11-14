# P1 verification: audit, gap matrix and test results

Date: 2026-11-13. Scope run (owner: "audit and test, then move onto the next gate"): P1-001, P1-002, P1-301, P1-302 and the read-only part of P1-307. **Nothing was changed on the cluster, no real mail was read, and no device was touched** (P1-004 approval is still outstanding).

Legend: PASS LIVE = observed on production. PASS SYNTHETIC = proven by tests or a scratch environment only. UNVERIFIED = not run. OPEN = not built.

## P1-001 baseline (live, read-only)

| Item | State |
|---|---|
| Images | api, web `c4aaaef4` (current code; later commits are docs only); mcp `fc0bb30` (older pin); postgres `pgvector/pgvector:pg16` |
| Ingress / SSO | `GET /` returns 302 to `sso.celestium.life` (PASS LIVE) |
| Migration head | `025_mail_expect_feedback`, 67 tables |
| Connections | 2 Gmail, both `authenticated` / `healthy`; **0 IMAP, 0 Graph, 0 relay** |
| Secrets | `NERVE_SECRET_KEY`, DB URL, NATS URL and `NERVECENTER_OBJECT_STORE_ENCRYPTION_KEYS` (from Secret `nervecenter-object-key`) are all set in the api (names only checked; values never read). A first probe reported the object-store key as unset because I used the wrong variable name |
| Rollback | unchanged from the deploy script: `kubectl rollout undo`, DB snapshot `/volume1/Backups/nervecenter/...pre-reobserve-20261113T152059Z.dump` |

## P1-002 gap matrix (code audit; live where noted)

| Task | Status | Evidence |
|---|---|---|
| P1-101 IMAP credentials endpoint | **OPEN** | `connections.py` has no `/credentials` route (routes: list, get, create, patch, delete, auth, verify, sync, import, reconnect, diagnostics, permissions) |
| P1-102 wizard IMAP password form | **OPEN** | `AddConnectionWizard.tsx:175`: "App passwords are stored via the backend CLI (the web UI never takes the password)" |
| P1-103 control plane, ImapClient, CORS | **PASS SYNTHETIC** | `ImapImapClient` exists; CORS drops `*` and enables credentials only with an explicit origin list (`app.py`); `/sync` and `/import` routes exist. Provider-specific OAuth: PKCE helper present |
| P1-104 shared Fernet repository, fail closed | **PASS SYNTHETIC** | `test_connections_auth`, `test_restart_persistence`; the 12 integration tests pass on real Postgres |
| P1-105 worker reconcile on create/enable/disable/delete | **PASS SYNTHETIC** | `MailIngestSupervisor.reconcile_once`; 9 tests: start on add, stop on disable and removal, restart on re-enable, no restart needed, survives DB failure. *Live:* 2 Gmail connections, both healthy |
| P1-106 Gmail OAuth hardening | PARTIAL | PKCE verifier/challenge and state in `oauth.py`; the Graph provider not shown healthy is **UNVERIFIED**; live consented staging test not run |
| P1-107 durable ingest / outbox boundary | **PASS SYNTHETIC** | commits `642e2b33`, `4f6fe4e3`, `55a2fbab` (stable id, durable-first reads, TOCTOU, pagination) |
| P1-108 per-connector health | **FIXED 2026-11-14, PASS LIVE** (see update below); rest PARTIAL | `last_success_at`, `last_attempt_at` in calendar and Today; connections diagnostics endpoint exists; full set (cursor age, worker state, recovery action) not checked field by field |
| P1-109 history imports | PARTIAL | Gmail history-expired handling and IMAP UIDVALIDITY reset exist in `mail_transport.py`; consent and progress UI not checked |
| P1-110 two-mailbox smoke | **PASS LIVE (partly)** | two real Gmail accounts have run side by side for weeks with separate scopes and cursors; no dedicated smoke test exists |

## P1-301 test runs

| Suite | Result |
|---|---|
| Backend (`-m "not integration"`) | **8798 passed**, 3 skipped, 1 failed: `test_routines.py::TestWellnessSleepSeam` (known, date-dependent, unrelated) |
| Integration on real Postgres | **12 passed** |
| Web build (`tsc && vite build`) | passed |
| Playwright e2e | **105 passed** |
| Apple (`scripts/test-apple.sh`) | **113 passed**: shared 13, relay 74, apps 26 |
| Ruff, repo-wide | **618 pre-existing errors** (tests/backend 195, backend/api 125, backend/persistence 98, communications 36, connectors 17, policy 15). NerveShield code is clean (0). Not fixed here, per the spec: reported separately |
| Web `npm run lint` | **cannot run**: `eslint` is not installed (`command not found`) although the script is defined. A tooling gap, not a lint pass |
| Gotcha | a bare `swift test` runs **0 tests** and reports success. Use `scripts/test-apple.sh`, as the repo already documents |

## P1-302 migrations on a disposable Postgres

`pgvector/pgvector:pg16` scratch container (removed afterwards):
- upgrade to `025_mail_expect_feedback`: 67 tables, matches production;
- downgrade to base: 1 table left (`alembic_version`), no failure;
- upgrade again: 67 tables;
- **restart persistence:** a row and the schema head survived `docker restart`;
- **dump and restore:** `pg_dump -Fc`, then `pg_restore` into a fresh database: schema head and the probe row both intact.

(P1-302 names migrations 006-010; the chain is now 001-025 and all of it was exercised.) An approved *staging* instance does not exist; production was not touched.

## P1-307 policy and privacy (read-only)

| Check | State |
|---|---|
| Remote OmniRoute denial for local-only routes | PASS SYNTHETIC: `test_mail_security_route_guard`, `observer_route`, `test_drafts`; PASS LIVE for the observer (secure/reader guard on every call) |
| Cross-account isolation | PASS SYNTHETIC in NerveShield tests (scoped by account); the connections API owner-check negative tests were **not found** by search (UNVERIFIED) |
| CSRF | tests exist in connectors registry and wiring; not exercised against live |
| Safe logs and traces | redaction tests present (`redact.py`, supervisor redaction tests) |

## What blocks "Production Activated" (P1-308)

Needs the owner: **P1-004** approval, **P1-201 to P1-205** (M5 Max relay, Messages, physical iPhone/Watch), **P1-303/305/306** (controlled test mailboxes and staging outage drills). Buildable without the owner: **P1-101 and P1-102** (IMAP credential endpoint and wizard form), plus a connections-API owner-isolation negative test. No IMAP account exists today, so these two matter only if you add a non-Gmail mailbox.

## Update 2026-11-14: P1-108 bug found and fixed

**Found (live):** the two healthy Gmail connections had `last_success_at = NULL` and a last attempt about 9 days old, although polls succeeded every 30 s and mail arrived the same day. The continuous worker set `operational_status="healthy"` but never wrote `last_success_at` or `last_attempt_at`; only the manual "Sync Now" path did. So "healthy" was a stale label from connection time, and the Today screen reported "fresh" with no success on record. It could not have detected a silently dead account.

**Fixed (`b485437d`, `mail_worker.py`):** every poll outcome stamps `last_attempt_at`; a successful poll (including an empty one) also stamps `last_success_at`. A failure never touches `last_success_at`, so the last real success stays on record and the 24 h stale window now works.
- 5 new tests (success, empty poll, auth and connect failure, failure after success); verified to **fail on the original worker**.
- One existing test (`test_health_reporting`) asserted the old behaviour ("the mail worker writes ... not last_attempt_at"). Updated to the corrected behaviour.
- Full suite: 8803 passed. Deployed.
- **Live check:** both accounts now show `last_success_at` 8 s and 28 s old, advancing across two reads 40 s apart; mail polling unaffected; no new errors.

**P1-201 build side (this Mac is the M5 Max):** `NervePersonalRelay` release build compiles; an ad-hoc signature on a `/tmp` copy verifies (`codesign --verify --strict`); `help` lists init-config, status, run, install, uninstall, pair, health and `import --consent`. **Not installed.** Install needs Full Disk Access (only the owner can grant it) and then reads Messages history, which is the P1-004 approval.

**New live observation:** a third connection row `632e3a89` (gmail, created 2026-11-14 04:01 UTC) is enabled but has no token, so it fails every poll with `connect_failed` and shows as "disconnected" on Today. Left in place pending the owner's decision.

## Still needs the owner

| Item | What only the owner can do |
|---|---|
| P1-004 + P1-201..203 | Approve reading Messages history; grant Full Disk Access; paste the pairing token |
| P1-205 | Physical iPhone/Watch delivery test |
| P1-305 | Send one self-addressed email with a deadline (a few seconds); the check afterwards is automated |
| P1-306 | Not run: forced outages need a staging environment, and production is not used for drills |
| DNS (NS-ADR-084) | Access to whoever controls the Route 53 zones |

## Update 2026-11-14 (later): owner decisions

- **Third connection deleted** (owner: "yes, continue with 2 accounts"). Row `632e3a89` was unconfigured with no secrets, cursors, import jobs or relay rows; removed through the app's own `delete_connection`, guarded to that ID and to `unconfigured`. The supervisor stopped its worker (`reason=removed`). The two real accounts stayed healthy, with `last_success_at` 13 s and 15 s old, and polling continued.
- **Messages relay not started.** "Continue with 2 accounts" is not approval to read Messages history (P1-004). Waiting for an explicit yes.
- **P1-305 baseline recorded** (UTC 2026-11-14 11:02): attention_items 79 (newest 02:42), communications 689 (newest 08:10), obligations 0.

## Update 2026-11-14 (relay trial)

P1-201..203 were exercised with owner approval: see `evidence/relay-trial.md`. Pairing, real-schema read, import (30 days: 68 messages), dedup and `healthy` status all PASS LIVE after fixing five relay defects. Durable message bodies and an SSO-free route for permanent operation are open. A separate `import_jobs` leak (53,905 rows) was found and fixed. P1-205 (physical iPhone/Watch) and P1-305 (deadline email) remain.

## Update 2026-11-14 (relay in permanent operation)

P1-201..203 now PASS LIVE: the relay runs as a LaunchAgent, a real new message (`test`) travelled Mac -> public SSO-free route -> durable row + encrypted body and the connection is `healthy` (see `evidence/relay-trial.md`). Still open: P1-205 (physical iPhone/Watch), P1-305 (deadline email), P1-306 (not run: no staging), a reboot test of the agent, and checking the Messages/Today screens as the logged-in owner.

## Update 2026-11-14 (owner confirmed)

The owner confirmed a relay message (`test`) on the Messages screen. P1-201..203 are complete end to end: Mac -> public route -> durable encrypted record -> Messages screen. Still open: P1-205 (iPhone/Watch), P1-305 (deadline email), P1-306 (no staging), an agent reboot test.
