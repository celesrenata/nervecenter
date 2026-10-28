# P1 Personal Operations Production Activation — Evidence (2026-10-28)

Branch: `feat/p1-personal-ops-production-activation` (from `main` @ `fb774d2`).
Integration step: four scope patches applied + one wiring gap closed + one
pre-existing test fixture failure fixed + full verification gate.

## What was implemented this session

1. **Patch #1 — durable commitment seam** (worker-2, tree `6ee03091`):
   - `backend/communications/detection/wiring.py` — new `DurableCommitmentStore`
     protocol + optional `commitment_store` kwarg on `CommunicationPipeline`
     (default `None` = pure, zero I/O).
   - NEW `tests/backend/test_commitment_restart.py`.
2. **Patch #2 — durable attention transitions** (worker-1, tree `ccb763a7`):
   - `backend/api/endpoints/attention.py` — resolve/suppress/acknowledge/snooze
     route through `_aggregator(request).transition` (degraded mode
     byte-identical to old in-memory behavior; wired mode returns durable row
     dict like dismiss).
   - `backend/attention/aggregator.py` — `transition()` extended for
     acknowledge/suppress; fixed latent snooze bug reading `.state` on
     `SnoozeRow`.
   - NEW `tests/backend/test_attention_persistence.py`.
3. **Patch #3 — mail OAuth + redaction** (worker-2, tree `ccb763a7`):
   - NEW `backend/connectors/mail/oauth_refresh.py` — `MailOAuthRefresher`
     (durable refresh, vault write-back of rotated refresh_token + fresh
     access_token, stable error codes).
   - `backend/ingest/mail_worker.py` — `oauth_refresher` seam, one
     refresh+retry on `TransportAuthError`, fixed fall-through bug where a
     successful refresh still mapped to `auth_expired`, `redact_transport_error`
     applied to all persisted/logged transport error text.
   - `backend/connectors/mail/__init__.py` exports.
   - NEW `tests/backend/test_mail_oauth_refresh.py`.
4. **Patch #4 — supervisor reconcile + health + registry** (worker-3, tree `ccb763a7`):
   - `backend/ingest/supervisor.py` — periodic reconcile loop
     (`NERVE_INGEST_RECONCILE_S`, default 30s), starts workers for newly enabled
     mail connections WITHOUT restart, stops disabled/removed; idempotent;
     transient DB failures logged never raised; new `status()` accessor.
   - `backend/api/health.py` — additive `ingest` + `connections` sections;
     legacy `db`/`nats` fields and HTTP-200 semantics untouched.
   - `backend/persistence/repositories/registry.py` — exposes
     bills/routines/wellness repos.
   - NEW `tests/backend/test_supervisor_reconcile.py` +
     `tests/backend/test_health_reporting.py`.

### Wiring gap closed (integrate step)

The durable commitment store from Patch #1 defaulted to `None`, so the live
app still never wrote detected commitments to the durable table.

- **Seam:** `backend/communications/detection/wiring.py:457` — new module-level
  `set_commitment_store(store)` setter (extend-only). It binds the store onto
  the live pipeline and records it in `_pending_store` so pipelines composed
  *after* startup (the normal case: startup wires the store before the first
  `get_pipeline()` call) pick it up at construction
  (`backend/communications/detection/wiring.py:486`).
- **Injection:** `backend/api/wiring.py:293` — inside
  `wire_dependencies()`, immediately after the `RepositoryRegistry` is
  constructed, `set_commitment_store(target.state.repository_registry.commitments)`
  is called, guarded by a log-not-raise `try/except` consistent with the rest
  of the startup wiring. Pure default preserved: tests that build the pipeline
  directly (or call `reset_pipeline()`) are unaffected.
- The frozen plan's "F owns wiring in Wave 1" constraint is superseded for this
  integration step (explicitly authorized by the task). The edit is extend-only
  and does not touch any other F-owned code.

### Pre-existing fixture failure fixed

