# Tasks: Phase 0 — Integrity Gate

**Spec:** [`spec.md`](./spec.md)
**Plan:** [`plan.md`](./plan.md)
**Status:** Done (2026-10-15)
**Scope:** gaps2.md §3 findings H-1…H-5 (G-0.1/G-0.2/G-0.3/G-0.4 + the uncommitted T009), executed as the six-commit integrity slice defined by gaps2 §5 Phase 0.

## Execution rules

- Keep the task order and ownership below. P0-01, P0-02, P0-03, and P0-05 are independent and may run in one wave; P0-04 must wait for all of them; P0-06 last.
- **Commit-hygiene norm (gaps2 §7 Q6, made a standing rule; seconded by gaps4 Phase 5 E5):** two clauses, both must hold for a commit to ship.
  - *(a) Generated project:* no commit that touches a **generated project** (e.g. the Apple Xcode projects generated from `apple/Distribution/xcodegen/*.xcodegen.yml`) **and its patcher** ships without the patcher — a generated artifact and its generator must move together; commit the regenerated `apple/Distribution/Generated/` tree alongside the xcodegen/`Fastfile` change that produced it.
  - *(b) Import chain:* no commit that touches a `backend/**` import chain — in this pack, commit 1 (`fix(connectors)`) — ships without `python -c "import backend.api.app"` verified in a **detached worktree of the resulting commit**, never just in the working tree. This is the check that would have caught H-1.
- **Fix + tests ship together:** the `fix(connectors)` commit must contain both `backend/connectors/mail/__init__.py` (the worktree fix) and `tests/backend/test_mail_connector.py` (P0-03 output). Never commit the import fix alone.
- Do not modify any file outside the owned paths above. The mail-plane *source* is frozen: P0-03 writes tests against it; if a test reveals a real plane defect, stop and report it — do not silently "fix" the plane.
- Record commit SHAs and command output in the verification log below. Do not claim a check passed without evidence.

## Dependency board

| ID | Workstream | Depends on | Owned paths | AC coverage | Status |
|---|---|---|---|---|---|
| P0-01 | H-2 calendar time-bombs | None | [`tests/backend/test_calendar.py`](../../tests/backend/test_calendar.py) | AC-P01 | [x] |
| P0-02 | H-3 web theme rename completion | None | `web/src/components/ThemeToggle.tsx` (add), `web/src/components/Layout.tsx`, `web/src/components/ThemeSwitcher.tsx` (delete) | AC-P02 | [x] |
| P0-03 | G-0.2 mail-connector tests | None | [`tests/backend/test_mail_connector.py`](../../tests/backend/test_mail_connector.py) (new) | AC-P03 | [x] |
| P0-05 | G-0.3 artifact untrack + ignore | None (commit lands before P0-06) | [`.gitignore`](../../.gitignore), `web/dist/` (index only) | AC-P05 | [x] |
| P0-04 | G-0.1/H-1/H-4/H-5 commit slice (6 commits) | P0-01, P0-02, P0-03, P0-05 all green + in working tree | Git commits bundling the worktree surface | AC-P03 (commit shape), AC-P04, AC-P06 | [x] |
| P0-06 | Final integration gate | P0-04 | Verification only; this file's log | AC-P01…AC-P07 | [x] |

## Detailed task checklists

### P0-01 — Calendar clock time-bombs (H-2)

- [x] In [`tests/backend/test_calendar.py`](../../tests/backend/test_calendar.py), apply the gaps2 §3.2 diffs to the two *API* tests (the `test_service_reset_hook` fix is already in the worktree — do not touch it):
  - `test_ingest_new_is_201_replay_is_200`: replace the bare `client.get("/api/calendar")` with `params={"start": at(0).isoformat(), "end": at(24).isoformat()}` before the `len(data) == 1` assertion.
  - `test_list_filters_to_window`: replace the first bare `client.get("/api/calendar")` with `params={"start": at(0).isoformat(), "end": at(30).isoformat()}` (covers e1 at day-1 09:00 and e2 at day-2 01:00); leave the narrow-window assertion below it unchanged.
