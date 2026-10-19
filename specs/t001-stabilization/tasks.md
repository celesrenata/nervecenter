# Tasks: T001 — Stabilization and Hygiene Closure

**Spec:** [`spec.md`](./spec.md)
**Plan:** [`plan.md`](./plan.md)
**Status:** Planned
**Scope:** G-0.1–G-0.4 stabilization workstreams plus the G-4 hygiene closure defined by AC001–AC008.

> **gaps2 delta (2026-10-14 evening) — T002/T003/T004 are SUPERSEDED by
> [`specs/phase0-integrity/`](../phase0-integrity/).** The premise of this pack
> (gaps1.md, morning) was that the mail plane was *staged but uncommitted*;
> `gaps2.md` §3.1/§3.7 found it was in fact **committed broken** at `a3c07da`
> (HEAD `import backend.api.app` fails: `MailConnector` re-export missing).
> Execute Phase 0 from `specs/phase0-integrity/tasks.md` (P0-01…P0-06) instead
> of T002/T003/T004 below. T009 below is already done in the worktree (see
> verification log) and lands inside phase0-integrity P0-04 commit 2.
> T005/T006/T007/T010 + T008 (the Phase 5 hygiene sweep) remain valid here.

## Execution rules

- Keep the task order and ownership below. T002 must be green before T003 creates the mail-connector commit.
- **Commit-hygiene norm (gaps2 §7 Q6, standing rule; seconded by gaps4 Phase 5 E5):** two clauses, both must hold for a commit to ship.
  - *(a) Generated project:* no commit that touches a **generated project** (e.g. the Apple Xcode projects generated from `apple/Distribution/xcodegen/*.xcodegen.yml`) **and its patcher** ships without the patcher — commit the regenerated `apple/Distribution/Generated/` tree alongside the xcodegen/`Fastfile` change that produced it.
  - *(b) Import chain:* no backend commit that touches an import chain may ship without `python -c "import backend.api.app"` verified in a **detached worktree of the resulting commit** (never just the working tree). Propagated to `specs/phase0-integrity/` and `specs/gap-closure-t003/`.
- **T007 correction (gaps2 §3.7):** the "stale references" claim was wrong — `backend/events/` and `backend/knowledge/` **both exist and are tested** (e.g. `tests/backend/events/test_dlq.py`). T007 is a *content* refresh (docs dated 2026-09-29, pre-personal-planes), not a path fix; the `rg` path gate in T007 passes trivially.
- T003 is the explicit dependency boundary for the G-0.2 mail-connector commit: any task that verifies the committed mail plane must run after that commit exists.
- T003 and T006 both touch distinct regions of [`backend/api/app.py`](../../backend/api/app.py); reconcile both changes during T008 rather than overwriting either region.
- Do not modify [`audit-gaps.md`](../../audit-gaps.md), [`web/src/`](../../web/src/), or out-of-scope G-1.x/G-2.x/G-3.x surfaces except where the plan explicitly assigns ownership.
- Record command output, commit identifiers, and unresolved gaps in the verification log below. Do not claim a check passed without evidence.

## Dependency board

| ID | Workstream | Depends on | Owned paths | AC coverage | Status |
|---|---|---|---|---|---|
| T002 | G-0.2 mail-connector tests | None | [`tests/backend/test_mail_connector.py`](../../tests/backend/test_mail_connector.py) | AC001 | ⚠️ **superseded** → [`phase0-integrity` P0-03](../phase0-integrity/tasks.md) |
| T003 | G-0.1 mail-connector commit | T002 green | [`backend/connectors/mail/`](../../backend/connectors/mail/), [`backend/api/endpoints/mail_connectors.py`](../../backend/api/endpoints/mail_connectors.py), [`backend/api/models.py`](../../backend/api/models.py), router region of [`backend/api/app.py`](../../backend/api/app.py), assigned web/theme commit surfaces, Git commits | AC002 | ⚠️ **superseded** → [`phase0-integrity` P0-04](../phase0-integrity/tasks.md) |
| T004 | G-0.3 build artifacts | None; final gate follows T003 where clean-tree evidence includes its commit | [`.gitignore`](../../.gitignore), [`web/dist/`](../../web/dist/), [`web/Dockerfile`](../../web/Dockerfile) if present | AC003 | ⚠️ **superseded** → [`phase0-integrity` P0-05](../phase0-integrity/tasks.md) |
| T005 | G-0.4 e2e masks / Apple runner | None | [`apple/Package.swift`](../../apple/Package.swift), [`apple/scripts/test-apple.sh`](../../apple/scripts/test-apple.sh) if needed | AC004 | [ ] |
| T006 | G-4.2 lifespan migration | None | [`backend/api/wiring.py`](../../backend/api/wiring.py), lifespan region of [`backend/api/app.py`](../../backend/api/app.py) | AC005 | [ ] |
| T007 | G-4.3 documentation refresh | None | [`docs/implementation-status.yaml`](../../docs/implementation-status.yaml), [`docs/architecture/current-state.md`](../../docs/architecture/current-state.md) | AC006 | [ ] |
| T009 | G-4.4 JWT key hygiene | None | [`tests/backend/test_auth_sso.py`](../../tests/backend/test_auth_sso.py) | AC007 | [x] |
| T010 | G-4.5 canonical test documentation | None | [`README.md`](../../README.md) | AC008 | [ ] |
| T008 | Integration gate | T002, T003, T004, T005, T006, T007, T009, T010 | Verification only; this file's log and command evidence | AC001–AC008 | [ ] |