`tests/backend/test_today.py::TestTodayWired::test_coming_up_calendar_window`
failed identically on clean HEAD. **Root cause (evidence-verified, not the
hypothesis in the task):** the endpoint reads the wall clock
(`datetime.now(UTC)`) while the fixture pins `NOW = 2026-10-27 12:00 UTC`; the
48h `coming_up` window no longer contained the fixture events once the wall
clock passed that date. It was **not** a missing-table error — the per-source
`try/except` in `_collect_sections` (`backend/api/endpoints/today.py:472`)
already degrades honestly, which is why the failure was `coming_up == []`,
not a 500.

- **Fix (fixture side, no assertion weakened):**
  - `backend/api/endpoints/today.py:563` — `get_today` reads
    `app.state._test_now` (a pinned `datetime`) when set, else the wall clock.
    Production behavior unchanged (the attribute is only set by the test
    fixture).
  - `tests/backend/test_today.py:80` — the `wired` fixture sets
    `app.state._test_now = NOW`, making the window assertions deterministic
    regardless of run date.
  - `tests/backend/test_today.py:47` — `CommitmentRow.__table__` added to
    `TODAY_TABLES` (defensive: the commitments provider's raw queries now
    have the table; the raw-probe pattern already handled absence, but the
    fixture now matches the full durable surface the endpoint touches).

### E2E spec drift fixed (root route change)

The P1 root-route change (`TodayView` at `/`, `OverviewView` at `/overview`)
left five Playwright specs asserting the old `Overview` at `/`. Spec updated
to test the shipped product (no product change):
- `web/tests/e2e/views.spec.ts` — VIEWS array: `/` → `Today`, plus
  `/overview` → `Overview`.
- `web/tests/e2e/views-a.spec.ts` — OV tests navigate to `/overview`.
- `web/tests/e2e/briefing.spec.ts` — BF-4 expects `Today` at `/`.
- `web/tests/e2e/login-shell.spec.ts` — break-glass login landing heading is
  `Today`.

## Gate commands + results (all run on the integrated tree, 2026-10-28)

| Stage | Command | Result |
| --- | --- | --- |
| Backend suite | `uv run pytest -m "not integration"` | **1375 passed, 0 failed** (39.7s). Baseline was 1338 passed + 1 pre-existing failure; ~37 net new tests added by the four patches + fixture fix. |
| Web build | `cd web && npm run build` | **tsc + vite green**, 86 modules, ~1s. No unit-test script exists in `web/` (scripts: dev/build/preview/lint/e2e); the web driver is Playwright. |
| Web e2e | `cd web && npm run e2e` | **69 passed, 0 failed** (36.2s, chromium; backend auto-spawned in degraded mode by `global-setup.mjs`). |

No assertion was weakened anywhere. No DCG guard or hook was bypassed (one
`git checkout --` was correctly *blocked* by the DCG and was not retried;
PKG-INFO was handled via `git apply --exclude` per the task instead).

## Status ladder per success criterion (acceptance.md rows)

- **Implemented** — all Wave 1–3 backend + web work is in the tree
  (patches #1–4 + prior landed work; tasks.md fully ticked this session).
- **Synthetic tests passed** — backend 1375/1375, web build clean, Playwright
  69/69.
- **Staging verified** — NOT yet performed for this branch (no staging deploy
  from `feat/p1-personal-ops-production-activation`).
- **Physical device verified** — NOT verified. Blockers:
  - Real-account Gmail/IMAP import requires live OAuth credentials (only the
    synthetic OAuth-rotation path is exercised in
    `tests/backend/test_mail_oauth_refresh.py`).
  - Mac relay install requires a macOS device (the relay scripts + tests are
    in the tree; install/pairing not run on hardware).
  - iPhone/Watch widget delivery requires an iOS device + Watch (the
    `apple/Widgets` bundle builds in CI tests only).
- **Production activated** — NOT activated. The branch is committed locally;
  no push, no image build, no k8s rollout.

## Remaining blockers (do not mark verified)

- Real-account Gmail/IMAP import end-to-end (needs credentials).
- Mac relay physical install + pairing (needs hardware).
- iPhone/Watch physical delivery (needs devices).
- Staging deploy of this branch (needs a staging target + image build).
- No alembic migration was added (per task constraint); the durable
  `commitments` table is expected from E's migration 010 / the existing schema.