- [x] Add a one-line NOTE comment on each, mirroring the reset-hook NOTE: the default window is anchored at the real clock (`[now, now+7d)`), so a fixed-date fixture needs an explicit window to stay clock-invariant.
- [x] Do **not** re-anchor `_day()` to `datetime.now()` and do **not** modify `backend/calendar/service.py`.

**Verification:**

```text
pytest tests/backend/test_calendar.py
rg -n 'datetime\(2026' tests/   # should only appear in window-explicit fixtures
```

**Exit evidence:** `pytest tests/backend/test_calendar.py` → **20 passed, 0 failed**; the generalized rule holds.

### P0-02 — Complete the web theme rename (H-3)

- [x] `git add web/src/components/ThemeToggle.tsx` (the only file referencing `useTheme` besides the tracked `ThemeContext.tsx`).
- [x] `git rm web/src/components/ThemeSwitcher.tsx` (tracked, referenced by nothing after the `Layout.tsx` rename — confirmed by `rg -n 'ThemeSwitcher' web/src web/tests` returning only its own definition).
- [x] Confirm the worktree `Layout.tsx` diff (3 sites: import + 2 usages) is the only web change; no other file imports the old name.
- [x] Re-run the web build **and** e2e after the delete (the last verified build had both files present): `cd web && npm run build && npm run e2e`.
- [ ] If the Q1 decision flips (revert instead of complete): revert `Layout.tsx` to HEAD, delete `ThemeToggle.tsx`, keep `ThemeSwitcher.tsx` — and update AC-P02 accordingly. (Not triggered — the rename was completed; plan assumption held.)

**Verification:**

```text
git ls-files web/src/components/ | rg 'Theme'
rg -n 'ThemeSwitcher' web/src web/tests   # expect: no matches
cd web && npm run build && npm run e2e && cd ..
```

**Exit evidence:** `ThemeToggle.tsx` tracked, `ThemeSwitcher.tsx` gone, zero stale references, `tsc && vite build` clean, **56 e2e passed**.

### P0-03 — Mail-connector test suite (G-0.2)

- [x] Create [`tests/backend/test_mail_connector.py`](../../tests/backend/test_mail_connector.py) with **≥20 tests**, mirroring the in-memory service / HTTP `TestClient` / reset-hook / envelope-assertion pattern of [`tests/backend/test_email.py`](../../tests/backend/test_email.py):
  - IMAP probe mapping: `CAPABILITY` ok → `CONNECTED`; auth rejection → `AUTH_EXPIRED`; network failure → `ERROR`.
  - OAuth: `build_authorization_url` fields (`state`, `redirect_uri`, `client_id`); `parse_callback` state validation + code extraction; `exchange_request` shape.
  - `make_token_ref` / `validate_token_ref`: round-trip and expiration.
  - Service registry: upsert semantics, 404 on missing id, `verify()` status transitions, `reset_mail_connector_service()`.
  - HTTP endpoints: list/create/verify/delete; assert the secret is **never** persisted or returned — only `token_ref`.
- [x] Tests target the existing plane contract (list/create/verify/delete + OAuth). If a test exposes a genuine plane defect, **stop and report** — do not modify the plane inside this task.
- [x] Run the focused suite; it must be green before P0-04 bundles it. (37 passed)

**Verification:**

```text
pytest tests/backend/test_mail_connector.py
```

**Exit evidence:** file exists, ≥20 tests, all behaviors above represented, focused command green. Retain the output as P0-04's prerequisite.

### P0-05 — Untrack build artifacts, ignore worker output (G-0.3)

