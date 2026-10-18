# NerveCenter — Gap Review & Closure Plan, Second Pass (`gaps2.md`)

**Date:** 2026-10-14 (evening UTC)
**Supersedes / extends:** `gaps1.md` (same day, morning) — where gaps1's claims have been
re-verified or turned out stale, this file says so explicitly.
**Scope:** Re-review of `prompt1.md` (v0.2 core spec) + `prompt2.md` (Personal Ops addendum)
against the live tree at `a3c07da` + current worktree, with fresh test runs on every suite.
**Method:** full read of both prompts' section maps; live runs of `pytest tests/` (non-integration),
`npx playwright test` (56 specs), `tsc && vite build`, `swift build && swift test`; import-check of
**HEAD in a detached worktree**; `git log`/`git diff` archaeology on the uncommitted tree; grep
verification of every "still missing" claim in gaps1.

---

## 1. Executive summary

The state of the repo changed materially between the morning's `gaps1.md` and this review:

1. **HEAD (`a3c07da`) does not import.** The mail-connector plane was committed *inside* the
   "specs: add three spec packs" commit — together with the spec packs — and the committed
   `backend/connectors/mail/__init__.py` still lacks the `MailConnector` re-export that
   `backend/connectors/__init__.py` imports. Verified in a detached worktree:
   `import backend.api.app` → `ImportError: cannot import name 'MailConnector'`.
   **Any clean checkout of HEAD today: the API server cannot boot and every app-based test
   file fails at collection.** The worktree contains a 3-line uncommitted fix that repairs it;
   the tree only passes tests because that fix is present *and uncommitted*. This is the
   highest-severity item in the repo right now.
2. **The Python suite is red again: 557 passed, 2 failed.** The two failures are new
   clock-dependence time-bombs in `tests/backend/test_calendar.py` — the same class as gaps1's
   Crash B, but in two tests gaps1's fix missed. They passed this morning and began failing
   at ~10:00 UTC today (the moment the hardcoded reference day `2026-10-14` left the default
   list window). Root cause and exact fix in §3.2.
3. **A web theme rename is half-finished in the worktree:** `Layout.tsx` (tracked, modified)
   now imports `ThemeToggle`, but `ThemeToggle.tsx` is **untracked** while the old
   `ThemeSwitcher.tsx` is tracked and fully unreferenced (dead code). Committing `Layout.tsx`
   without `ThemeToggle.tsx` breaks `tsc` on the commit.
4. Everything else in gaps1's gap inventory (T1 reasoning layer, T2 Apple personal surfaces,
   T3 core brain) was re-checked and is **still unbuilt, exactly as recorded** — see §4 for the
   current status of every G-item. The three spec packs added at `a3c07da`
   (`specs/t001-stabilization`, `specs/personal-reasoning-policy-closure`,
   `specs/gap-closure-t003`) are the executable work breakdown for most of it; this plan
   sequences those plus the new integrity items they missed.
5. One gaps1 claim is **wrong and corrected here**: gaps1/T007 said the status docs reference
   `backend/events/` and `backend/knowledge/` "that don't match the real tree" — both paths
   **exist** and are real (tested: `tests/backend/events/test_dlq.py`, registry/PDP suites).
   The docs are stale on *content* (dated 2026-09-29, pre-personal-planes), not on those paths.

**Bottom line:** close the four integrity items in Phase 0 (≤ half day, unblocks everything),
then execute the three spec packs in the order §5 gives.

---

## 2. Verified current state (evidence, 2026-10-14 ~21:00 UTC)

