# NerveCenter — Gap Review & Closure Plan, Fourth Pass (`gaps4.md`)

**Date:** 2026-10-18 (≈15:06–15:30 UTC)
**Supersedes / extends:** `gaps3.md` (2026-10-16) and `gaps-apple1.md` (2026-10-18, Apple-plane
focus). Where prior claims were re-verified, went stale, or were **wrong**, this file says so
explicitly. `audit-gaps.md` remains in force for web design-bar items it owns.
**Scope:** Re-review of `prompt1.md` (core spec) + `prompt2.md` (Personal Ops addendum) against the
live tree at `35d3e78` (2026-10-16) **+ a large body of uncommitted work landed since `gaps3.md`**
(the Apple data-loading lift, the T003-06 XCUITest harness, the watch-as-IPA distribution change,
harbor deploy fix, docs), with **fresh runs of every suite** and a diff-level review of the
uncommitted tree.
**Method:** full section-map read of both prompts; fresh runs of `pytest` (non-integration),
`npm run build` + `npx playwright test`, `swift build && swift test` (bare and forced-rebuild);
`git diff` of all 24 modified files; `git ls-tree` archaeology on unreachable snapshot commits;
grep re-verification of every "still missing" claim in `gaps3.md`.

---

## 1. Executive summary — the delta since `gaps3.md`

`gaps3.md` (2026-10-16) found: Python 601 green, web 60 green, Apple 46 green (after a manual
forced rebuild that exposed the stale-test-product under-run trap). Since then, **~24 tracked files
were modified and ~15 untracked paths added**, none committed. The delta is almost entirely
**Apple distribution + a verified data-loading lift**, and this pass found:

1. **NEW FAILING TEST (D1):** `tests/apple/test_generate_manifest.py::test_missing_artifacts_marked_not_present`
   fails against the uncommitted `generate_manifest.py` rewrite — the test still expects the old
   artifact names (`NerveWatch.app`, `NerveWatchWidgetExtension.watchkitarchive`) while the new
   code ships the watch as a self-contained `NerveWatch.ipa` with an embedded
   `NerveWatchWidgetExtension.appex`. Python suite is now **600 passed, 1 failed** (was 601/0).
   The code change is the right shape; the test is stale. One small fix.

2. **NEW BUNDLE-ID INCONSISTENCY (D2 — the one real code bug in the new tree):** the uncommitted
   work disagrees with itself on the watch bundle-ID family:
   - `xcodegen/NerveWatch.xcodegen.yml` (source of truth) → `com.celesrenata.NervePhone.watch` / `.watch.widgets`
   - hand-edited `Generated/NerveWatch-Info.plist` → `com.celesrenata.NervePhone.watch`
   - hand-edited `Generated/NerveWatch.xcodeproj/project.pbxproj` → `com.celesrenata.NerveWatch` / `.NerveWatch.widget`
   - uncommitted `generate_manifest.py` → `com.celesrenata.NerveWatch` / `.NerveWatch.widget`
   - untracked `e2e/scripts/baseline_audit.sh` → `com.celesrenata.NervePhone.watch`
   A single `xcodegen generate` regenerates the pbxproj with the `.NervePhone.watch.*` IDs and
   every hand-edited file diverges from the actual build; the manifest script would then record
   bundle IDs that don't match the IPA it ships. Must be converged on **one** family (recommend
   `com.celesrenata.NervePhone.watch.*` — it matches the xcodegen source of truth,
   `WKCompanionAppBundleIdentifier`, and the phone-alignment intent of commit `35d3e78`).

3. **Apple under-run trap recurred — worse (D3):** `gaps3.md` §3 documented the stale
   `.build` test-product under-run and proposed a structural fix (canonical command + CI) that
   **has not landed**. This pass a bare `cd apple && swift test` reported **0 tests** in all
   three bundles ("Test run with 0 tests … passed") — a complete false green, down from
   `gaps3`'s 2-of-46. After `touch`-ing the test sources + `swift build --build-tests` the full
   **46/46** ran and passed. The trap is live and `build_and_test.sh` still inherits it verbatim.

4. **The T003-06 verification path is ~80% built, uncommitted (progress, not a gap):** the
   XCUITest harness (`apple/e2e/XCUITestSupport/NerveE2E.swift`, `NervePhoneUITests`,
   `baseline_audit.sh`), the launch-arg routers (`--nerve-tab` / `--nerve-view` / `--nerve-dump` /
   `--nerve-server`), accessibility identifiers on phone tabs + watch screens, the
   `patch_uitests.py` xcodegen patcher, and baseline audit evidence (`docs/audit/
   2026-10-18-baseline/`) all exist and are verified working (see `gaps-apple1.md` §2). This
   moves `gaps3` D5/T003-06 from OPEN to PARTIAL. But it is tangled with the distribution commit
   (the generated pbxproj carries the UITest-target patch from `patch_uitests.py`, which is
   untracked) — committing them separately breaks regeneration.

5. **Watch contract half-built (D6):** the watch's `WatchSnapshotSource` expects **three** files
   in `group.nerv.center` (`cached-state.json`, `approvals.json`, `stop-agents.json`); the phone's
   new `publishToAppGroup` writes **only** `cached-state.json` (grep: no `approvals.json` write
   anywhere in `apple/iOS`). So even on a paired device the watch Approvals screen has no data
   source. `gaps-apple1.md` G3's "publish verified" covers only the snapshot file.

6. **Security hygiene (D5):** `stabulous.p12` (a signing key) was dropped at the repo root on
   2026-10-16; the new `.gitignore` rules correctly keep it + `fastlane/.env` +
   `credentials/*` out of git on `main` (verified: `git ls-files` shows neither). But the key
   **and** `apple/Distribution/credentials/nervecenter.p12[.old]` + `fastlane/.env.example`
   **exist as blobs in unreachable "Zoo parallel task snapshot" commits** (e.g. `a91b896`,
   `git log --all` only — not reachable from `main`). If this repo is ever pushed, those refs go
   with it. The `.gitignore` comment ("rotate it; remove from history") records the intent; the
   rotation + purge has not happened, and the local `stabulous.p12` file still sits in the
   worktree.