## Detailed task checklists

### T002 — Mail-connector test suite (G-0.2)

- [ ] Add approximately 20 or more tests in [`tests/backend/test_mail_connector.py`](../../tests/backend/test_mail_connector.py), following the in-memory service, HTTP `TestClient`, reset-hook, and envelope-assertion pattern in [`tests/backend/test_email.py`](../../tests/backend/test_email.py).
- [ ] Cover IMAP probe mapping: successful `CAPABILITY` → `CONNECTED`, authentication rejection → `AUTH_EXPIRED`, and network failure → `ERROR`.
- [ ] Cover OAuth authorization URL fields (`state`, `redirect_uri`, `client_id`), callback state validation and code extraction, and token exchange request shape.
- [ ] Cover `make_token_ref`/`validate_token_ref` round-trip and expiration behavior.
- [ ] Cover service registry upsert and 404 semantics, `verify()` status transitions, and `reset_mail_connector_service()`.
- [ ] Cover HTTP list/create/verify/delete contracts and assert that secrets are never persisted or returned; only `token_ref` is exposed.
- [ ] Run `pytest tests/backend/test_mail_connector.py` and retain green output as the prerequisite evidence for T003.

**Verification:**

```text
pytest tests/backend/test_mail_connector.py
```

**Exit evidence:** the test file exists, has ≥20 tests, all listed behaviors are represented, and the focused command passes.

### T003 — Mail-connector plane commit (G-0.1)

- [ ] Confirm T002's focused test command is green before committing.
- [ ] Confirm the Crash A re-export in [`backend/connectors/mail/__init__.py`](../../backend/connectors/mail/__init__.py) exposes `MailConnector` from [`backend/api/models.py`](../../backend/api/models.py).
- [ ] Commit the verified mail-connector plane as the required G-0.2 mail-connector commit, keeping the connectors, web theme, and web downloads work as 2–3 logical commits per the plan; do not create one monolithic commit.
- [ ] Ensure the router mount and existing endpoint/OAuth contracts remain unchanged.
- [ ] After the G-0.2 commit, verify no mail-plane files remain staged or uncommitted unexpectedly and no tracked [`web/dist/index.html`](../../web/dist/index.html) entry is included in the resulting clean-tree evidence.
- [ ] Pass the resulting commit identifier to T008; T008 must treat this commit as a hard dependency, not as an assumed or reconstructed state.

**Verification:**

```text
git status --short
git diff --check
pytest tests/backend/test_mail_connector.py
pytest tests/ -m "not integration"
git log -n 3 --oneline -- backend/connectors/mail
```

**Exit evidence:** T002's green result, the G-0.2 mail-connector commit identifier, logical commit boundaries, and post-commit status output.

### T004 — Build artifacts and worker output (G-0.3)

- [ ] Verify [`web/Dockerfile`](../../web/Dockerfile) or the relevant [`Dockerfile.api`](../../Dockerfile.api) build stage runs `npm run build` in-image before relying on an untracked local bundle.
- [ ] Run `git rm -r --cached web/dist` to untrack the generated bundle without deleting local files.
- [ ] Confirm [`web/dist/`](../../web/dist/) and the existing `dist/` rule remain in [` .gitignore`](../../.gitignore) (use the actual root file [` .gitignore`](../../.gitignore); no new duplicate rule is needed).
- [ ] Add `.patches/` to the build-artifact/worker-output section of [` .gitignore`](../../.gitignore).
- [ ] Rebuild the web app and confirm generated output does not reappear as tracked changes.