- [x] Re-confirm [`web/Dockerfile`](../../web/Dockerfile) builds in-image: `RUN npm run build` (line 33) + `COPY --from=build /app/dist/` (line 49). If the build stage is missing or changed, add/restore it **before** untracking.
- [x] `git rm -r --cached web/dist` (index only; keep local files for dev builds).
- [x] Add `.patches/` to `.gitignore` under the build-artifacts/worker-output section (verify the existing `dist/` (line 15) and `web/dist/` (line 42) rules remain).
- [x] Rebuild the web app and confirm generated output does not reappear as tracked changes. (`npm run build` green; `git status` shows no `web/dist` churn)
- [x] This work is committed as its own `chore` commit inside P0-04 (commit 6) — do not leave it loose in the working tree past P0-04. (commit 6 → 13cefdc)

**Verification:**

```text
rg -n 'npm run build|COPY --from' web/Dockerfile
git rm -r --cached web/dist
grep -nE '(^|/)dist/|^\.patches/' .gitignore
cd web && npm run build && cd ..
git ls-files web/dist
git status --short
```

**Exit evidence:** `git ls-files web/dist` empty; `.gitignore` has the `.patches/` rule; `.patches/*` and `web/dist/*` absent from status; rebuild succeeds without tracked-artifact churn.

### P0-04 — Commit the integrity slice as logical commits (G-0.1/H-1/H-4/H-5)

Execute on the working tree produced by P0-01/P0-02/P0-03/P0-05, in this order. Record each SHA in the verification log.

- [x] **Commit 1 — `fix(connectors): re-export MailConnector from mail plane (HEAD boot breakage)`** → `6bdda63`
  - `git add backend/connectors/mail/__init__.py tests/backend/test_mail_connector.py`
  - **Must contain both files** (the plane lands with its tests — the H-1 gate).
  - After committing, run the detached-worktree import gate on this commit (see below) and record the result. → IMPORT_OK
- [x] **Commit 2 — `test(hygiene): clock-invariant calendar window asserts + RFC7518-sized SSO keys`** → `85e681c`
  - `git add tests/backend/test_calendar.py tests/backend/test_auth_sso.py` (the t001 delta banner is spec authoring — see commit 7 delta note below).
- [x] **Commit 3 — `feat(web): complete ThemeSwitcher→ThemeToggle rename`** → `3f14891`
  - `git add web/src/components/Layout.tsx web/src/components/ThemeToggle.tsx` + the staged `git rm web/src/components/ThemeSwitcher.tsx`.
  - Pre-verified by P0-02: `npm run build` + `npm run e2e` (56) green with this exact file set.
- [x] **Commit 4 — `docs: add gaps1.md + gaps2.md (gap reviews)`** → `dcf1bfd`
  - `git add gaps1.md gaps2.md` — the gap docs live beside `audit-gaps.md`.
- [x] **Commit 5 — `chore: swift debug launch configs for NervePersonalRelay (workspace)`** → `59d946c`
  - `git add nervcenter.code-workspace`
