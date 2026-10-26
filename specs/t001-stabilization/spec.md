# Spec: T001 — Stabilization and Hygiene Closure (G-0 / G-4)

**Feature ID:** t001-stabilization
**Created:** 2026-10-14
**Status:** Done (2026-10-26)
**Branch target:** main (repo root: nervcenter)
**Upstream:** [gaps1.md](../../gaps1.md) §4 (Gap inventory, T0 + T4), §5 (Sequenced closure plan, Phase 0 + Phase 5)
**Companions:** [gaps1.md](../../gaps1.md) §2 (Verified current state), §3 (Crashes found & fixed)

## Problem

Phase 0 of the closure plan (gaps1.md §5) and Phase 5 (the hygiene sweep) are
the two highest-integrity, lowest-risk workstreams remaining. The repo is
currently **stable but dirty**: 559 Python tests pass, `swift build` is green,
the web app builds — but the tree carries three classes of unaddressed debt
that undermine confidence in every subsequent feature.

1. **Uncommitted, untested mail-connector plane (G-0.1 / G-0.2).**
   The `backend/connectors/mail/` plane (IMAP probe client, OAuth
   authorization-url builder, token-ref registry, HTTP endpoints, and a router
   mount) is staged in the worktree with **zero tests**. Nothing in `tests/`
   references `MailConnector*`. The Crash A re-export bug (gaps1.md §3.1)
   that made `import backend.api.app` fail was patched by hand but the
   fix has no regression test. Until tests exist and the plane is committed,
   every test run and every code review against `backend/api/app.py` is
   running against an unverified, untracked surface.

2. **Build artifacts tracked in git (G-0.3).**
   `web/dist/index.html` is committed and currently staged (a `git status`
   dirty entry). The `.gitignore` already has `dist/` (line 15) and
   `web/dist/` (line 42), but the file was added before the ignore rule, so
   it is still in the index. Regenerated bundles in git cause churn and
   stale-ship risk: CI cannot depend on a committed dist that may be
   out-of-date relative to `web/src/`. Additionally, `.patches/` (parallel-task
   worker output: `worker-1.patch`, `worker-2.patch`, `worker-3.patch`) is
   **not** gitignored, so it will pollute every `git status` and future diff
   tooling.