**Verification:**

```text
git rm -r --cached web/dist
grep -nE '(^|/)(dist/|web/dist/)|^\.patches/' .gitignore
cd web && npm run build && cd ..
git ls-files web/dist
git status --short
```

**Exit evidence:** `git ls-files web/dist` is empty, `.gitignore` contains the required rules, `.patches/*` is absent from status, and the rebuild succeeds without tracked artifact churn. If the Dockerfile path differs, record the verified build-stage path explicitly.

### T005 — E2E masks / Apple test runner (G-4.1)

- [ ] Investigate the default [`apple/Package.swift`](../../apple/Package.swift) test-target resolution and platform gating.
- [ ] Prefer a root-cause fix that makes `swift test` run `NerveAppsTests`, `NerveRelayTests`, and `NerveSharedTests` by default without breaking the build.
- [ ] If SwiftPM cannot provide that default behavior safely, add executable [`apple/scripts/test-apple.sh`](../../apple/scripts/test-apple.sh) that runs all three bundles explicitly with `swift test --filter` and fails on any bundle failure.
- [ ] Ensure the canonical runner is suitable for the e2e-mask/CI invocation and is documented by T010; do not count a two-test default run as complete.

**Verification:**

```text
cd apple && swift build && swift test
./apple/scripts/test-apple.sh
```

Run the applicable command (default test or fallback script) and retain evidence that all three bundles ran and 33+ tests passed. If the fallback script is used, the first command may still show SwiftPM's incomplete default behavior and must be reported rather than hidden.

### T006 — FastAPI lifespan migration (G-4.2)

- [ ] Replace `on_event("startup"/"shutdown")` in [`backend/api/wiring.py`](../../backend/api/wiring.py) with an `@asynccontextmanager` lifespan handler.
- [ ] Preserve startup-before-request and shutdown-after-request ordering for `wire_dependencies(target)` and `teardown_dependencies(target)`.
- [ ] Wire the lifespan into both the module-level `app` and `create_app()` in [`backend/api/app.py`](../../backend/api/app.py), preserving T003's router mount.
- [ ] Keep [`tests/backend/test_app_wiring.py`](../../tests/backend/test_app_wiring.py) green and confirm no `on_event` usage remains in the migrated path.

**Verification:**

```text
pytest tests/backend/test_app_wiring.py
pytest tests/ -m "not integration" -W always
rg 'on_event|asynccontextmanager|lifespan' backend/api/wiring.py backend/api/app.py
```

**Exit evidence:** full non-integration tests pass, startup/shutdown assertions remain green, and the warning count attributable to FastAPI `on_event` is zero.

### T007 — Structural documentation refresh (G-4.3)

- [ ] Refresh [`docs/implementation-status.yaml`](../../docs/implementation-status.yaml) against the real tree.
- [ ] Refresh [`docs/architecture/current-state.md`](../../docs/architecture/current-state.md) to list personal planes P0–P4, the mail-connector plane, policy PDP, event bus, persistence, and JWT/SSO auth with correct paths.
- [ ] Correct stale top-level references to `backend/events/` and `backend/knowledge/` where they do not exist.
- [ ] Leave [`audit-gaps.md`](../../audit-gaps.md) unchanged and limit this task to structural accuracy, not a full evidence re-verification.

**Verification:**

```text
python - <<'PY'
from pathlib import Path
for path in (Path('docs/implementation-status.yaml'), Path('docs/architecture/current-state.md')):
    assert path.exists(), path
    assert path.read_text()
print('status documents present and non-empty')
PY
rg 'backend/events/|backend/knowledge/' docs/implementation-status.yaml docs/architecture/current-state.md
```

The `rg` command must produce no stale top-level-path matches; validate YAML syntax with the repository's available Python/YAML tooling if installed and record the result.

### T009 — JWT test-key hygiene (G-4.4)

- [x] Replace the short HS256 fixtures in [`tests/backend/test_auth_sso.py`](../../tests/backend/test_auth_sso.py) with fixed keys longer than the RFC 7518 minimum.
- [x] Preserve the test contract and avoid logging key material.

