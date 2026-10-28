# P1 Execution Plan — Activation, Not Architecture Expansion

## Baseline evidence from actual source

| Finding | File / reason |
|---|---|
| Wizard still requires out-of-band IMAP password | `web/src/components/connections/AddConnectionWizard.tsx` auth step |
| Real IMAP probe exists but is not supplied at app construction | `backend/api/app.py:_install_connection_control_plane` vs `backend/connectors/registry.py:_probe_for` |
| No production trigger registration at control-plane construction | `backend/connectors/registry.py:_triggers`, `app.py` |
| Supervisor takes account snapshot at `start()` only | `backend/ingest/supervisor.py` |
| Supervisor created with pre-vault registry | `backend/api/wiring.py` before Fernet registry replacement in `app.py` |
| Mail worker sends events through process-wide in-memory service | `backend/ingest/mail_worker.py:DefaultIngestSeam` |
| Relay has separate explicit-consent history endpoint | `backend/api/endpoints/relay.py:/import-request` vs generic wizard import |
| Live tests were not executed for Postgres/Mac/devices | `specs/personal-ops-recovery/{acceptance,privacy-review}.md` |
| Control-plane CORS is overly permissive for sensitive credential endpoints | `backend/api/app.py:_configure` uses wildcard origins and credentials |

## Implementation policy

- Start with a deployment-readiness report and consented **staging** verification. Never change a production account, rotate a key, overwrite message history or turn on external communication without operator approval.
- Inspect already-configured K8s secrets by **existence, type and wiring only**, without printing values.
- Freeze a small API delta before parallel patches:
  - `POST /api/connections/{id}/credentials` (app-password only; encrypted, authenticated, user-scoped, no plaintext response)
  - Connection lifecycle interface `ensure_worker(id)` / `remove_worker(id)` / `sync_now(id)`
  - `GET /api/personal/health` secret-free diagnostics
  - Relay-specific import/pair action with explicit consent
  - Durable `ingest_communication` contract including transaction/replay semantics
- Map platform authentication and CSRF protections *before* accepting credentials in a new endpoint. Reject cross-origin credential POSTs and ensure a per-user owner/resource binding. Close wildcard CORS for authenticated personal endpoints.
- Avoid concurrent edits to `backend/api/app.py`, `backend/api/wiring.py`, `web/src/components/connections/AddConnectionWizard.tsx`, and `backend/connectors/registry.py`.

## DAG and ownership

```text
      P1-0 preflight / current deployment snapshot (read-only)
                         |
      P1-1 contracts + auth/security gate
                         |
      +------------------+------------------+
      |                  |                  |
 P1-2 mail auth     P1-3 worker         P1-4 durable
     web/API        lifecycle          communication
      |                  |                  |
      +------------+-----+------------------+
                   |
          P1-5 staging real mail
                   |
      +------------+----------------------+
      |                                   |
 P1-6 Mac relay                    P1-7 Today integration
      |                                   |
      +------------------+----------------+
                         |
             P1-8 actual device gate
                         |
               P1-9 production rollout
```

Parallel tracks independent of core mail work after P1-1: Mac relay installer/harness; iPhone/Watch delivery diagnostics; K8s readiness dashboards; integration-test fixtures.

| Worker | Owned paths | Deliverable |
|---|---|---|
| Security/Contracts | `specs/personal-ops-production-activation/`, policy tests | Wire/security freeze, threat model |
| Mail/UI | `web/src/components/connections/`, `web/src/api/connections.ts`, `backend/api/endpoints/connections.py` | IMAP credential wizard + endpoint |
| Mail/Runtime | `backend/ingest/`, `backend/connectors/mail/` | Supervisor dynamic lifecycle + incremental reliability |
| App/Wiring | `backend/api/app.py`, `backend/api/wiring.py`, `backend/connectors/registry.py` | Single Fernet registry, probes, triggers, OAuth |
| Durability | `backend/communications/`, `backend/persistence/repositories/`, `backend/attention/` | Durable message→attention path |
| macOS | `apple/Relay/`, `apple/Relay/INSTALL.md`, `tests/apple/` | Real relay pairing/import/restart test |
| Mobile | `apple/iOS/`, `apple/Watch/`, `apple/Shared/` | Actual device delivery and ack convergence |
| Infra/QA | `deployments/`, `tests/integration/`, `specs/.../evidence/` | Readiness, live Postgres/MinIO, canary evidence |

*Shared-file ownership is exclusive. Integrate after each dependency gate, not when all parallel work finishes.*

## Operational test stages

1. **Local synthetic:** unit + provider stubs + isolated PostgreSQL + MinIO-compatible object store; no real mail credentials.
2. **Staging real accounts:** explicitly authorized controlled test mailboxes, two independent connections. Exercise actual network, auth, sync, restart, migration, cross-account isolation, and fault recovery.
3. **Physical devices:** M5 Max with Messages enabled and Full Disk Access (user consent); iPhone/Watch with installed NerveCenter; capture screenshots/status, not private message text.
4. **Production canary:** enable one connector for limited historical scope, prove Today entry and acknowledgment; extend to remaining accounts once healthy.

## Gate commands (adapt to environment)

```bash
uv run pytest tests/backend/test_connections_api.py tests/backend/test_mail_ingest.py tests/backend/test_restart_persistence.py -q
uv run pytest tests/ -m "not integration" -q
uv run ruff check backend/ingest backend/connectors/mail backend/api/endpoints/connections.py
cd web && npm run build
cd web && npx playwright test tests/e2e/personal-flow.spec.ts
bash apple/scripts/test-apple.sh
```

New live integration suite must be separately runnable and skipped unless test secrets/connectors are explicitly provisioned, e.g. `uv run pytest -m live_personal`. Tests must not accidentally send outgoing email, bills, or SMS.

## Metrics and evidence

Persist only sanitized evidence under `specs/personal-ops-production-activation/evidence/`: test environment versions, migration head, connection IDs hashed, counts, cursor positions (non-secret), resource liveness, timestamps, trace IDs, test verdicts. Never private content, emails, tokens, passwords, phone numbers or names.

Observe:
- worker count vs enabled connection count, last good poll, error/backoff, cursor age
- messages detected vs stored vs attended, per-account lag, dead letters/replays
- attention false negatives for known test requests, duplicate count
- delivery created/accepted/seen/acknowledged counts separately (no false push guarantees)

## Required review of known edge risks

- Current OAuth builder uses a non-standard-looking Google scope and omits PKCE/state enforcement in the implementation path. Verify against current provider API; no silent account permissions escalation.
- `app.py` currently uses `allow_origins=["*"]` with credentials. Correct before accepting secret-bearing browser POSTs.
- `MailIngestWorker._process_batch` advances seen keys before ensuring all downstream writes are durable. Design replay-safe atomicity/outbox; test crash just after object storage, just before attention write, and just before cursor commit.
- `MailIngestSupervisor` may run once per API replica. Add per-connection lease/ownership before scaling replicas; current deployment is one replica, but never rely on this indefinitely.
- Distinguish known bugs from unverified suspicions. Add failing regression tests where practical before patching.

## Acceptance review and release rule

Zoo writes `evidence/verification.md` with every P1 criterion labeled `PASS LIVE`, `PASS SYNTHETIC`, `BLOCKED`, or `UNVERIFIED`. A simulated test cannot be relabeled live. No release unless secrets are encrypted, owner authorization is enforced, and source-to-attention survives restart. Roll out with rollback plan and explicit operator approval.