7. **Everything from `gaps3.md` §6 that was OPEN is still OPEN, re-verified this pass** (T1
   reasoning brain + persistence, OmniRoute transport, ask 202-ACK stub, synthetic-lab, personal
   MCP/CLI, Apple personal surfaces, relay channels, CI, on_event, README testing, status docs).
   See §5 ledger. The only status change: G-2.5 (verify path) OPEN→PARTIAL (item 4 above).

**Bottom line:** Phase 0 (≤ 1 day) lands the uncommitted tree *coherently* — fix the manifest
test, converge the watch bundle IDs, strip junk, commit in 4 slices, gate everything. Phase 1
lands the test-truth infrastructure `gaps3` called for (canonical Apple test command + CI) —
it has now failed us twice and is the highest-leverage structural item. Then `gaps3` Phases
B–E execute substantially unchanged, with T2 (Apple personal surfaces) re-scoped around the
now-landed harness.

---

## 2. Verified current state (evidence, 2026-10-18 this session)

| Area | State | Evidence (command, this session) |
|---|---|---|
| **Tree** | 🟡 HEAD `35d3e78` + 24 modified + ~15 untracked paths (see §3.1 inventory) | `git status --short`; `git diff --stat` |
| **Python suite** | 🔴 **600 passed, 1 failed** (D1) | `.venv/bin/python -m pytest tests/ -m "not integration" -q` → `1 failed, 600 passed in 4.54s` |
| — failing test | `tests/apple/test_generate_manifest.py::test_missing_artifacts_marked_not_present` → `KeyError: 'NerveWatch.app'` (old name; uncommitted manifest rewrite renamed it) | pytest output; `git diff apple/Distribution/generate_manifest.py` |
| **Web build** | ✅ clean (66 modules) | `cd web && npm run build` |
| **Web e2e** | ✅ **60 passed** (28.9s, chromium) | `npx playwright test --reporter=line` |
| **Apple build** | ✅ `swift build` clean | `cd apple && swift build` (58s) |
| **Apple tests (bare)** | 🔴 **0 tests run** — "Test run with 0 tests in 0 suites passed" × 3 bundles — the `gaps3` under-run trap, worse (D3) | `cd apple && swift test` before rebuild |
| **Apple tests (forced)** | ✅ **46 passed, 0 failed** after `touch` test sources + `swift build --build-tests` | per-bundle: NerveSharedTests 13 / NerveRelayTests 31 / NerveAppsTests 2 |
| **Phone data loading** | ✅ **Built + verified this session's predecessor** — fetch-on-load, `--nerve-server` override, App Group publish with live values (14 agents / healthy / "1s ago") | `docs/apple-data-lift-2026-10-18.md` + `docs/audit/2026-10-18-baseline/phone/*` (untracked, present in tree) |
| **Watch read path** | ⚠️ code-correct; publish verified on phone; **read still unverified** (standalone watch-sim App Group isolation — env limit) **and `approvals.json` is never published at all (D6)** | `WatchSnapshotSource.swift:17-27`; grep `approvals.json` in `apple/iOS` → 0 hits |
| **T003-06 harness** | 🟡 PARTIAL, uncommitted — `NerveE2E.swift`, `NervePhoneUITests`, `baseline_audit.sh`, launch-arg routers, `patch_uitests.py`, audit evidence | `apple/e2e/**` (untracked); `apple/Distribution/patch_uitests.py` (untracked); baseline run logs in script output |
| **Watch bundle IDs** | 🔴 inconsistent across 5 artifacts (D2) | §1.2 list; `git diff` of pbxproj/plist/manifest/xcodegen |
| **MCP tools** | ✅ 18 (§45); **0** of the 11 §64 personal | `python -c "from mcp.tools_module import _SPEC; print(len(_SPEC))"` → `18` |
| **CLI commands** | ✅ 12 modules; **0** personal (§65) | `ls cli/commands/` |
| **Reasoning "brain"** | ❌ `backend/{communications,followups,drafts,persons,persons}` **do not exist** | `ls backend/` (all five "No such file") |
| **OmniRoute transport** | ❌ no `httpx`/`NERVE_OMNIROUTE`/`aiohttp` in `backend/inference/` | grep → no matches |
| **Ask pipeline** | ❌ still `make_write_endpoint` 202-ACK stub; no `context` service registered | `backend/api/endpoints/context.py:14-21` |
| **`synthetic-lab/`** | ❌ `.gitkeep` only | `ls synthetic-lab/` |
| **CI** | ❌ none (no `.github/workflows`, `.gitlab-ci.yml`, `.circleci`) | `ls` |
| **`on_event`** | ❌ still at `backend/api/wiring.py:352/357` (deferral comment at :348) | grep |
| **README testing** | ❌ `README.md:67` still bare `pytest tests` (hits integration tests) | grep |
| **Status docs** | 🟡 `implementation-status.yaml` got an uncommitted data-lift note (T204) but `updated: "2026-09-29"` still | `git diff docs/implementation-status.yaml` |
| **Signing key hygiene** | ⚠️ `stabulous.p12` at repo root (gitignored, untracked); p12 blobs present in unreachable snapshot commits only (D5) | `git ls-tree -r a91b896`; `git ls-files` on main → clean |
| **Harbor deploy** | ✅ fix in tree (uncommitted): postgres StatefulSet `Available` wait removed (K3s v1.36.4 never populates `status.conditions`); pod-level Ready waits already cover it | `git diff deployments/harbor/deploy.sh` |
| **Spec packs** | ✅ 11 packs; `t001-stabilization` 3/48 rows done (T009 + 2); `gap-closure-t003` all 12 unchecked (T003-06 ~80% in worktree); `personal-reasoning-policy-closure` 0 rows done; `mobile-apps` M201–M206 0 rows done | grep `- [x]` / table rows |

---

## 3. Defects found this pass — root causes & fixes

### 3.1 Uncommitted-tree inventory (what Phase 0 must sort)