- [x] **Commit 6 — `chore: untrack web/dist, ignore .patches/ (worker output)`** → `13cefdc`
  - The staged changes from P0-05 (`.gitignore` + `git rm -r --cached web/dist`).
  - **Commit 7 (delta, outside the plan's six):** the spec-authoring surface (this pack + `specs/gap-closure-t003/{plan,spec}.md` deltas + the `specs/t001-stabilization/tasks.md` gaps2 banner) is a 7th `docs(specs)` commit so `git status --short` ends empty; the plan's six-commit shape is otherwise intact.

**Detached-worktree import gate (after commit 1 and after commit 6):**

```text
git worktree add /tmp/nc-p0-gate <sha>
(cd /tmp/nc-p0-gate && python -c "import backend.api.app" && echo IMPORT_OK)
git worktree remove /tmp/nc-p0-gate
```

**Verification (per commit):**

```text
git log -n 6 --stat --oneline
git status --short        # must be empty after commit 6
```

**Exit evidence:** six SHAs in order; `git show --stat` for each in the verification log; `IMPORT_OK` for the final commit; clean status.

### P0-06 — Final integration gate

- [x] On the **final commit** (not the worktree): detached worktree of the final SHA; `python -c "import backend.api.app"` → exit 0. (Ran on 13cefdc — the slice tail; the docs commit 7 adds spec files only, cannot affect the import chain; final SHA re-verified post-commit.)
- [x] `.venv/bin/python -m pytest tests/ -m "not integration"` → **596 passed, 0 failed** (≥559 threshold met).
- [x] `npx playwright test --reporter=line` → **56 passed** (27.4s, from `web/`; mock backend boots/tears down clean).
- [x] Apple evidence: `cd apple && swift test` (full) → `All tests passed`, exit 0. The plan's per-bundle `--filter` names are stale (filter runs report cached/0-test because of stale `.build` products, not because the tests don't exist). Canonical per-bundle counts (2026-10-18, gaps4.md D3): **NerveSharedTests 13, NerveRelayTests 31, NerveAppsTests 2 = 46 total**; the stale-product under-run is now guarded by `apple/scripts/test-apple.sh` (touch + `swift build --build-tests` + count assertion) wired into CI `.github/workflows/ci.yml`; Swift-Testing macro count is 0 by design.
- [x] `git status --short` → empty (post commit 7); `git ls-files web/dist` → empty (since commit 6).
- [x] AC-P01…AC-P06 each have an evidence row in the log below; the import-chain norm (AC-P07) is present in this pack's execution rules and (after the gaps2 delta amendments) in the sibling packs' rules.
- [x] If the gate fails, reopen the offending task (P0-01…P0-05); do not patch around it here. (Gate passed; no reopen.)

**Verification:**

```text
git worktree add /tmp/nc-p0-gate HEAD
(cd /tmp/nc-p0-gate && python -c "import backend.api.app" && echo IMPORT_OK)
.venv/bin/python -m pytest tests/ -m "not integration" -q
cd web && npx playwright test --reporter=line && cd ..
cd apple && swift test --filter NerveAppsTests && swift test --filter NerveRelayTests && swift test --filter NerveSharedTests && cd ..
git status --short
git ls-files web/dist
git worktree remove /tmp/nc-p0-gate
```

**Exit evidence:** `IMPORT_OK` on the commit; full Python suite ≥559 passed / 0 failed; e2e 56 passed; all three Apple bundles green (or the tooling limitation recorded explicitly); clean status; empty `web/dist` index.

## Verification log