| Area | State | Evidence (commands, this session) |
|---|---|---|
| **HEAD `a3c07da`** | 🔴 `import backend.api.app` → `ImportError: MailConnector` | detached worktree `git worktree add /tmp/nc-head-verify HEAD` + repo venv python |
| Python suite (worktree, with uncommitted fixes) | 🟡 **557 passed, 2 failed** (`test_calendar.py`) | `.venv/bin/python -m pytest tests/ -m "not integration" -q` → `2 failed, 557 passed, 678 warnings in 4.40s` |
| Web build | ✅ `tsc && vite build` clean (64 modules) | `cd web && npm run build` |
| Web e2e | ✅ **56 passed** (27.2s, chromium) | `npx playwright test --reporter=line` (boots mock backend per `global-setup.mjs`) |
| Apple build | ✅ `swift build` clean | `cd apple && swift build` (1.91s) |
| Apple tests (default) | ✅ Full `swift test` (or the canonical `apple/scripts/test-apple.sh`) runs all three bundles — **NerveSharedTests 13, NerveRelayTests 31, NerveAppsTests 2 = 46 total** (2026-10-18, gaps4.md D3; per-bundle `--filter` names in the plan are stale, the under-run was a stale `.build` product, now guarded by touch + `--build-tests` + count assertion, CI `ci.yml`) | `swift test` output: `Executed 46 tests, with 0 failures` |
| MCP | ✅ 18 tools (§45); 0 of the 11 §64 personal tools | `from mcp.tools_module import _SPEC; len(_SPEC) == 18` |
| CLI | ✅ 14 commands; 0 personal (§65) commands | `cli/commands/` listing |
| Backend "brain" (G-1.1–G-1.4) | ❌ `backend/communications/`, `followups/`, `drafts/` **do not exist** | `ls backend/` |
| OmniRoute transport (G-3.1) | ❌ no `httpx`/`NERVE_OMNIROUTE_URL` anywhere in `backend/inference/` | `grep -rn 'httpx\|NERVE_OMNIROUTE' backend/inference/` → no matches |
| `synthetic-lab/` (G-3.3) | ❌ empty (`.gitkeep`) | `ls synthetic-lab/` |
| Stale e2e 404 masks (G-3.5) | ❌ still present in **3** files: `ask.spec.ts:19`, `views.spec.ts:30`, `views-a.spec.ts:41` (mask includes `/api/tasks` too) | grep |
| FastAPI `on_event` (G-4.2) | ❌ `backend/api/wiring.py:352/357` still `on_event` (code comment at :348 even documents the deferral) | grep |
| Status docs (G-4.3) | ❌ `docs/implementation-status.yaml` `updated: "2026-09-29"` — predates all personal planes, mail plane, SSO activation | head of file |
| README test commands (G-4.5) | ❌ no canonical Testing section; README shows plain `pytest tests` (would hit integration-marked tests) | grep |
| `web/dist` tracked (G-0.3) | ❌ `git ls-files web/dist` → `web/dist/index.html` (tracked *and* modified); `.gitignore` has `web/dist/` but the file predates the rule; **`.patches/` still untracked and un-ignored** | `git ls-files`, `cat .gitignore` |
| Uncommitted worktree | 🟡 7 modified + 3 untracked paths (see §3.3) — much smaller than gaps1's "68 staged", because the mail plane + web theme landed in `a3c07da` | `git status --short`, `git diff --stat` |
| Spec packs | ✅ 3 new packs exist: `t001-stabilization` (T002–T010, AC001–AC008), `personal-reasoning-policy-closure` (R101–R113, AC-R01–R18), `gap-closure-t003` (T003-01…12, AC1–AC12) | `specs/` |

---

## 3. Failures found this pass — root causes & exact fixes

### 3.1 H-1 (T0, repo-breaking): HEAD cannot import — the mail plane shipped broken