**Tracked, modified (24):**
- Data-lift app code: `NervePhoneApp.swift` (+123: bootstrap fetch, `--nerve-server` parse,
  launch-arg tab router, accessibility ids), `PhoneEnvironment.swift` (+75: `ServerConfig.parse`,
  `fetchInto`, `publishToAppGroup` → `group.nerv.center/cached-state.json` via `NerveJSON`),
  `PhoneFetchService.swift` (+`fetchAll()`, paths now relative — base URL already carries `/api`),
  `WireModels.swift` (`WireSystem.id` optional + derived `rowID`), `PhoneDataStore.swift`
  (`touchUpdatedAt`).
- Watch testability: `NerveWatchApp.swift` (+89: `NavigationPath` + `WatchDebug` launch-arg
  router + `writeAuditDump`), `WatchHomeView.swift` (+3 id).
- Widgets: `NerveCenterWidgetBundle.swift` (+16: watchOS accessory families).
- Distribution: `fastlane/Fastfile` (312-line rewrite: `build_ipa` / `build_watch`
  (self-contained watch IPA, embedded widget) / `publish` / `distribute`),
  `generate_manifest.py` (watch artifact → `NerveWatch.ipa` + embedded `.appex`; **bundle IDs
  disagree with xcodegen — D2**), xcodegen ymls (watch bundle IDs), generated
  `NerveWatch.xcodeproj/project.pbxproj` (hand-edited bundle IDs — D2), 3 plists (watch →
  `com.celesrenata.NervePhone.watch` + new `WKApplication` key), 2 schemes, new
  `NerveCenterWorkspace.xcworkspace/contents.xcworkspacedata`,
  `watch_export_options.template.plist`.
- Deploy/docs: `deployments/harbor/deploy.sh` (StatefulSet wait fix),
  `docs/DEPLOYMENT.md` (311-line rewrite), `docs/implementation-status.yaml` (T204 note),
  `docs/testflight-runbook.md` (+38).
- Root: `.gitignore` (+signing-credential ignores incl. `stabulous.p12`).

**Untracked (15):** `apple/e2e/` (harness + UI tests + audit script),
`apple/Distribution/patch_uitests.py`, `apple/Distribution/fastlane/.gitignore`,
`apple/e2e-artifacts/` (screenshot evidence), `docs/audit/` (baseline screenshots + data-dumps),
`docs/apple-data-lift-2026-10-18.md`, `gaps-apple1.md`, `gaps3.md`,
`apple/Distribution/fastlane/.env` (secret — correctly ignored), `apple/.swiftpm/`,
`apple/Distribution/DerivedData/`, 2× nested duplicate `…/NervePhone.xcodeproj/NervePhone.xcodeproj/`
+ `NerveWatch…/NerveWatch…/` dirs, 3× `xcuserdata/` dirs, `.roo/rules-code/30-k8s-deploy.md` +
`.roo/skills/`.

### 3.2 D1 — manifest test fails against the new artifact shape (S)

`test_missing_artifacts_marked_not_present` asserts entries
`("NervePhone.ipa", "NerveWatch.app", "NerveWatchWidgetExtension.watchkitarchive")` all
`present: false`. The uncommitted manifest rewrite (the correct shape — a watchOS app exports as
a self-contained IPA with its appex embedded, mirroring the phone) renamed the two watch entries
to `NerveWatch.ipa` and `NerveWatchWidgetExtension.appex` (the latter `embedded: true`).

**Fix:** update the test's expected tuple to the new names (keep the phone entry; keep the
`present is False` assertion for the absent-ipa case — for the embedded appex, also assert
`embedded is True` since it will never exist standalone). Run → `tests/apple/` 5/5, full suite
601/0.

### 3.3 D2 — watch bundle-ID family must be converged (S)

Five artifacts disagree (table in §1.2). Decision: **`com.celesrenata.NervePhone.watch`** for the
watch app, **`com.celesrenata.NervePhone.watch.widgets`** for its widget extension — this is what
the xcodegen source of truth already says, what `WKCompanionAppBundleIdentifier`
(`com.celesrenata.NervePhone`) implies, and what the `35d3e78` "align bundle IDs to
`com.celesrenata.NervePhone`" commit intended. Then:
1. `Generated/NerveWatch.xcodeproj/project.pbxproj`: `com.celesrenata.NerveWatch` →
   `com.celesrenata.NervePhone.watch` (2×), `com.celesrenata.NerveWatch.widget` →
   `com.celesrenata.NervePhone.watch.widgets` (2×) — or better, regenerate via
   `xcodegen generate` + `patch_uitests.py` and diff to confirm no other drift.
2. `generate_manifest.py`: same renames on the two watch entries' `bundle_id`.
3. Verify `baseline_audit.sh` (already `NervePhone.watch`), the watch scheme, and
   `watch_export_options.template.plist` agree; grep for `com.celesrenata.NerveWatch` across
   `apple/` → should return only the *product/target names* (fine), zero bundle IDs.
4. Re-run the manifest test (D1) + `xcodebuild -scheme NerveWatch build` for the watchOS sim to
   confirm the regenerated project still compiles.

**Converged 2026-10-26:** the pbxproj now matches the xcodegen source of truth — all four
`PRODUCT_BUNDLE_IDENTIFIER` values in `apple/Distribution/Generated/NerveWatch.xcodeproj/project.pbxproj`
carry `com.celesrenata.NervePhone.watch` (watch app, ×2) and `com.celesrenata.NervePhone.watch.widgets`
(widget extension, ×2). `rg 'com\.celesrenata\.NerveWatch'` across `apple/` returns zero bundle-ID hits
(product/target names like `NerveWatch` remain, as intended).

**Remaining external dependency (signing re-verification):** full closure of D2 still requires a
re-issued development provisioning profile registered to the `com.celesrenata.NervePhone.watch`
App ID. The previously committed `apple/Distribution/credentials/NerveWatch.mobileprovision` was
registered to `com.celesrenata.NerveWatch` (team `NQV7T36W69`), so it does not match the converged
bundle-ID family; that profile is gitignored and absent from the committed tree, so it must be
re-issued (App ID + profile) before signing re-verification can be run. Until then, the watch
build for `generic/platform=watchOS Simulator` fails at compile time for an unrelated reason
(see build note below), not at signing.