| Date | Task/AC | Command or evidence | Result | Notes / commit |
|---|---|---|---|---|
| 2026-10-14 | Baseline (plan) | `pytest tests/backend/test_calendar.py`; `git status --short`; `git show HEAD:backend/connectors/mail/__init__.py`; `rg -n 'npm run build\|COPY --from' web/Dockerfile`; `git ls-files web/dist`; `rg -n 'patches' .gitignore` | Verified live: 2 failed/18 passed calendar; 7 M + 4 untracked; HEAD mail `__init__` lacks `MailConnector`; Dockerfile builds in-image; `web/dist/index.html` tracked; no `.patches` rule | See [`plan.md`](./plan.md) ground-truth table |
| 2026-10-15 | P0-01 / AC-P01 | `pytest tests/backend/test_calendar.py -q` | 20 passed | Explicit `[at(0), at(24))` window on the ingest/replay asserts + NOTE comments; clock-invariant (lands in commit 2, 85e681c) |
| 2026-10-15 | P0-02 / AC-P02 | `npm run build`; `npx playwright test --reporter=line` | build green (64 modules); 56/56 passed | ThemeToggle.tsx tracked + Layout.tsx rewired + ThemeSwitcher.tsx deleted, atomic (commit 3, 3f14891); no e2e reference to the 3-way control anywhere |
| 2026-10-15 | P0-03 / AC-P03 | `pytest tests/backend/test_mail_connector.py -q` | 37 passed | New suite: probe→status map, OAuth URL/callback/exchange shapes, token-ref round-trip, raw-secret rejection, registry 404/reset, HTTP CRUD + verify never persists credential (bundled in commit 1, 6bdda63) |
| 2026-10-15 | P0-05 / AC-P05 | `git rm -r --cached web/dist`; `.gitignore` `.patches/` | staged | `git ls-files web/dist` empty after commit 6 (13cefdc); `git check-ignore .patches/worker-1.patch` → `.gitignore:25:.patches/`; Dockerfile builds dist in-image, nothing depends on the tracked file |
| 2026-10-15 | P0-04 / AC-P03, AC-P04, AC-P06 | `git log -n 6 --stat --oneline` | 6bdda63, 85e681c, 3f14891, dcf1bfd, 59d946c, 13cefdc | Commit shape per plan with two deltas: (1) the t001-stabilization delta banner is spec authoring → lands with commit 7 instead of commit 2; (2) commit 1 is exactly the 2-file fix+tests bundle (an earlier commit that swept in the staged dist deletion was soft-reset and recommitted) |
| 2026-10-15 | P0-04 import gate (commit 1) | detached worktree of 6bdda63, `.venv/bin/python -c "import backend.api.app"` | IMPORT_OK | The plan's bare `python` has no deps in a fresh worktree — used the main checkout's venv interpreter from the worktree cwd (same import surface) |
| 2026-10-15 | P0-04 import gate (commit 6) | detached worktree of 13cefdc, same command | IMPORT_OK | Gate re-run on the slice's tail before commit 7 |
| 2026-10-15 | P0-06 / AC-P01…AC-P07 | see rows below | PASS | Full gate, pre-commit-7 (tree identical modulo the docs commit) |
| 2026-10-15 | P0-06 pytest | `.venv/bin/python -m pytest tests/ -m "not integration" -q` | 596 passed, 0 failed | ≥559 threshold met |
| 2026-10-15 | P0-06 e2e | `npx playwright test --reporter=line` (from web/) | 56 passed (27.4s) | mock backend boot/teardown clean |
| 2026-10-15 | P0-06 apple | `cd apple && swift test` (full) | All tests passed, exit 0 | Per-bundle `--filter` names in the plan are stale (filter runs report cached/0-test on stale `.build` products). Canonical counts as of 2026-10-18: NerveSharedTests (13), NerveRelayTests (31), NerveAppsTests (2) = 46 total; the under-run trap is now guarded by `apple/scripts/test-apple.sh` (CI `ci.yml`); Swift-Testing macro count is 0 by design |
| 2026-10-15 | P0-06 clean tree | `git status --short`; `git ls-files web/dist` | empty / empty (post-commit-7) |  |

## AC coverage matrix

| Acceptance criterion | Implementing task | Required evidence |
|---|---|---|
| AC-P01 (H-2) | P0-01 | 20/20 calendar tests green; NOTE comments; generalized grep rule clean |
| AC-P02 (H-3) | P0-02 | `ThemeToggle` tracked / `ThemeSwitcher` deleted / zero stale refs / build clean / e2e 56 |
| AC-P03 (H-1, G-0.2, G-0.4) | P0-03 + P0-04 (commit 1) | ≥20 mail tests green; `fix(connectors)` commit contains fix **and** tests; detached-worktree `IMPORT_OK` |
| AC-P04 (H-4) | P0-04 (commit 2) | SSO keys + T009 log row committed; 0 PyJWT weak-key warnings |
| AC-P05 (H-5, G-0.3) | P0-05 + P0-04 (commit 6) | `git ls-files web/dist` empty; `.patches/` ignored; rebuild clean |
| AC-P06 (G-0.1) | P0-04 | Six ordered commits; gap docs tracked; `git status` empty; SHAs logged |
| AC-P07 (G-0.4, gaps2 §7 Q6) | P0-04 + P0-06 | Detached-worktree import + full suite + e2e on the **final commit**; import-chain norm recorded in this pack and propagated to the sibling packs' execution rules |