**Verification:**

```text
pytest tests/backend/test_auth_sso.py -W always
```

**Exit evidence:** the focused suite passes and produces zero PyJWT `weak key` warnings.

### T010 — Canonical test commands in README (G-4.5)

- [ ] Add a `Testing` section to [`README.md`](../../README.md).
- [ ] Document Python: `pip install -e ".[dev]"` and `pytest tests/ -m "not integration"`.
- [ ] Document web Playwright e2e: `cd web && npm install && npm run e2e`.
- [ ] Document Apple: `cd apple && swift build && swift test`, or [`apple/scripts/test-apple.sh`](../../apple/scripts/test-apple.sh) when T005 uses the fallback.
- [ ] State expected results without presenting them as verified until the corresponding command evidence exists (for example, `559+ passed, 0 failed`).

**Verification:**

```text
rg -n 'Testing|pytest tests/ -m|npm run e2e|swift (build|test)|test-apple\.sh' README.md
```

**Exit evidence:** the section contains all three canonical suite commands and accurately describes expected results and the selected Apple runner.

### T008 — Final integration gate

- [ ] Confirm T002 is green and T003's G-0.2 mail-connector commit identifier is recorded.
- [ ] Reconcile the separate T003/T006 edits to [`backend/api/app.py`](../../backend/api/app.py).
- [ ] Run the full non-integration Python suite, web e2e suite, and all-bundle Apple runner.
- [ ] Confirm the web rebuild does not reintroduce tracked [`web/dist/`](../../web/dist/) files or `.patches/*` entries.
- [ ] Confirm all AC001–AC008 have a corresponding evidence entry and no unresolved gap is papered over.

**Verification:**

```text
pytest tests/ -m "not integration"
cd web && npm run e2e && npm run build && cd ..
./apple/scripts/test-apple.sh
# If T005 fixed SwiftPM default resolution instead:
cd apple && swift test && cd ..
git ls-files web/dist
git status --short
git diff --check
```

**Exit evidence:** all applicable suites pass, the G-0.2 commit is present, generated artifacts are untracked/ignored, and the final status/diff checks satisfy AC001–AC008. Record environment limitations (for example unavailable Apple or browser tooling) explicitly.

## Verification log

| Date | Task/AC | Command or evidence | Result | Notes / commit |
|---|---|---|---|---|
| 2026-10-14 | Baseline | `pytest tests/ -m "not integration"`; `swift build`; web build | Baseline from [`plan.md`](./plan.md) | 559 Python tests and existing build state as documented; task execution evidence still required |
|  | T002 / AC001 |  | Pending |  |
|  | T003 / AC002 |  | Blocked until T002 | Must include the G-0.2 mail-connector commit identifier |
|  | T004 / AC003 |  | Pending |  |
|  | T005 / AC004 |  | Pending |  |
|  | T006 / AC005 |  | Pending |  |
|  | T007 / AC006 |  | Pending |  |
|  | T009 / AC007 | `.venv/bin/python -m pytest tests/backend/test_auth_sso.py -W always -q` | Passed: 9 passed, 22 warnings; no PyJWT `InsecureKeyLengthWarning` remains | FastAPI `on_event`, Starlette/httpx, and SQLAlchemy warnings remain for separate T006/dependency cleanup |
|  | T010 / AC008 |  | Pending |  |
|  | T008 / AC001–AC008 |  | Blocked until all prerequisites | Final clean-tree and all-suite gate |

## AC coverage matrix

| Acceptance criterion | Implementing task | Required evidence |
|---|---|---|
| AC001 | T002 | ≥20 focused mail tests and green `pytest tests/backend/test_mail_connector.py` |
| AC002 | T003 | 2–3 logical commits, including the G-0.2 mail-connector commit, plus clean post-commit status evidence |
| AC003 | T004 | `web/dist` absent from `git ls-files`, `.patches/` ignored, successful rebuild |
| AC004 | T005 | Default `swift test` or fallback script runs all three bundles and 33+ tests pass |
| AC005 | T006 | Lifespan implementation, green app-wiring tests, zero FastAPI `on_event` warnings |
| AC006 | T007 | Both status documents path-accurate and stale top-level references removed |
| AC007 | T009 | ≥64-byte HS256 fixtures and zero PyJWT weak-key warnings |
| AC008 | T010 | README Testing section with Python, web, and Apple commands plus expected results |