**Build note (2026-10-26):** `xcodebuild -project Distribution/Generated/NerveWatch.xcodeproj
-scheme NerveWatch -destination 'generic/platform=watchOS Simulator' build` → `** BUILD FAILED **`
with `cannot find 'WatchAttentionStore' in scope` / `cannot find type 'AttentionSeverityDot' in
scope` / `cannot find 'AttentionWristActions' in scope` — the generated project omits
`Watch/Store/WatchAttentionStore.swift` and `Watch/Support/WatchAttentionDesign.swift` from its
file lists (stale generated project: the committed `NerveWatch.xcodeproj` was generated before
those two source files existed, and the xcodegen `sources:` glob is not re-applied without a
regenerate). This is a generated-project staleness issue, distinct from D2's bundle-ID
convergence, and is out of scope for this change; a future `xcodegen generate` +
`patch_uitests.py` pass should pick the files up and be re-verified with the watch build.

### 3.4 D3 — Apple under-run trap, second occurrence (structural, Phase 1)

Bare `swift test` → 0 tests across all three bundles (stale `.build` products again); forced
rebuild → 46/0. `gaps3.md` Phase A's fix (canonical command + CI count assertion) has not landed
and the failure mode has degraded from "2 of 46" to "0 of 46". See Phase 1 below.

### 3.5 D5 — signing-key hygiene: rotate + purge unreachable refs (S + decision)

`git ls-files` on `main` is clean (no p12, no `.env`), and the new `.gitignore` entries are
correct. Residuals: (a) `stabulous.p12` still physically in the worktree — rotate the key
(export a new one from Keychain Access) and `rm` the file; (b) unreachable "Zoo parallel task
snapshot" commits (reachable only via `git log --all`) contain
`credentials/nervecenter.p12`, `nervecenter.p12.old`, `fastlane/.env.example`, `stabulous.p12`
blobs — if rotation makes the old key dead, the blobs are inert; otherwise purge the snapshot
refs (`git for-each-ref` → delete/expire, `git reflog expire --all`, `git gc --prune=now`) **only
if the repo is confirmed local-only or the remote copy is scrubbed the same way**.
`fastlane/.env` (untracked, ignored) may contain the same credentials — after rotation, verify
the live `.env` holds the new key.

**2026-10-26 update (answers §7.2):** the repo is **not local-only** — `origin` is
`github.com/celesrenata/nervecenter`. The p12 blobs therefore also live in the remote's object
store once the snapshot refs were ever pushed, or at minimum the rotation must be confirmed
complete before the local purge. **Decisive, not performed:** rotating the signing key and
`git filter-repo`-ing (or scrubbing) the remote history are destructive, outward-facing
operations — that is a user decision, not a closeout action. Until then, the only safe move is
key rotation (makes the old blobs inert) + local `rm` of the worktree p12 files; the history
purge waits on an explicit go/no-go.

### 3.6 D6 — watch `approvals.json` contract half-built (S, part of T003-08 scope)

`WatchSnapshotSource` defines three expected App-Group files; the phone publishes one.
`WatchApprovalsView`/`WatchApprovalRow` are fully built and waiting on data that never arrives.
Close by extending `publishToAppGroup` to also write `approvals.json` (the pending-approvals
rows the phone already fetches — `store.pendingApprovals` plus the planned-change payload the
row model requires) on the same cadence as `cached-state.json`. Verify on a paired sim/device
run that the watch Approvals screen renders them. (The `stop-agents.json` emergency marker is
watch→phone; confirm `WatchStopView` writes it — small check, include in the same task.)

### 3.7 D7 — stale generated watch project omits two source files (S, surfaced 2026-10-26)

Discovered while re-verifying D2: the **tracked** `Generated/NerveWatch.xcodeproj/project.pbxproj`
omits `apple/Watch/Store/WatchAttentionStore.swift` and `apple/Watch/Support/WatchAttentionDesign.swift`
from its file list even though types from both are referenced by files that *are* in the list —
so a watch simulator build fails at compile, independently of the D2 bundle-ID convergence.
(Confirmed: the nested **untracked** duplicate
`apple/Distribution/Generated/NerveWatch.xcodeproj/NerveWatch.xcodeproj/` — gitignored,
`.gitignore:64` — is a separate stale copy, not the one Xcode opens.) The xcodegen source of
truth (`apple/Distribution/xcodegen/NerveWatch.xcodegen.yml`) matches the converged bundle-ID
family; the fix is a `xcodegen generate` + `patch_uitests.py` pass against the tracked project
(`xcodegen` is not installed on this host — `brew install xcodegen` first), then rebuild the
watch target to confirm the two files compile in. Distinct from D2: D2 is a bundle-ID/signing
convergence (done in-tree, profile re-issue outstanding); D7 is generated-project staleness
(source list out of date) — both must be true before the watch target ships.

### 3.8 D4 — commit-scope entanglements (S, Phase 0 execution detail)

Verified **false** this pass: `gaps-apple1.md` G7.1 claimed "`NervePhoneApp.swift` depends on the
untracked `apple/e2e/XCUITestSupport/NerveE2E.swift`" — grep of `apple/iOS` + `apple/Watch` +
`apple/Shared` shows **zero** references to `NerveE2E`/`NerveUIHarness` from app code (the apps
parse launch args inline: `NervePhoneApp.swift:151-154`, `NerveWatchApp.swift:78-95`). Only the
UITest target depends on the harness, so app code and harness can commit separately **except**
for the generated `NervePhone.xcodeproj/project.pbxproj` (modified, carries the UITest-target
patch produced by the untracked `patch_uitests.py`) — commit the patcher with the project, or
the next regeneration loses the target.