- `a3c07da` committed the whole mail-connector plane (`backend/connectors/mail/*`,
  `endpoints/mail_connectors.py`, 204-line endpoint, router mount, models) **mixed into the
  spec-packs commit** — so the plane has no logical commit of its own (G-0.1's "2–3 logical
  commits" is violated; T003 in `specs/t001-stabilization` expected a clean tree to build on).
- Committed `backend/connectors/mail/__init__.py` exports the imap/oauth/service names but not
  `MailConnector` (verified: `MailConnector in HEAD __all__: False`), while committed
  `backend/connectors/__init__.py` does `from backend.connectors.mail import MailConnector, …`
  and `backend/api/app.py:33` imports the package.
- Consequence at HEAD: `ImportError` on `import backend.api.app` → uvicorn boot fails,
  collection fails for all 18 app-based test files. (This is what gaps1 called "Crash A";
  the difference is that gaps1 found it in the *staged* file and fixed it in the worktree.
  It was then committed in the broken state — the fix stayed uncommitted.)
- **Fix (in worktree, must be committed with tests):**
  `backend/connectors/mail/__init__.py` — add
  `from backend.api.models import MailConnector` and include it in `__all__` (already present
  in the worktree diff, 3 lines).
  Longer-term, per gaps1 §3.1 note, move `MailConnector`/`MailAuthMethod`/`MailConnectorStatus`
  out of `backend/api/models.py` into `backend/connectors/mail/models.py` (optional, low urgency).
- **Gate:** commit the fix together with `tests/backend/test_mail_connector.py` (T002) — never
  commit the import fix alone without the plane's tests, or the broken-plane lesson repeats.

### 3.2 H-2 (T0): two calendar tests are clock time-bombs — the class recurred

`tests/backend/test_calendar.py` hardcodes `_day() = datetime(2026, 10, 14)` and ingests
events on that day. `CalendarService.list_events()` defaults its window to `[now, now+7d)`
(`backend/calendar/service.py:91-105`). gaps1 fixed only `test_service_reset_hook` (now green,
uses an explicit window) but **two API tests still rely on the default window**:

- `test_ingest_new_is_201_replay_is_200:95` — ingests `e1` at `at(9)–at(10)` then asserts
  `len(GET /api/calendar)` == 1. Green while `now < 2026-10-14T10:00Z`; fails after.
  **Failing now** (event falls before `now`, excluded by the default window).
- `test_list_filters_to_window:281` — ingests `e1` at `at(9)–at(10)` and `e2` at
  `at(25)–at(26)` (i.e. next day 01:00–02:00), asserts the default list returns both.
  `e1` is now in the past → only `{e2}` returned. **Failing now** (assertion
  `Extra items in the right set: 'e1'`).

Both are deterministic failures from ~10:00 UTC today onward — they will not self-heal.

**Exact fix** (clock-invariant, same technique as the reset-hook fix; the endpoint already
accepts `start`/`end` query params — `endpoints/calendar.py:4`, service signature
`list_events(start, end)`):

```python
# test_ingest_new_is_201_replay_is_200 — replace the bare GET:
data = client.get(
    "/api/calendar",
    params={"start": at(0).isoformat(), "end": at(24).isoformat()},
).json()["data"]
assert len(data) == 1

# test_list_filters_to_window — first assertion only (the narrow-window assertion
# below it already passes explicit params):
data = client.get(
    "/api/calendar",
    params={"start": at(0).isoformat(), "end": at(30).isoformat()},   # covers e1 (day 1 09:00) and e2 (day 2 01:00)
).json()["data"]
assert {e["event_id"] for e in data} == {"e1", "e2"}
```

Add a one-line comment on each, mirroring the reset-hook NOTE, explaining *why* the explicit
window is required (default window is anchored at the real clock).

**Generalized rule to adopt:** any test that ingests a fixed-date datum and then reads a
list endpoint *without* an explicit window is a time-bomb. Grep-able check for CI:
`rg -n '_day\(\)|datetime\(2026' tests/` should only appear inside fixtures that pass
explicit windows. (Other suites were checked: `test_briefing.py` uses `PINNED_NOW` and the
service honors it; `test_evidence/registry/dlq/pdp` use fixed `NOW` values with injected
clocks or explicit windows — not affected. Calendar is the only plane with a
`now`-anchored *default* window on a list path.)

### 3.3 H-3 (T0): half-applied web theme rename — untracked file the tracked tree depends on

Worktree diff of `web/src/components/Layout.tsx`: `ThemeSwitcher` → `ThemeToggle` (3 sites).
But:
- `web/src/components/ThemeToggle.tsx` is **untracked** (the only file that references
  `useTheme` alongside the tracked `ThemeContext.tsx`), while
- `web/src/components/ThemeSwitcher.tsx` is **tracked and now referenced by nothing**
  (grep for `ThemeSwitcher` across `web/src` + `web/tests` → only its own definition).

If `Layout.tsx` is committed alone, `tsc` fails on any fresh checkout (the import target
doesn't exist). If nothing is committed, the dead `ThemeSwitcher` stays.

**Fix:** commit `ThemeToggle.tsx` together with the `Layout.tsx` rename, and **delete**
`web/src/components/ThemeSwitcher.tsx` (or, if the rename is unwanted, revert `Layout.tsx`
and delete `ThemeToggle.tsx` — decision in §7 Q1; the toggle is the better component, so the
plan assumes completing the rename). `npm run build` + `npm run e2e` must be re-run after the
delete (build was verified clean with both files present, 20:45 UTC).

### 3.4 H-4 (T0): uncommitted test-hygiene work (T009) done but unlanded

`tests/backend/test_auth_sso.py` (worktree) now uses 64+ byte HS256 fixtures (T009/G-4.4
complete per `specs/t001-stabilization/tasks.md`, verified: 9 passed, no
`InsecureKeyLengthWarning`). It is uncommitted. Commit it with the H-2 test fixes as one
"test hygiene" commit.

### 3.5 H-5 (T0): tracked build artifact + untracked worker output

- `web/dist/index.html` is tracked **and** modified; `.gitignore` already has `web/dist/`
  but the file predates the rule → `git rm -r --cached web/dist` (T004/G-0.3).
  Verified safe: `web/Dockerfile` runs `npm run build` **in-image** (line 33) and copies
  `--from=build /app/dist/` (line 49), so the shipped image never needs the tracked dist.
- `.patches/` (3 worker `.patch` files) is untracked and not ignored → add to `.gitignore`.

### 3.6 H-6 (T4, carried): default `swift test` still exercises 2 of ~66 tests

Re-verified this pass (G-4.1/T005 open): plain `swift test` → `Executed 2 tests`
(`NerveAppsTests` only). `apple/build_and_test.sh` exists but just wraps `swift build &&
swift test`, so it inherits the same gap. The T005 fallback script
(`apple/scripts/test-apple.sh` with per-bundle `--filter`) is still the required path until a
SwiftPM root-cause fix lands.

### 3.7 Corrections to gaps1.md claims (for the record)

| gaps1 claim | This pass |
|---|---|
| "68 staged files" uncommitted (G-0.1) | Stale since `a3c07da` (12:19 PDT): the mail plane + web theme are **committed** (inside the spec-packs commit, see H-1). Current uncommitted surface is 7 modified + 3 untracked (§2). |
| "one-line fix, applied" (Crash A §3.1) | Applied **only in the worktree**; the committed plane is still broken (H-1). |
| Status docs reference `backend/events/`, `backend/knowledge/` "that don't exist" (G-4.3) | **Wrong** — both directories exist and are tested. The docs are stale on *content* (2026-09-29), not on those paths. T007's `rg` gate will pass trivially; the real work is content refresh. |
| e2e mask in `ask.spec.ts` + `views.spec.ts` (G-3.5) | Confirmed, plus a **third** masked file `views-a.spec.ts:41` (mask also contains `/api/tasks`). |

---

## 4. Remaining spec gaps — status re-verified this pass

Status: **OPEN** (nothing landed since gaps1), **PARTIAL**, or **CLOSED**.

### T0 — Integrity (all four were "planned in T001 spec"; now with hard evidence)

| ID | Gap | Status | Notes |
|---|---|---|---|
| G-0.1 | Land/finish uncommitted work | **PARTIAL** | Mail plane landed (broken — H-1); web theme half-applied (H-3); test fixes uncommitted (H-2/H-4) |
| G-0.2 | Mail-connector tests (T002) | **OPEN** | `tests/backend/test_mail_connector.py` does not exist; grep `MailConnector` in `tests/` → only `test_email.py` adjacency |
| G-0.3 | Untrack `web/dist`, ignore `.patches/` (T004) | **OPEN** | Verified §3.5 |
| G-0.4* | *New:* land the HEAD import fix with the plane (H-1) | **OPEN** | Not in gaps1 (the breakage wasn't committed yet when gaps1 was written) |

### T1 — prompt2 P3+ "reasoning layer" (spec pack: `personal-reasoning-policy-closure`)

| ID | Gap | Status |
|---|---|---|
| G-1.1 | Commitment/Request/Deadline detection (§16–18) | **OPEN** — R103 |
| G-1.2 | Unified Person + cross-channel conversations (§6/§7/§32) | **OPEN** — R101/R102 |
| G-1.3 | Follow-Up engine (§57) | **OPEN** — R104 |
| G-1.4 | Drafts plane (§40/§63) | **OPEN** — R105 |
| G-1.5 | Document flat-API mapping (§63) | **OPEN** — R109 |
| G-1.6 | §64 personal MCP tools (18 → 29) | **OPEN** — R112 (verified: 18 tools now) |
| G-1.7 | §65 personal CLI commands | **OPEN** — R113 (verified: 14 commands, none personal) |
| G-1.8 | Privacy/attention policy (§35–39, §58–59) | **OPEN** — R107/R108 |
| G-1.9 | §50 search / §51 memory / §52 daily state | **OPEN** — R109/R110 |

### T2 — Apple personal surfaces (spec pack: `gap-closure-t003`)

| ID | Gap | Status |
|---|---|---|
| G-2.1 | iPhone personal views (§66) | **OPEN** — T003-07 (verified: `apple/iOS/App/Views/` still only the 6 engineering tabs) |
| G-2.2 | Watch personal (§27/§60/§67) | **OPEN** — T003-08 |
| G-2.3 | §68/§69 widgets | **OPEN** — T003-09 |
| G-2.4 | Relay beyond iMessage | **OPEN** — T003-10/11 (blocked on the H-1 mail-connector commit landing first) |
| G-2.5 | Device/simulator verify path | **OPEN** — T003-06 (no signing config; `apple/Distribution/credentials/` = README only) |

### T3 — prompt1 core depth (spec pack: `gap-closure-t003`)

| ID | Gap | Status |
|---|---|---|
| G-3.1 | Real OmniRoute transport | **OPEN** — T003-03 (verified: no `httpx`/`NERVE_OMNIROUTE_URL` in `backend/inference/`) |
| G-3.2 | Ask pipeline (202-ACK → grounded answer) | **OPEN** — T003-04 (verified: `endpoints/context.py` still builds `/ask` via the generic write-endpoint factory → ACK semantics) |
| G-3.3 | `synthetic-lab/` eval corpus | **OPEN** — T003-05 (verified: `.gitkeep` only) |
| G-3.4 | gRPC decision (ADR) | **OPEN** — T003-01 |
| G-3.5 | Remove stale e2e masks | **OPEN** — T003-02, now in **3** files (H-5/§3.7) |

### T4 — Hygiene

| ID | Gap | Status |
|---|---|---|
| G-4.1 | All-bundle `swift test` (T005) | **OPEN** — verified §3.6 |
| G-4.2 | Lifespan migration (T006) | **OPEN** — `wiring.py:352/357` still `on_event` |
| G-4.3 | Status doc refresh (T007) | **OPEN** — content-stale (2026-09-29); path claim corrected §3.7 |
| G-4.4 | JWT test keys (T009) | **PARTIAL** — code done in worktree, uncommitted (H-4) |
| G-4.5 | README Testing section (T010) | **OPEN** |

---

## 5. Sequenced closure plan

### Phase 0 — Integrity gate (≤ 0.5 day; do before anything else)

Ordered by dependency; each item is small and verifiable.

1. **Fix the two calendar time-bombs (H-2).** Apply the §3.2 diffs; run
   `pytest tests/backend/test_calendar.py` → 16 passed. Add the NOTE comments.
2. **Complete the web theme rename (H-3).** `git add web/src/components/ThemeToggle.tsx`;
   `git rm web/src/components/ThemeSwitcher.tsx`; `npm run build && npm run e2e` (56 pass).
3. **Write the mail-connector tests (T002/G-0.2 — M).** `tests/backend/test_mail_connector.py`,
   ≥20 tests per the T002 checklist (probe mapping, OAuth URL/callback/exchange,
   `token_ref` round-trip, registry upsert/404/`verify()` transitions, HTTP contract with
   secret-never-stored assertion), mirroring `tests/backend/test_email.py` patterns.
4. **Commit the integrity slice as logical commits (G-0.1/H-1/H-4).** Suggested 4 commits:
   - `fix(connectors): re-export MailConnector from mail plane (HEAD boot breakage)` —
     **must include the step-3 test file** (the plane lands with tests, closing G-0.2/G-0.4 at
     once);
   - `test(hygiene): clock-invariant calendar window asserts + RFC7518-sized SSO keys`
     (H-2 + H-4, i.e. `test_calendar.py` + `test_auth_sso.py` + the T009 log row in
     `specs/t001-stabilization/tasks.md`);
   - `feat(web): complete ThemeSwitcher→ThemeToggle rename` (H-3: `ThemeToggle.tsx`,
     `Layout.tsx`, deleted `ThemeSwitcher.tsx`);
   - `docs: commit gaps1.md + gaps2.md + t001 task-log progress` (untracked gap docs belong
     in the repo; keep them beside `audit-gaps.md`).
5. **Untrack artifacts (G-0.3/T004 — S).** `git rm -r --cached web/dist`; add `.patches/` to
   `.gitignore`; rebuild web and confirm `git ls-files web/dist` is empty and status is clean
   (Dockerfile builds in-image — verified §3.5).
6. **Gate.** On the resulting commit: `pytest tests/ -m "not integration"` → **559+ passed,
   0 failed**; `git worktree` smoke: fresh checkout of the commit imports
   `backend.api.app` cleanly; `npm run e2e` → 56; per-bundle `swift test --filter` for all
   three bundles (T005 not required yet, but the evidence should be collected).

Exit criteria: `git status` clean; HEAD boots; all suites green *on the commit*; the
T001 verification log rows for T002/T003/T004 are filled with command evidence.

### Phase 1 — Personal reasoning core (~1–2 weeks; spec pack `personal-reasoning-policy-closure`)

Execute **R101 → R102 → R103** strictly (sequential ownership of `backend/communications/**`),
with **R107 in parallel after R101's `Communication.sensitivity` contract lands** (policy
foundation unblocks R105/R108), then R104/R105/R108 in any order, then R106 (e2e wiring),
R109/R110, and finally R112 (MCP 18→29) + R113 (CLI) + R111 (evidence gate).

Key constraints to carry in (all already encoded in the spec pack's "Global
implementation rules", restated because they're the ones most likely to be violated under
pressure):
- Deterministic path is the *required* path; `model_used: "deterministic"` + non-empty
  `why[]` on every surfaced item (§21, §71).
- Flat API shape stays flat; **do not** build the `/api/personal/*` alias layer (decision
  recorded in gaps1 §7 Q2 — keep documenting, not aliasing).
- No persistence tables, no real OmniRoute transport, no Apple UI in this pack (R-packs are
  deliberately bounded; the real transport arrives in Phase 3 and Apple in Phase 4).
- Identity resolution conservative: value-equality links, name-similarity only ever
  `PROPOSED`, merges require the explicit API action.
- Drafts can never reach `SENT` through the API.

Gate: `test_communications.py` + `test_detection.py` (≥15 tests, recall-first fixtures per
§72) + `test_followups.py` + `test_drafts.py` + `test_personal_policy.py` (6-case PDP matrix)
+ `test_operational_state.py` + `test_personal_search.py` green; MCP suite asserts exactly 29
tools; the §62 Alice scenario (one person, three communications, one conversation) passes
end-to-end; §61 scenario reaches NOW with audit fields intact.

### Phase 2 — Privacy & attention policy (3–5 days, overlaps Phase 1 from R107 on)

R107 (firewall + labels + `INFER_ROUTE` PDP decision) → R108 (budget/quiet/escalation) →
R110 (search + memory extraction). PDP matrix must prove: confidential/local-only content
never routes to a remote tier; quiet periods suppress *notification* only, never `/now`
results; budget bundles low-priority items without touching urgent ones. (This is gaps1
G-1.8/G-1.9, now inside the R-pack rather than a free-standing effort.)

### Phase 3 — Core "brain" (~1–2 weeks, parallel track; spec pack `gap-closure-t003`, Phase A)

**T003-03** (OmniRoute transport: `NERVE_OMNIROUTE_URL` env, auth header,
primary/fallback mapping, telemetry write into the routes plane, **recorded-fixture mode**
for tests — see §7 Q3 on whether a live endpoint is reachable) →
**T003-04** (ask pipeline: compiler + PDP pre-flight → routed call → provenance-cited answer;
202-ACK only as honest degradation when inference is unavailable) →
**T003-05** (synthetic-lab corpus + replay + CI regression gate; ask-QA cases first,
detection recall/precision cases land when Phase 1's engines do — read-only dependency).
Quick close-outs on the same track: **T003-01** (gRPC ADR: HTTP is v0, gRPC deferred —
decision, not build) and **T003-02** (remove the stale 404 masks in **all three** spec files —
`ask.spec.ts`, `views.spec.ts`, `views-a.spec.ts` — and re-green; this either finds nothing or
surfaces a real routing bug, either way the suite becomes honest).

Gate: `nerve ask "what broke?"` returns a grounded answer against seeded state with
resolvable provenance ids; `get_route_history` shows telemetry for the routed call; fallback
path exercised by a test; lab report (recall/precision per §72/§81) runs in CI.

### Phase 4 — Apple personal surfaces (~1–2 weeks; spec pack `gap-closure-t003`, Phase B)

**T003-06 first** (dev-sign/simulator or XCUITest verify path — without it everything in this
phase is compile-only, the G-2.5 rationale). Then **T003-07** (iPhone Today/Inbox/People/
Waiting/Briefing + the Today | Engineering | Ask reorg), **T003-08** (Watch personal
experience, "3 NEED YOU" complication, wrist escalation actions), **T003-09** (§68/§69
widgets + timeline cadence), then **T003-10** (relay email capture over the mail-connector
plane — *requires the Phase 0 commit from this plan, read-only dependency*) →
**T003-11** (voicemail/calendar relay scope-in). Every view: loading/empty/error/stale
states, no fabricated rows (the web-quality rule applies to Apple too).

Gate: each new view renders against the real backend in the verify path (seeded attention
item, brief, calendar event visible; empty ≠ error); relay e2e for email ingest with
`source: "personal-relay"` provenance; widget snapshots show the §68/§69 examples from
seeded data.

### Phase 5 — Hygiene sweep (interleaved, ~1 day total; T001 spec pack)

**T005** (all-bundle Apple test runner: root-cause SwiftPM default resolution, else
`apple/scripts/test-apple.sh` with per-bundle `--filter`; `build_and_test.sh` must call the
winner — currently it propagates the 2-test gap) · **T006** (lifespan migration of
`wiring.py:352/357`; remove the deferral comment at :348) · **T007** (refresh
`docs/implementation-status.yaml` + `docs/architecture/current-state.md` to the §2 table —
content refresh, *not* the path fix gaps1 mis-stated; keep `audit-gaps.md` as history) ·
**T010** (README Testing section: the three canonical commands + expected results, stated as
expected not verified until evidence exists). T009 is already done (commit in Phase 0).
T008 is the final integration gate — its verification block already lists every command.

**Workstream mapping (for §73 cross-reference):** PERSONAL-F → R101/R102 · PERSONAL-G →
R103 · PERSONAL-I → R104 · PERSONAL-K → R105 · PERSONAL-H → R108 · PERSONAL-L/M →
T003-07/08 · PERSONAL-O → T003-05 · PERSONAL-N → R107.

---

## 6. Explicit non-gaps (re-verified this pass, no action)

- MCP §45 — all 18 tools present and wired (`_SPEC` inspected).
- CLI §47 core — 14 commands functional.
- iPhone §56 engineering tabs — all six exist (the *personal* views are the gap, G-2.1).
- Attention actions (§55) — resolve/suppress/acknowledge/snooze endpoints exist.
- SSO — Keycloak PKCE live-verified (2026-10-12, `audit-gaps.md`); test fixtures now
  RFC-7518-clean (H-4).
- Relay core engine — cursor/backlog/replay/HTTP-4xx tested (`NerveRelayTests`, 31 pass via
  `--filter`).
- Event bus / DLQ / PDP / persistence / calendar conflict engine — tested; the only calendar
  defect is in the *tests*, not the service (§3.2).
- Web app — 20 views, build clean, e2e 56/56 green (the masks are the only dishonesty, G-3.5).

## 7. Open questions (decisions needed, not work)

1. **Web theme rename (H-3):** complete the rename (recommended — `ThemeToggle` is the newer,
   better-marked component) or revert `Layout.tsx`? One word settles Phase 0 step 2.
2. **`web/dist` policy (carried from gaps1 §7.1):** now confirmed the Dockerfile builds
   in-image, so untrack is safe — needs only the go-ahead to `git rm -r --cached`.
3. **OmniRoute access (carried, now blocking Phase 3):** is a real `NERVE_OMNIROUTE_URL`
   reachable from this host? If not, T003-03/04 proceed in recorded-fixture mode (the plan
   already supports it; T003-05 ships ask-only until detection engines land).
4. **Apple signing (carried):** which dev team/credentials for T003-06, or do we standardize
   on simulator-only UI tests?
5. **gRPC (carried):** confirm HTTP-as-v0 (T003-01 writes the ADR either way; default:
   defer).
6. **Commit hygiene norm (new, from H-1):** should a rule be recorded in the spec packs'
   execution rules — *"no backend code commit that touches an import chain may ship without
   the import verified in a detached worktree / on the resulting commit"*? This pass's
   single worst defect (HEAD un-bootable) is exactly what such a gate would have caught.

---

## Appendix A — Command evidence (this session, 2026-10-14 UTC)

```text
~10:41  pytest tests/ -m "not integration" -q        → 2 failed, 557 passed (4.40s)
10:43   pytest tests/backend/test_calendar.py -v (2 failing) → assertion diffs captured
10:45   cd web && npm run build                       → tsc + vite clean, 64 modules
10:47   cd apple && swift build && swift test         → build OK; swift test: 2 tests (NerveAppsTests only)
10:55   npx playwright test --reporter=line           → 56 passed (27.2s), mock backend auto-stopped
10:55   python -c "from mcp.tools_module import _SPEC" → 18 tools
11:06   git worktree add /tmp/nc-head-verify HEAD; python -c "import backend.api.app"
        → ImportError: cannot import name 'MailConnector' from 'backend.connectors.mail'
11:09   git diff --stat                               → 7 modified, 3 untracked (worktree vs HEAD)
```
