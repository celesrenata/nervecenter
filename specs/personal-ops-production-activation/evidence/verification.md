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
| P1-108 per-connector health | PARTIAL | `last_success_at`, `last_attempt_at` in calendar and Today; connections diagnostics endpoint exists; full set (cursor age, worker state, recovery action) not checked field by field |
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