Suggested slices (each independently green + committable):
1. `feat(distribution): watch ships as self-contained IPA; manifest/xcodegen/pbplists/schemes
   + XCUITest harness + patch_uitests.py` — Fastfile, generate_manifest.py (+ D1 test fix),
   xcodegen ymls, Generated/** (plists, pbxproj, schemes, workspace), `patch_uitests.py`,
   `apple/e2e/**`, `fastlane/.gitignore`, `.gitignore` signing entries, `docs/audit/`
   baseline (the regression baseline `gaps-apple1.md` G7.4 wants kept).
2. `feat(apple): data-loading lift — fetch-on-load, --nerve-server, App Group publish` —
   the six app-code files + `docs/apple-data-lift-2026-10-18.md`.
3. `fix(deploy): harbor — skip StatefulSet Available wait (K3s 1.36.4)` — `deploy.sh` +
   `docs/DEPLOYMENT.md`.
4. `docs: testflight runbook + implementation-status note + gap reviews (gaps-apple1, gaps3,
   gaps4)` — `docs/testflight-runbook.md`, `docs/implementation-status.yaml`, the gap docs.

Explicitly **not** committed: `apple/.swiftpm/`, `DerivedData/`, nested duplicate `.xcodeproj`
dirs, `xcuserdata/`, `apple/e2e-artifacts/` (transient XCUITest output — gitignore it;
`docs/audit/` is the durable evidence), `.roo/` (IDE state — decision in §7 Q3).

---

## 4. Corrections to prior docs (for the record)

| Prior claim | This pass |
|---|---|
| `gaps-apple1.md` G7.1: "`NervePhoneApp.swift` depends on untracked `NerveE2E.swift`, so the lift cannot be committed in isolation" | **Wrong** — app code has no `NerveE2E` reference; only the UITest target does. The real entanglement is generated-pbxproj ↔ `patch_uitests.py` (§3.8). |
| `gaps-apple1.md` §2: "swift test 2/2 pass" | That was the **under-run** (2 of 46); the package has 46 tests across 3 bundles (D3). |
| `gaps-apple1.md` G3 / `docs/apple-data-lift`: "publish verified; read blocked by env" | Publish is verified for `cached-state.json` **only** — `approvals.json` is never written by the phone at all (D6), so the watch Approvals screen is dead even on paired hardware until it lands. |
| `gaps3.md` §2: "repo otherwise clean except uncommitted runbook + .swiftpm/ + xcuserdata" | Stale — 24 modified + 15 untracked paths now (§3.1). |
| `gaps3.md` Phase A: "A1 fix not yet landed; A2 CI not yet landed" | Confirmed still true, and the trap has now caused a **0-of-46** false green (D3). |

---

## 5. Per-gap status ledger (delta vs `gaps3.md` §6)

Legend: 🟢 closed · 🟡 partial · 🔴 open (unchanged from `gaps3`).

### T0 — Integrity (new items this pass)

| ID | Gap | Status | Notes |
|---|---|---|---|
| **D1** | Manifest test vs new artifact names | 🟢 CLOSED (2026-10-26) | §3.2 — landed with the distribution commit; `pytest tests/apple/test_generate_manifest.py` → 5 passed (re-verified 2026-10-26) |
| **D2** | Watch bundle-ID family convergence | 🟡 CONVERGED, signing re-verify pending | §3.3 — pbxproj now matches the xcodegen source of truth: all 4 `PRODUCT_BUNDLE_IDENTIFIER` values are `com.celesrenata.NervePhone.watch` / `.watch.widgets`; `rg 'com\.celesrenata\.NerveWatch'` over `apple/` → zero bundle-ID hits. Converged 2026-10-26. **External dependency for full closure:** re-issued development provisioning profile registered to the `com.celesrenata.NervePhone.watch` App ID (the previously committed `NerveWatch.mobileprovision` was registered to `com.celesrenata.NerveWatch`, team `NQV7T36W69`; it is gitignored and absent from the committed tree). Watch sim build fails only from a separate, pre-existing generated-project staleness issue (missing `WatchAttentionStore.swift` / `WatchAttentionDesign.swift` in file lists) — see §3.7 D7 |
| **D7** | Stale generated watch project (source list) | 🟡 OPEN (NEW, 2026-10-26) | §3.7 — tracked `Generated/NerveWatch.xcodeproj/project.pbxproj` omits `WatchAttentionStore.swift` + `WatchAttentionDesign.swift`; watch sim build fails at compile. Fix: `xcodegen generate` + `patch_uitests.py` (xcodegen not installed on this host yet); distinct from D2 |
| **D3** | Apple under-run trap (stale SPM test products) | 🟢 CLOSED (2026-10-26) | §3.4 — structural fix landed: `apple/scripts/test-apple.sh` (touch → `swift build --build-tests` → `swift test` → per-bundle count assertion 13/74/23 = 110) is the canonical command and is run by CI (`.github/workflows/ci.yml`); verified 110 passed, 0 failures live 2026-10-26 (`PASS 110 tests (13/74/23)`). Bare `swift test` default-resolution root-cause fix remains outstanding (t001 T005 fallback path) |
| **D5** | Signing-key rotation + snapshot-ref purge | 🟡 PARTIAL (NEW) | §3.5 — gitignore done; `stabulous.p12` still physically in the worktree (re-verified 2026-10-26); rotate + purge + local-file removal pending. **2026-10-26:** remote confirmed = `github.com/celesrenata/nervecenter` (answers §7.2 Q2 — repo is NOT local-only); key rotation + `git filter-repo`/remote history purge is a user decision, not performed; local p12 files not deleted |
| **D4** | Commit-scope slices for the 24+15 path tree | 🟢 CLOSED (2026-10-26) | §3.8 — the 2026-10-18 uncommitted tree landed as logical commits (`87c69be`…`0c1b6ed`); working tree clean apart from new work; junk (root-level patch/`.roo`) explicitly excluded per the plan |

### T1 — Personal reasoning "brain" + persistence (spec pack `personal-reasoning-policy-closure`)

| ID | Gap | Status (this pass) |
|---|---|---|
| G-1.1 | Commitment/Request/Deadline detection (§16–18) | 🟢 CLOSED (2026-10-20) — R103 landed in `specs/personal-reasoning-policy-closure` (detection extractors in `backend/communications/detection/`) |
| G-1.2 | Unified Person + cross-channel conversations (§6/§7/§32) | 🟢 CLOSED (2026-10-20) — `Person`/`ContactIdentity`/`Communication`/`Conversation` in `backend/communications/models.py`; cross-channel normalization (R102) |
| G-1.3 | Follow-Up engine (§57) | 🟢 CLOSED (2026-10-20) — `backend/followups/` (R104) |
| G-1.4 | Drafts plane (§40/§63) | 🟢 CLOSED (2026-10-20) — `backend/drafts/` drafter + PDP gate (R105) |
| G-1.5 | Document flat-API mapping (§63) | 🟢 CLOSED (2026-10-20) — eight §52 sections mapped in R109; §63 mapping documented |
| G-1.6 | §64 personal MCP tools (18 → 29) | 🟢 CLOSED (2026-10-20) — 29 tools (18 spec-45 + 11 R112), contract-tested (`tests/mcp/`) |
| G-1.7 | §65 personal CLI commands | 🟢 CLOSED (2026-10-20) — attention/brief/commitments/communication/people/today commands wired (R113) |
| G-1.8 | Privacy/attention policy (§35–39, §58–59) | 🟢 CLOSED (2026-10-20) — PDP (R107) wired into ask + drafts; attention policy (R108) |
| G-1.9 | §50 search / §51 memory / §52 daily state | 🟢 CLOSED (2026-10-20) — cross-channel search + durable memory (R110); daily operational state (R109). Note: `synthetic-lab/ask_fixture.json` unpopulated → 2 seeded ask cases stay pending (no results invented) |
| G-1.10 | Personal-plane persistence (all 8 services in-memory) | 🟡 DEFERRED BY DESIGN — spec line 6 (2026-10-20 closeout): durable personal persistence intentionally out of scope for the G-1 feature |

### T2 — Apple personal surfaces (spec packs `mobile-apps` M201–M206 + `gap-closure-t003`)

| ID | Gap | Status (this pass) |
|---|---|---|
| G-2.1 | iPhone personal views (§66) | 🟢 CLOSED (2026-10-18) — D2' iPhone personal surfaces (Today/Inbox/People/Waiting/Briefing + reorg), commit `ba27754`; `apple/iOS/App/Views` holds 13 personal views |
| G-2.2 | Watch personal (§27/§60/§67) | 🟢 CLOSED (2026-10-18) — D3' watch attention-first home + `NEED YOU` complication + wrist actions, commit `7b5eded`; **D6** (approvals.json) landed with the data lift (`docs/apple-data-lift-2026-10-18.md`) |
| G-2.3 | §68/§69 personal widgets | 🟢 CLOSED (2026-10-18) — D4' personal widget content (§68/§69) in the App Group, commit `8d935eb`; watchOS accessory family support landed |
| G-2.4 | Relay beyond iMessage/SMS (email/voicemail/calendar/contacts) | 🟢 CLOSED (2026-10-18) — D5' capture planes (email/voicemail/calendar/contacts) + local-first retention, commit `c6d2def`; backend mail-connector plane is the tested foundation |
| G-2.5 | Device/simulator verify path | 🟡 **PARTIAL (was OPEN)** — T003-06 harness + launch-arg routers + baseline audit + audit evidence built and working (landed with the Phase 0 commits `87c69be`/`0c1b6ed`); the D6 `approvals.json` contract landed with the data lift (phone writes `approvals.json` on the `cached-state.json` cadence — `apple/iOS/App/Environment/PhoneEnvironment.swift:192-202`); still outstanding: paired phone+watch **watch-display** verification (hardware pairing) and the `contentdelivery.apple.com` egress stall that blocks TestFlight signing (runbook); watch sim build additionally blocked by D7 |

### T3 — prompt1 core depth (spec pack `gap-closure-t003`)

| ID | Gap | Status (this pass) |
|---|---|---|
| G-3.1 | Real OmniRoute transport | 🔴 OPEN — no transport anywhere (T003-03) |
| G-3.2 | Ask pipeline (202-ACK → grounded answer) | 🔴 OPEN — stub confirmed unchanged (T003-04) |
| G-3.3 | `synthetic-lab/` eval corpus | 🔴 OPEN — `.gitkeep` (T003-05) |
| G-3.4 | gRPC/transport decision ADR | 🔴 OPEN (T003-01) |
| G-3.5 | e2e masks | ⚠️ per `gaps3` §7 reclassification: **annotate, don't delete** (Class A induced-error + Class B real 501 `/api/decisions`); annotation work not started (T003-02) |

### T4 — Hygiene

| ID | Gap | Status (this pass) |
|---|---|---|
| G-4.1 | Canonical all-bundle `swift test` command | 🟢 CLOSED (2026-10-26) — `apple/scripts/test-apple.sh` is the canonical command (touch → build → test → per-bundle count assertion 13/74/23 = 110) and is run by CI; live run 2026-10-26 → 110 passed, 0 failures (D3) |
| G-4.2 | Lifespan migration (`on_event`) | 🟢 CLOSED (2026-10-26) — `backend/api/wiring.py:345-366` uses `@asynccontextmanager _wiring_lifespan` attached via `target.router.lifespan_context`; `rg 'on_event' backend/api/*.py` → only the docstring hit; zero registrations remain |
| G-4.3 | Status doc refresh | 🟢 CLOSED (2026-10-26) — `docs/implementation-status.yaml` `updated: 2026-10-26` with the recent_activity block covering P0–P4 planes, R107 PDP wiring, web goals delete, Apple theme picker, D2 convergence, 1003 baseline + closeout items (T208 fix, T207 DNS change, D7, D5 remote finding) |
| G-4.4 | JWT test keys | 🟢 CLOSED (`85e681c`) |
| G-4.5 | README Testing section | 🟢 CLOSED (2026-10-26) — `README.md:69-93` §Testing documents all three canonical commands with counts (backend `pytest` 1003 / web e2e ≥60 / Apple `test-apple.sh` 110 = 13/74/23) |
| G-4.6 | CI | 🟢 CLOSED (2026-10-26) — `.github/workflows/ci.yml` has `python` / `web` / `apple` jobs with count floors (1003 / ≥60 / 110, 0 failures); the `apple` job runs `bash apple/scripts/test-apple.sh` (D3 guard) |

---

## 6. Phased closure plan

Ordered by dependency + leverage. Phases 0–1 are new this pass; Phases 2–5 carry
`gaps3.md` §8 forward with the re-scoping noted inline.

### Phase 0 — Land the uncommitted tree coherently (≤ 1 day; do first)

1. **D1:** update `test_missing_artifacts_marked_not_present` to the new artifact names
   (`NerveWatch.ipa`, `NerveWatchWidgetExtension.appex` + `embedded: true` assertion).
   Gate: `pytest tests/apple/ -q` → 5/5; full suite → **601/0**.
2. **D2:** converge the watch bundle-ID family on `com.celesrenata.NervePhone.watch[.widgets]`
   per §3.3 (regenerate pbxproj via `xcodegen` + `patch_uitests.py` if any residual drift).
   Gate: `rg 'com\.celesrenata\.NerveWatch' apple/Distribution/` → zero *bundle-ID* hits
   (target/product names exempt); watch sim build green.
3. **Junk sweep (§3.8):** delete `DerivedData/`, nested duplicate `.xcodeproj` dirs;
   `.gitignore`: `apple/.swiftpm/`, `*/DerivedData/`, `xcuserdata/`,
   `apple/e2e-artifacts/`; decide `docs/audit/` = committed baseline (recommend: yes) and
   commit `docs/audit/2026-10-18-baseline/` with slice 1.
4. **D5 (first half):** rotate the signing key, `rm stabulous.p12`, refresh
   `apple/Distribution/fastlane/.env` with the new key; confirm
   `git status --short` shows no secret paths. (Ref purge is second half — §7 Q2.)
5. **Commit in the 4 slices of §3.8** (distribution+harness → data-lift → deploy → docs),
   each green on its own commit.
6. **Gate:** `git status --short` clean (modulo deliberate ignores); on the final commit:
   `pytest tests/ -m "not integration"` → 601/0; `npm run build && npx playwright test` →
   60/60; Apple via the Phase 1 command → 46/46; fresh detached-worktree `import
   backend.api.app` clean (the `gaps2` H-1 gate, kept permanent).

### Phase 1 — Test-truth infrastructure (1–2 days; unblocks trust in every later gate)

Carried from `gaps3` Phase A; now with a second live failure to point at.

- **A1.** Canonical Apple test command: `apple/scripts/test-apple.sh` that (a) `touch`es
  `apple/Tests/**/*.swift`, (b) `swift build --build-tests`, (c) runs `swift test`, (d)
  **asserts the per-bundle counts** (NerveSharedTests 13 / NerveRelayTests 31 / NerveAppsTests
  2; fail non-zero if the total ≠ 46 — the under-run can never be green again). `build_and_test.sh`
  must call it.
- **A2.** CI: `.github/workflows/ci.yml` — `python` job (venv/uv,
  `pytest tests/ -m "not integration"`, expect ≥601), `web` job (`npm ci && npm run build &&
  npx playwright test`, expect ≥60), `apple` job (A1 command, expect 46). macOS runner for
  Apple; the A1 assertion doubles as the cold-cache guard.
- **A3.** Amend the stale count claims: `gaps2.md:62`, `specs/phase0-integrity/tasks.md:147/182`
  ("NerveSharedTests 31" → Relay 31 / Shared 13 / Apps 2, 46 total).
- **A4.** (Folded into Phase 0 step 5 here: `docs/testflight-runbook.md` lands as slice 4.)
- **Gate:** a deliberately red Apple test fails CI; fresh-checkout `swift test` reports 46.

### Phase 2 — T1 reasoning brain + persistence (≈1–2 weeks; `gaps3` Phase B, unchanged)

R101(+G-1.10 persistence) → R107 → R102 → R103 → R104/R105 → R109/R110 → R112 → R113 → R111,
per `gaps3.md` §8 B1–B10, with the same gates (`test_communications.py`,
`test_detection.py`, `test_followups.py`, `test_drafts.py`, `test_personal_policy.py`, 29-tool
MCP suite, §61/§62 scenarios). **New dependency note:** nothing in Phase 0/1 changes this
phase's shape; the mail-connector plane (committed + tested) remains the relay-email
foundation for Phase 4 D6.

### Phase 3 — T3 core depth (≈1–2 weeks, parallel track; `gaps3` Phase C, unchanged)

T003-01 (transport ADR) → T003-03 (OmniRoute transport + fixture mode) → T003-04 (ask
pipeline with provenance; 202 only on genuine unavailability) → T003-05 (synthetic-lab
corpus + CI gate, ask-QA first, detection cases when Phase 2 lands). T003-02 is now
**annotate-not-delete** per `gaps3` §7 (mark each masked entry with the test/endpoint that
justifies it; correct the stale "known-missing /api/systems" prose only).

### Phase 4 — T2 Apple personal surfaces (≈1–2 weeks; re-scoped from `gaps3` Phase D)

**D1' (was D5/T003-06 — now mostly done):** Phase 0 lands the harness; remaining:
- paired phone+watch verification (XCUITest pairing or hardware) so the watch
  `cached-state.json` read is evidenced, not just code-correct;
- **D6:** phone publishes `approvals.json` (+ verify `stop-agents.json` write);
- wire the audit harness to the new personal tabs once they exist (launch-arg router already
  generalizes to `--nerve-tab=Today|Inbox|…`).
**D2' (T003-07 / G-2.1):** iPhone Today/Inbox/People/Waiting/Briefing + the
`Today | Engineering | Ask` reorg (needs Phase 2's personal API for depth; Today/Waiting/
Briefing can start on today's attention/briefing/calendar endpoints).
**D3' (T003-08 / G-2.2):** watch attention-first home (§27), `n NEED YOU` complication (§67),
wrist actions + escalation (§60) — rides on the Phase 0 App Group plumbing.
**D4' (T003-09 / G-2.3):** §68/§69 personal widget content (watchOS accessory families
already supported in the uncommitted widget code).
**D5' (T003-10/11 / G-2.4):** relay email capture over the mail-connector plane → voicemail →
calendar/contacts (EventKit/Contacts are S–M each; §70 local-first retention).
Gate per view: rendered against the real backend in the simulator verify path; honest
loading/empty/error states; no fabricated rows.

### Phase 5 — Hygiene closeout (interleaved; `gaps3` Phase E, unchanged)

E1 `on_event` → lifespan (`wiring.py:352/357`, remove the :348 deferral comment) · E2 status-doc
refresh (`updated:` → today; record the personal surface plane, the data-lift, the Apple count
correction, and the bundle-ID decision from D2) · E3 README Testing section (the Phase 1
canonical commands) · E4 (done: G-4.4 committed at `85e681c`) · E5 (new this pass): second the
commit-hygiene norm from `gaps2` §7 Q6 into the spec packs' execution rules — *"no commit that
touches a generated project + its patcher ships without the patcher; no backend commit touching
an import chain ships without a fresh-worktree import check"*.

**Sequencing:**

```
Phase 0 (≤1 day)  ─► lands 24+15 paths, D1/D2/D4/D5½
Phase 1 (1–2 days, can start during Phase 0 step 6)  ─► CI + test truth
Phase 2 (T1 brain) ──► single backend owner; starts as soon as Phase 0 gates
Phase 3 (T3 brain, parallel) ──► C3 depends on C2
Phase 4 (Apple personal) ──► depends on Phase 2 API + Phase 0 harness
Phase 5 (hygiene) ──► interleaved; E2 last
```

---

## 7. Open questions / decisions needed

1. **D2 — canonical watch bundle-ID family:** recommend `com.celesrenata.NervePhone.watch.*`
   (matches xcodegen truth + companion semantics). Confirm or counter-propose; one word
   unblocks Phase 0 step 2.
2. **D5 — repo visibility:** ~~is `nervcenter` local-only or pushed to a remote?~~
   **Answered 2026-10-26:** the remote is `github.com/celesrenata/nervecenter`. The snapshot-ref
   purge (§3.5) must therefore cover the remote — `git filter-repo` + force-push, or key
   rotation confirmed complete first. This is a user decision; not performed.
3. **`.roo/` state:** commit `.roo/rules-code/30-k8s-deploy.md` (reusable deploy rule) or
   gitignore `.roo/` as IDE state? (The `20-web-design.md` sibling is already tracked, which
   suggests commit — but verify intent.)
4. **OmniRoute availability (carried from `gaps3` §9.2):** is a reachable
   `NERVE_OMNIROUTE_URL` available from this host, or does Phase 3 C2/C3 proceed in
   recorded-fixture mode only?
5. **Personal-plane persistence model (carried from `gaps3` §9.1):** first-class rows (new
   tables + migration) vs event-sourced fabric rows — decide before Phase 2 B1 builds on it.
6. **Paired watch verification method:** XCUITest iPhone→Watch pairing (portable, simulator)
   vs hardware (blocked in practice by the `contentdelivery.apple.com` egress stall per the
   runbook)? Recommend the pairing path; hardware stays best-effort.

---

## 8. Explicit non-gaps (re-verified this pass, no action)

- MCP §45 — all 18 tools present and wired.
- CLI §47 core — 12 modules functional.
- iPhone §56 engineering tabs — all six exist **and now render live data** (data-lift verified).
- Attention actions (§55) — resolve/suppress/acknowledge/snooze endpoints exist.
- SSO — Keycloak PKCE live-verified (2026-10-12); test fixtures RFC-7518-clean.
- Relay core engine — cursor/backlog/replay/HTTP-4xx tested (NerveRelayTests, 31 pass).
- Mail-connector plane — committed + tested (85e681c/6bdda63 era; `tests/backend/test_mail_connector.py`).
- Event bus / DLQ / PDP / persistence / calendar conflict engine — tested; calendar tests clock-invariant.
- Web app — 20 views, build clean, e2e 60/60 green (masks are load-bearing per `gaps3` §7).
- Apple shared-package tests — 46/46 green (after the mandatory rebuild; the *trap* is the
  gap, not the code).
- Phone→watch `cached-state.json` publish — verified with live values (14 agents / healthy).

---

## 9. Appendix — command evidence (this session, 2026-10-18 UTC)

```text
15:08  .venv/bin/python -m pytest tests/ -m "not integration" -q
       → 1 failed (test_generate_manifest.py::test_missing_artifacts_marked_not_present,
         KeyError 'NerveWatch.app'), 600 passed, 710 warnings in 4.54s