3. **Hygiene debt (G-4.1 – G-4.5).**
   - **G-4.1:** `swift test` in `apple/` runs only `NerveAppsTests` (2 tests);
     `NerveRelayTests` (31) and `NerveSharedTests` require explicit `--filter`.
     The root cause is likely a platform restriction (test targets that depend
     on `NerveApps` only run on macOS/iOS; the default `swift test` invokes
     the macOS host, but some targets may be excluded by platform gating or
     SwiftPM's default test-target resolution). The fix is either root-cause
     the `Package.swift` wiring or add `apple/scripts/test-apple.sh` that runs
     all three bundles explicitly.
   - **G-4.2:** `backend/api/wiring.py:357` uses `app.on_event("shutdown")`
     (687 warning occurrences per test run). FastAPI deprecated `on_event`
     in favor of ASGI lifespan handlers. The docstring at line 348 explicitly
     says "no lifespan refactor" — that decision is now reversed by this spec.
   - **G-4.3:** `docs/implementation-status.yaml` and
     `docs/architecture/current-state.md` predate the personal-operations
     planes (P0–P4) and the mail-connector plane. They reference paths like
     `backend/events/`, `backend/knowledge/` that do not match the real tree.
     `audit-gaps.md` is retained as historical record (not to be rewritten).
   - **G-4.4:** `tests/backend/test_auth_sso.py` triggers PyJWT "weak key"
     warnings (32-byte HS256 keys). Use ≥64-byte random keys in test fixtures.
   - **G-4.5:** The canonical test commands (`pytest -m "not integration"`,
     `npm run e2e`, per-bundle `swift test`) are scattered across
     `audit-gaps.md` and specs. Document them in `README.md` in a
     dedicated "Testing" section.

## Goals

1. The mail-connector plane is **tested and committed**: ~20+ pytest
   tests covering IMAP probe mapping, OAuth URL/callback/token-ref, service
   registry semantics, and HTTP endpoint contracts (list/create/verify/delete,
   secret never stored, only `token_ref`); committed as 2–3 logical commits.
2. `web/dist` is untracked and `.patches/` is gitignored: `git status`
   shows no build-artifact or worker-artifact entries after a rebuild.
3. `swift test` (or a canonical script) runs **all three test bundles**
   (`NerveAppsTests`, `NerveRelayTests`, `NerveSharedTests`) by default.
4. `backend/api/wiring.py` uses FastAPI **lifespan handlers** instead of
   `on_event`; the deprecation warning count drops to 0.
5. `docs/implementation-status.yaml` and `docs/architecture/current-state.md`
   reflect the post-T001 tree (personal planes P0–P4, mail-connector plane,
   policy PDP, event bus, persistence, auth).
6. JWT test keys in `test_auth_sso.py` are ≥64 bytes; PyJWT "weak key"
   warnings are eliminated.
7. `README.md` gains a "Testing" section documenting the canonical
   commands for Python, web (e2e), and Apple (per-bundle) test suites.

## Scope

**In scope:**

- `backend/connectors/mail/**` — the mail-connector plane source is
  already written (staged); this spec covers its **test suite** and the
  **commit** (not a rewrite of the plane itself). The Crash A re-export
  fix (`backend/connectors/mail/__init__.py` re-exporting `MailConnector`
  from `backend.api.models`) is committed as part of the mail-connector
  commit.
- `tests/backend/test_mail_connector.py` (new) — ~20 pytest tests
  mirroring the pattern of `tests/backend/test_email.py`.
- `.gitignore` — add `.patches/` to the build-artifacts / worker-output
  section.
- `web/dist/` — `git rm -r --cached web/dist` (untrack only; do not delete
  the local files). Verify `web/Dockerfile` (or `Dockerfile.api`) builds the
  web app in-image so CI does not depend on committed dist.
- `apple/Package.swift` (root-cause or workaround for G-4.1) and/or
  `apple/scripts/test-apple.sh` (new) — ensure all three test bundles run.
- `backend/api/wiring.py` — migrate `install_dependency_wiring` from
  `on_event` to a `@asynccontextmanager` lifespan handler (G-4.2).
  `backend/api/app.py` — wire the lifespan context manager into
  `FastAPI(lifespan=...)` (both the module-level `app` and `create_app()`).
- `docs/implementation-status.yaml` — refresh to match the real tree
  (G-4.3).
- `docs/architecture/current-state.md` — refresh to match the real tree
  (G-4.3).
- `tests/backend/test_auth_sso.py` — replace 32-byte HS256 test keys with
  ≥64-byte random keys (G-4.4).
- `README.md` — add a "Testing" section (G-4.5).

**Out of scope:**

- `backend/connectors/mail/**` source code (already written; only tested
  and committed).
- `web/src/**` (theme/Downloads feature — G-0.1's web portion is committed
  as a separate commit but not in this spec's test scope).
- `apple/iOS/**`, `apple/Watch/**`, `apple/Widgets/**` (G-2.x — Phase 4).
- `synthetic-lab/**` (G-3.3 — Phase 3).
- `docs/implementation-status.yaml` evidence-link updates beyond
  structural accuracy (full evidence re-verification is a separate pass).
- Any change to `audit-gaps.md` (retained as historical record).

## Constraints

- **No breaking changes to the API contract.** The mail-connector endpoints
  (`/api/mail-connectors` list/create/verify/delete) and the OAuth flow
  are already wired into `app.py`; tests must match the existing contract,
  not reshape it.
- **`web/dist` untracking must not break the Docker build.** Verify that
  `web/Dockerfile` (or the relevant build stage) runs `npm run build`
  in-image. If it does not, add the build stage before untracking.
- **Lifespan migration must preserve both startup and shutdown behavior.**
  `wire_dependencies(target)` at startup and `teardown_dependencies(target)`
  at shutdown must run in the correct order; tests that rely on app
  state (e.g., `test_app_wiring.py`) must remain green.
- **Swift test runner fix must not break `swift build`.** Any `Package.swift`
  changes must keep all existing targets compiling.
- **Commit discipline.** The mail-connector work is committed as 2–3
  logical commits (connectors / web theme / web downloads), not one
  monolithic commit. The hygiene items (G-4.x) are committed separately.
- **No scope creep.** G-1.x, G-2.x, G-3.x are explicitly out of scope
  for this spec; they are addressed by separate specs in the Phase 1–4
  closure plan.

## Assumptions

- The mail-connector plane source (staged in the worktree) is functionally
  complete and correct; the Crash A fix (re-export of `MailConnector`) has
  been applied. Tests verify the existing behavior, not new behavior.
- `web/Dockerfile` (or `Dockerfile.api` build stage) already includes a
  `npm run build` step that produces `web/dist` in-image. If not, the fix
  is to add the build stage (S size, within G-0.3 scope).
- The `Package.swift` test-target platform restriction (G-4.1) is a
  SwiftPM default-resolution issue, not a platform restriction in the
  `platforms` array. The fix is either a `--filter` script or a
  `Package.swift` adjustment (e.g., explicit test-target product or a
  `test-apple.sh` that invokes `swift test --filter` for each bundle).
- FastAPI 0.95+ is available in the project (lifespan handlers require
  the `asynccontextmanager` pattern introduced in 0.53+).
- The `docs/implementation-status.yaml` structure is YAML and can be
  edited in place; no schema migration is required.

## Acceptance Criteria

- **AC001 (G-0.2 / G-0.1):** `tests/backend/test_mail_connector.py` exists
  with ≥20 tests covering: (a) IMAP probe mapping (CAPABILITY ok →
  CONNECTED, auth reject → AUTH_EXPIRED, network fail → ERROR); (b) OAuth
  `build_authorization_url` (state, redirect_uri, client_id); (c)
  `parse_callback` (state validation, code extraction); (d)
  `exchange_request` (token exchange shape); (e) `make/validate_token_ref`
  (round-trip, expiration); (f) service registry upsert/404 semantics;
  (g) `verify()` status transitions; (h) `reset_mail_connector_service()`;
  (i) HTTP endpoints list/create/verify/delete (secret never stored, only
  `token_ref` returned). All tests pass: `pytest
  tests/backend/test_mail_connector.py` → green.
- **AC002 (G-0.1):** The mail-connector plane is committed as 2–3 logical
  commits (connectors / web theme / web downloads). `git status` after the
  commits is clean (no staged mail-connector files, no `web/dist/index.html`
  entry).
- **AC003 (G-0.3):** `web/dist` is untracked: `git rm -r --cached web/dist`
  is applied; `web/dist/` is in `.gitignore` (already present, verified).
  `.patches/` is added to `.gitignore`. `git status` shows no `.patches/*`
  entries. A rebuild (`cd web && npm run build`) does not dirty the tree
  with tracked files.
- **AC004 (G-4.1):** `swift test` in `apple/` runs all three test bundles
  (`NerveAppsTests`, `NerveRelayTests`, `NerveSharedTests`) by default, OR
  `apple/scripts/test-apple.sh` exists and runs all three explicitly.
  `./apple/scripts/test-apple.sh` (or `swift test`) → all 33+ tests pass.
- **AC005 (G-4.2):** `backend/api/wiring.py` uses a FastAPI lifespan handler
  (`@asynccontextmanager` + `FastAPI(lifespan=...)`), not `on_event`.
  `pytest tests/ -m "not integration"` shows 0 `on_event` deprecation
  warnings. `test_app_wiring.py` remains green (startup/shutdown behavior
  preserved).
- **AC006 (G-4.3):** `docs/implementation-status.yaml` and
  `docs/architecture/current-state.md` are refreshed to match the real tree:
  personal planes (P0–P4: attention, messages, email, voicemail, calendar,
  contacts, waiting-on, briefing), mail-connector plane, policy PDP, event
  bus, persistence, auth (JWT + SSO) all listed with correct paths. Stale
  references to non-existent paths (`backend/events/` as a top-level module,
  `backend/knowledge/` as a top-level module) are corrected.
- **AC007 (G-4.4):** `tests/backend/test_auth_sso.py` uses ≥64-byte HS256
  keys (e.g., `os.urandom(64)` or a fixed ≥64-byte string). `pytest
  tests/backend/test_auth_sso.py -W always` → 0 PyJWT "weak key" warnings.
- **AC008 (G-4.5):** `README.md` has a "Testing" section documenting:
  - Python: `pip install -e ".[dev]"` and `pytest tests/ -m "not integration"`
  - Web: `cd web && npm install && npm run e2e` (Playwright)
  - Apple: `cd apple && swift build && swift test` (or
    `./apple/scripts/test-apple.sh` for all bundles)
  - Expected results (e.g., "559+ passed, 0 failed").

## Verification

Each AC maps to a task in [tasks.md](./tasks.md) with its exact command or
verification step. Evidence lives in `docs/implementation-status.yaml` (if
updated) and the tasks.md verification log; gaps are stated explicitly,
never papered over.