15:09  cd web && npm run build                    → tsc + vite clean, 66 modules
15:10  cd apple && swift build && swift test      → build OK; bare swift test: 0 tests × 3 bundles
15:10  touch $(find apple/Tests -name '*.swift') && swift build --build-tests && swift test
       → NerveSharedTests 13 / NerveRelayTests 31 / NerveAppsTests 2 — 46 passed, 0 failed
15:11  cd web && npx playwright test --reporter=line  → 60 passed (28.9s)
15:14  grep httpx|NERVE_OMNIROUTE|aiohttp backend/inference/  → no matches
15:14  ls backend/{communications,followups,drafts,persons,people}  → 5× No such file
15:14  python -c "from mcp.tools_module import _SPEC; print(len(_SPEC))"  → 18
15:14  ls .github/workflows .gitlab-ci.yml .circleci  → No such file
15:15  git status --short  → 24 modified + 15 untracked paths (§3.1)
15:17  git ls-tree -r a91b896 | grep -i 'p12|\.env'  → nervecenter.p12, .p12.old,
         fastlane/.env.example, stabulous.p12 (unreachable-from-main snapshot only)
15:17  grep com.celesrenata.{NerveWatch,NervePhone.watch} across Distribution/
       → 5-artifact disagreement (D2)
15:27  grep 'approvals.json' apple/iOS --include='*.swift'  → 0 hits (D6)
15:27  grep 'NerveE2E|NerveUIHarness' apple/{iOS,Watch,Shared}  → 0 hits (corrects gaps-apple1 G7.1)
```

*Prepared by re-running every suite this session (600 py / 60 web / 46 apple-forced) and
re-verifying each `gaps3` "still missing" claim against the live `35d3e78` tree + uncommitted
diff. No tracked repo files were modified during this review; only `.build/` artifacts were
touched by the forced Apple test rebuild (untracked).*
