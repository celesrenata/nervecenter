# NerveCenter — Gap Review & Closure Plan, Third Pass (`gaps3.md`)

**Date:** 2026-10-16 (≈14:10–14:40 UTC)
**Supersedes / extends:** `gaps2.md` (2026-10-14) and `gaps1.md` — where prior claims have been
re-verified, turned out stale, or turned out **wrong**, this file says so explicitly and flags the
correction. `audit-gaps.md` (web audit) remains in force for the web design-bar items it owns.
**Scope:** Re-review of `prompt1.md` (core spec) + `prompt2.md` (Personal Ops addendum) against the
live tree at `35d3e78` (2026-10-16) + current worktree, with **fresh runs of every suite**.
**Method:** full read of both prompts' section maps; fresh runs of `pytest` (non-integration),
`npx playwright test`, `swift build && swift test` (bare + per-filter); import/wiring inspection of
`backend/api/{wiring,app}.py`, `backend/api/endpoints/*`, `backend/{inference,context}/*`; grep
re-verification of every "still missing" claim in gaps2; per-bundle Apple test discovery forensics.

---

## 1. Executive summary — the delta since `gaps2.md`

Between `gaps2.md` (2026-10-14, tree `a3c07da`) and now (`35d3e78`, 2026-10-16), the repo gained
**19 commits that are all distribution / TestFlight / docs / deploy work** — *no gap-closure code*
landed. So the **gap inventory is materially unchanged**, but this pass found **three things
`gaps2.md` got wrong or understated**, one of which is a real test-hygiene defect:

1. **NEW (corrects G-4.1): the default `swift test` under-runs — and the prior docs mis-attributed
   the "31".** `gaps2.md` §2 (line 62) and `specs/phase0-integrity/tasks.md` (line 147) both claim
   *"NerveSharedTests 31, NerveAppsTests 2."* **That is incorrect.** The "31" is
   **`NerveRelayTests`**, not Shared. Actual per-bundle counts (verified this pass, §3):
   **NerveSharedTests = 13, NerveRelayTests = 31, NerveAppsTests = 2 → 46 total.** Worse, a bare
   `swift test` previously reported **only 2 tests** because of **stale prebuilt test bundles** under
   `apple/.build/` (the `NerveSharedTests.xctest` binary was dated **Oct 4**, the others Oct 13).
   After forcing a rebuild it now reports all 46. This is a genuine CI/discovery trap, not a doc nit —
   see §3 and Phase A of the plan.

2. **REFRAME (corrects the gaps2 "brain" framing): the prompt2 P0–P2 *surface* plane is now BUILT.**
   `gaps2.md` correctly flagged that the deep reasoning "brain"
   (`backend/communications/`, `followups/`, `drafts/`, `persons/`) does not exist — **still true**.
   But it understated that the **Personal Operations surface plane is in fact implemented and tested**:
   `attention`, `email`, `messages`, `voicemail`, `waiting_on`, `contacts`, `briefing`, `calendar`
   each have a service + FastAPI endpoints + unit tests, and they are wired into the app at startup.
   What is still unbuilt is the *deeper* layer: commitment/request/deadline detection, the unified
   Person/Communication/Conversation model, the follow-up engine, and the drafts plane (the
   `personal-reasoning-policy-closure` R101–R105 scope), plus **persistence** for any of it.

3. **CONFIRMED (closes the "not fully confirmed" flag from the prior review): `POST /api/context/ask`
   is a pure 202-ACK stub.** It is built by the generic `make_write_endpoint` factory with
   `service_name="context"`; no `context` service is ever registered on `app.state.services`
   (`build_services` wires only goals/tasks/entities/relationships; `wire_dependencies` adds
   systems+evidence). The `ContextCompiler` exists in `backend/context/compiler.py` but is **not
   called** by the ask endpoint. So the ask path never does real work — it always returns
   `{"accepted": true, "executed": false, ...}` (202). G-3.2 stays **OPEN** and now has a precise root
   cause (§5, T3).

Everything else in `gaps2.md` §4 (T1 reasoning brain, T2 Apple personal surfaces, T3 core depth,
G-3.x, G-4.x) was re-checked and is **still open, exactly as recorded** — see the §6 ledger.

**Bottom line:** (a) fix Apple test discovery + add CI so the 46 tests actually run in one command
and can't silently under-run again; (b) build the T1 reasoning brain *and* give the whole personal
plane persistence (it is all in-memory today); (c) land the OmniRoute transport + synthetic-lab + real
ask pipeline; (d) build the T2 Apple personal surfaces via the `mobile-apps`/`gap-closure-t003` packs;
(e) close the hygiene items (on_event, CI, status docs, README, CLI personal cmds, MCP personal tools).

---

## 2. Verified current state (evidence, 2026-10-16 this session)

All commands run in the worktree; the repo is otherwise clean except the uncommitted
`docs/testflight-runbook.md` + untracked `apple/.swiftpm/` and two `xcuserdata/` dirs.

| Area | State | Evidence (command, this session) |
|---|---|---|
| **HEAD `35d3e78`** | ✅ boots; imports clean | `git status --short`; last 19 commits all dist/TestFlight/docs |
| Python suite | ✅ **601 passed**, 0 failed (up from 559 in gaps2) | `.venv/bin/python -m pytest tests/ -m "not integration" -q` → `601 passed, 710 warnings in 3.96s` |
| — new tests | ✅ +42 net vs gaps2 (mail-connector plane now tested) | `tests/backend/test_mail_connector.py` present; 8 personal-plane test files (attention/briefing/calendar/contacts/email/messages/voicemail/waiting_on) all present |
| Web build | ✅ `npm run build` clean | `cd web && npm run build` |
| Web e2e | ✅ **60 passed** (up from 56 in gaps2) | `cd web && npx playwright test --reporter=line` → `60 passed (29.3s)` |
| Apple build | ✅ `swift build` clean | `cd apple && swift build` |
| Apple tests (default) | ✅ **46 passed** — **was reporting only 2 before the forced rebuild** (G-4.1 root cause, §3) | `cd apple && swift test` → `Executed 13/31/2 tests` per bundle; `All tests` pass |
| Apple per-bundle | ✅ NerveSharedTests **13** · NerveRelayTests **31** · NerveAppsTests **2** | `swift test --filter <bundle>` after `touch`+`swift build --build-tests` |
| MCP tools | ✅ 18 tools (§45); **0** of the §64 personal tools | `.venv/bin/python -c "from mcp.tools_module import _SPEC; print(len(_SPEC))"` → `18` |
| CLI commands | ✅ 12 command modules; **0** personal (§65) commands | `ls cli/commands/*.py` (agents/ask/changes/compute/entity/goal/incident/mutations/pause/routes/status/task) |
| Personal surface plane (P0–P2) | ✅ attention/email/messages/voicemail/waiting_on/contacts/briefing/calendar services + endpoints + tests — **but in-memory, not persisted** | `backend/api/app.py` mounts all 8 routers; singletons bound in `_configure`; **no** personal tables in the 24 `__tablename__` models |
| Reasoning "brain" (G-1.1–G-1.4) | ❌ `backend/communications/`, `followups/`, `drafts/`, `persons/`, `people/` **do not exist** | `ls backend/` |
| OmniRoute transport (G-3.1) | ❌ no `httpx`/`NERVE_OMNIROUTE_URL`/`aiohttp` in `backend/inference/` | `grep -rn 'httpx\|NERVE_OMNIROUTE\|aiohttp\|urllib' backend/inference/` → no matches (`omniroute.py` is an in-process router over pluggable client objects) |
| Ask pipeline (G-3.2) | ❌ `POST /api/context/ask` = 202-ACK stub (no `context` service; `ContextCompiler` uncalled) | `backend/api/endpoints/context.py` (thin `make_write_endpoint` adapter); `build_services` has no `"context"` key |
| `synthetic-lab/` (G-3.3) | ❌ empty (`.gitkeep` only) | `ls synthetic-lab/` |
| e2e masks (G-3.5) | ⚠️ present in **4** files; **load-bearing, not stale** (see §7) | `web/tests/e2e/{ask,views,views-a,views-b}.spec.ts` |
| FastAPI `on_event` (G-4.2) | ❌ `backend/api/wiring.py:352/357` still `@target.on_event("startup"/"shutdown")`; comment at :348 documents the deliberate deferral | `grep -n on_event backend/api/wiring.py` |
| Status docs (G-4.3) | ❌ `docs/implementation-status.yaml` `updated: "2026-09-29"` — predates personal plane, mail plane, SSO | `head` of file |
| README test commands (G-4.5) | ❌ no canonical Testing section; `README.md:67` shows plain `pytest tests` (hits integration tests) | `grep -n pytest README.md` |
| CI | ❌ none — no `.github/workflows`, `.gitlab-ci.yml`, or `.circleci` | `ls` (all "No such file") |
| Spec packs | ✅ 11 packs present; `phase0-integrity` all `[x]`+gated; `personal-reasoning-policy-closure` + `gap-closure-t003` = the executable breakdown for T1/T2/T3; `mobile-apps/tasks.md` (M201–M206) = the T2 Apple breakdown, **none started** | `specs/` |

---

## 3. The Apple test-discovery defect (NEW — root cause + fix)

**Symptom.** A bare `cd apple && swift test` reported **only 2 tests** (the `NerveAppsTests` bundle)
and claimed `All tests passed` — while 44 other tests existed and were silently not run.

**Root cause (confirmed).** Stale prebuilt test bundles under `apple/.build/out/Products/Debug/`. The
`NerveSharedTests.xctest` binary was dated **Oct 4** while `NerveRelayTests.xctest` and
`NerveAppsTests.xctest` were dated **Oct 13**. SwiftPM's incremental build considered the test
products up-to-date and did not re-link them, so the default run executed only the fresh
`NerveAppsTests` (2 tests) and skipped the stale `NerveSharedTests` (and the per-filter `swift test
--filter NerveSharedTests` reported **0 tests** against the stale bundle). This is a classic
SwiftPM "test product not invalidated on source change" trap — it means a green `swift test` can be
**lying about coverage**.

**Evidence of the fix.** After `touch`-ing the `NerveSharedTests` sources + `swift build
--build-tests`, the bundle re-linked and the full run went to **46 tests, 0 failures**. Per-bundle
counts after the rebuild:

```
NerveSharedTests:  13   (CacheTests 3, EventTests 3, MockTransportTests 3,
                         SynchronizedStateTests 2, TaskStateTests 2)
NerveRelayTests:   31   (MessageNormalizerTests 7, MessageStoreTests 4, OutboxTests 5,
                         RelayConfigTests 9, RelayEngineTests 6)
NerveAppsTests:     2   (NerveAppsTests 2)
────────────────────────
Total:             46
```

**Correction to prior docs.** `gaps2.md:62` and `specs/phase0-integrity/tasks.md:147` attribute
"31" to **NerveSharedTests**; it is **NerveRelayTests**. Shared is **13**. The phase0-integrity
P0-06 "Apple evidence" line therefore overstates Shared coverage and should be amended (see §8,
Phase A / H item).

**Why it matters for CI.** Without a guard, the next developer (or a fresh CI runner with a cold
`.build`) that hits this again will get a false green. The fix is structural (Phase A below): a
single canonical Apple test command that forces a clean test-product build and asserts the expected
per-bundle counts, wired into CI so the under-run can never be silent.

---

## 4. What is now BUILT (the reframe — progress `gaps2.md` understated)

The following **prompt2 Personal Operations surface plane** is implemented, wired into the app, and
unit-tested. It was landed *before* `a3c07da` (the gaps-review base), which is why `gaps2.md`'s
attention stayed on the unbuilt deeper "brain". None of it is persisted — every service is a
**process-wide in-memory singleton** (no DB tables, no event-sourced backing), so state is lost on
restart and is not shared across API replicas:

| Plane | Spec § | Service | Endpoints | Tests |
|---|---|---|---|---|
| Attention (Now/queue/scoring) | §20–22 | `backend/attention/service.py` (`AttentionService`) | `backend/api/endpoints/attention.py` (GET list/now, POST create/resolve/suppress/ack/snooze) | `test_attention.py` |
| Email | §9–10 | `backend/email/service.py` (`EmailService`) | `endpoints/email.py` (threads, ingest, acknowledge, **draft**) | `test_email.py` |
| Messages | §11–13 | `backend/messages/service.py` (`MessagesService`) | `endpoints/messages.py` | `test_messages.py` |
| Voicemail | §14–15 | `backend/voicemail/service.py` | `endpoints/voicemail.py` | `test_voicemail.py` |
| Waiting-on | §19/§31 | `backend/waiting_on/service.py` (`WaitingOnService`) | `endpoints/waiting_on.py` (ingest/resolve/**follow-up draft**) | `test_waiting_on.py` |
| Contacts / person-resolution | §6/§45/§79 | `backend/contacts/service.py` (`ContactsService`) | `endpoints/contacts.py` (resolved people, ingest, VIP) | `test_contacts.py` |
| Briefing | §30 | `backend/briefing/service.py` (`BriefingService`) | `endpoints/briefing.py` (`?kind=morning\|midday\|evening`) | `test_briefing.py` |
| Calendar | §42–43 | `backend/calendar/service.py` | `endpoints/calendar.py` | `test_calendar.py` (clock-invariant) |

Wiring proof: `backend/api/app.py` imports `get_{attention,briefing,calendar,contacts,email,messages,voicemail,waiting_on}_service` and mounts all eight routers (`app.py:29–89`); the singletons are bound onto
`app.state` in `_configure` (with an optional `NERVE_SEED_ATTENTION` dev seed for the Now screen).

**What is still unbuilt on top of this surface (the real T1 gap):**

- **Detection engines** — commitment (§16), request (§17), deadline (§18) are *not* separate engines;
  `waiting_on.follow-up` and `email.draft` are rule-based *stubs* ("AI drafts, human sends"), and
  `Contact`/`AttentionItem` are the only personal model classes in `backend/api/models.py`. There is
  no `Person`/`Communication`/`Conversation`/`Commitment`/`Deadline`/`FollowUp`/`Draft`/`RequestItem`
  schema layer.
- **Cross-channel conversation joining** (§32, §62) — the planes are siloed singletons; nothing joins
  "one person, three communications, one conversation".
- **Follow-up engine** (§57) — no `backend/followups/`.
- **Drafts plane** (§40/§63) — no `backend/drafts/` (drafts today are ad-hoc strings returned by
  `email.draft` / `waiting_on.follow-up`).
- **Persistence / event-sourcing** — none of the personal plane is durable (see §2 row).
- **Privacy/attention policy** (§35–39, §58–59) — no firewall/label/attention-budget enforcement.

This maps exactly to `specs/personal-reasoning-policy-closure` tasks **R101–R105** (foundation →
normalize → detection → follow-up → drafts) and R107–R113 (policy, MCP, CLI).

---

## 5. Confirmed root causes for the still-open core gaps (T3)

- **G-3.1 OmniRoute transport.** `backend/inference/omniroute.py` is an *in-process* router
  (`OmniRouteAdapter.add_provider(name, client)` + `route(...)` over pluggable client objects);
  there is **no network transport** (no `httpx`/`aiohttp`/`requests`, no `NERVE_OMNIROUTE_URL`, no
  auth header handling, no primary/fallback transport, no fixture/offline mode). So "route to
  OmniRoute" cannot actually reach an OmniRoute host today. → `gap-closure-t003` T003-03.
- **G-3.2 Ask pipeline.** Confirmed 202-ACK stub (§1.3). `backend/context/compiler.py`
  (`ContextCompiler`) is real but **never invoked** by the ask path; `backend/context/prioritization.py`
  is likewise orphaned from the endpoint. The honest target is *compiler → PDP pre-flight →
  OmniRoute → provenance answer*, with 202 **only** when inference is genuinely unavailable. → T003-04.
- **G-3.3 synthetic-lab.** `synthetic-lab/` = `.gitkeep` only; no eval corpus, no replay, no CI gate.
  → T003-05.
- **G-3.4 gRPC ADR.** No transport decision ADR filed (existing ADRs ADR-0001…0009 do not cover
  the inference transport). → T003-01.

---

## 6. Per-gap status ledger (delta vs `gaps2.md` §4)

Legend: 🟢 built/closed this pass · 🟡 partially built · 🔴 still open (unchanged).

### T1 — Personal reasoning "brain" (spec pack `personal-reasoning-policy-closure`, R101–R113)

| ID | Gap | Status (this pass) |
|---|---|---|
| G-1.1 | Commitment/Request/Deadline detection (§16–18) | 🔴 OPEN — no detection engines (R103). Rule-based stubs only in `waiting_on`/`email` |
| G-1.2 | Unified Person + cross-channel conversations (§6/§7/§32) | 🟡 **PARTIAL** — `contacts` person-resolution + `AttentionItem` exist (surface), but no `Person`/`Communication`/`Conversation` schema, no cross-channel join (R101/R102) |
| G-1.3 | Follow-Up engine (§57) | 🔴 OPEN — no `backend/followups/` (R104); only a `waiting_on.follow-up` stub |
| G-1.4 | Drafts plane (§40/§63) | 🔴 OPEN — no `backend/drafts/` (R105); drafts are ad-hoc strings |
| G-1.5 | Document flat-API mapping (§63) | 🔴 OPEN (R109) |
| G-1.6 | §64 personal MCP tools (18 → 29) | 🔴 OPEN — **18 confirmed**, 0 personal (R112) |
| G-1.7 | §65 personal CLI commands | 🔴 OPEN — **12 cmd modules, 0 personal** (R113) |
| G-1.8 | Privacy/attention policy (§35–39, §58–59) | 🔴 OPEN (R107/R108) — no firewall/labels/attention-budget |
| G-1.9 | §50 search / §51 memory / §52 daily state | 🔴 OPEN (R109/R110) |
| **NEW G-1.10** | **Personal-plane persistence** | 🔴 **OPEN (newly named)** — all 8 personal services are in-memory singletons; no DB tables, no event-sourced backing, no multi-replica safety. Must be closed for the plane to be "persistent" per prompt1 §3/§14. |

### T2 — Apple personal surfaces (spec packs `mobile-apps` M201–M206 + `gap-closure-t003` T003-07…09)

| ID | Gap | Status (this pass) |
|---|---|---|
| G-2.1 | iPhone personal views (§66) | 🔴 OPEN — `apple/iOS/App/Views/` = only the 6 prompt1 engineering tabs (Agents/Ask/Goals/Incidents/Overview/Systems); **no** Today/Inbox/People/Waiting/Briefing/PersonDetail. `mobile-apps` M202–M205 not started |
| G-2.2 | Watch personal (§27/§60/§67) | 🔴 OPEN — `apple/Watch/Views/` = Approvals/Goal/Home/Stop only |
| G-2.3 | §68/§69 personal widgets | 🔴 OPEN — `apple/Widgets/` = Small/Medium/Large (prompt1 engineering, not personal) |
| G-2.4 | Relay beyond iMessage (email capture §46–48) | 🔴 OPEN — `apple/Relay/` daemon exists (iMessage) but no mail/voicemail/calendar capture; T003-10/11 |
| G-2.5 | Device/simulator verify path | 🔴 OPEN — no signing config; `apple/Distribution/credentials/` = README only; `docs/testflight-runbook.md` (uncommitted) documents the `contentdelivery.apple.com` egress stall |

### T3 — prompt1 core depth (spec pack `gap-closure-t003`, T003-01…06)

| ID | Gap | Status (this pass) |
|---|---|---|
| G-3.1 | Real OmniRoute transport | 🔴 OPEN — §5 (T003-03) |
| G-3.2 | Ask pipeline (202-ACK → grounded answer) | 🔴 OPEN — **confirmed 202-ACK stub** (§5); T003-04 |
| G-3.3 | `synthetic-lab/` eval corpus | 🔴 OPEN — `.gitkeep` only; T003-05 |
| G-3.4 | gRPC/transport decision (ADR) | 🔴 OPEN — no ADR; T003-01 |
| G-3.5 | e2e masks | ⚠️ **RECLASSIFIED** — load-bearing, not stale (see §7); the "remove stale masks" framing in T003-02 needs a per-file triage, not a blanket removal |

### T4 — Hygiene

| ID | Gap | Status (this pass) |
|---|---|---|
| G-4.1 | All-bundle `swift test` | 🔴→**NEW DEFECT** — root cause found (§3): stale `.build` test products under-run to 2. Fix = Phase A canonical Apple command + CI guard |
| G-4.2 | Lifespan migration (`on_event`) | 🔴 OPEN — `wiring.py:352/357` (comment at :348 documents the deliberate deferral) |
| G-4.3 | Status doc refresh | 🔴 OPEN — `docs/implementation-status.yaml` dated 2026-09-29; **must now also record the personal plane (§4) + the Apple-count correction (§3)** |
| G-4.4 | JWT test keys | 🟡 PARTIAL — code done pre-review, verify it's committed (gaps2 said worktree-only) |
| G-4.5 | README Testing section | 🔴 OPEN — `README.md:67` still bare `pytest tests` |
| **NEW G-4.6** | **CI** | 🔴 **OPEN (newly named)** — no CI at all; this is what makes the §3 Apple under-run and any future red suite silent. Highest-leverage hygiene item. |

---

## 7. e2e-mask nuance — do NOT strip blindly (G-3.5 reclassified)

`gaps2.md`/`T003-02` framed the e2e masks as *stale dead-end 404s to remove*. That is **only
partly right** and stripping them wholesale would break tests that depend on them. The masks fall
into **two distinct classes** — the plan must treat them differently:

**Class A — intentional *induced-error* tolerance (KEEP).** These masks exist because the test
*deliberately* induces a 5xx via `page.route` to verify the `role=alert` banner, and the browser's
resource-load error for that induced response would otherwise fail the console-capture policy.

- `web/tests/e2e/views-a.spec.ts:41` — `KNOWN_MISSING_BACKEND_PATHS = ['/api/systems/', '/api/systems',
  '/api/decisions', '/api/tasks']`. The `/api/systems` entries are **load-bearing for the SY-3 test**
  (`views-a.spec.ts:218–234` deliberately `page.route('**/api/systems/**' → 501`) and `/api/tasks`
  for the OV-1/TA-3 induced-500 test. **Do not remove** these — per the standing constraint, the
  "systems entries are for induced-error tolerance."

**Class B — real backend *dead-end* tolerance (KEEP until the endpoint is actually implemented).**
- `/api/decisions` — a **real, current 501** (`backend/api/endpoints/decisions.py:25,37` is a stub
  that raises 501 until a decisions service is wired). Masked in `ask.spec.ts:23`, `views.spec.ts:31`,
  and `views-b.spec.ts:43`. This is a legitimate "endpoint not implemented" mask, **not** stale.

**The one genuinely stale-ish candidate (verify, then act).** Because `SystemsService` is *always*
registered in `wire_dependencies` (`wiring.py:245–252`), the **real** `GET /api/systems/` in the
dependency-free e2e mock backend now returns **200** (the `nervecenter-api` row), *not* 501. So a
mask entry that exists purely because "`/api/systems` is known-missing on the live backend" is now
partly obsolete — but in `views-a.spec.ts` the `/api/systems` entries are still used by the
**induced**-501 test (Class A), so the array entry stays; what can be trimmed is any *comment* that
asserts the live endpoint is missing. **Net: the masks are load-bearing; the actionable work is to
(a) annotate each array entry with which test/endpoint justifies it, and (b) correct the stale
"known-missing /api/systems" prose — not to delete the filters.** This supersedes T003-02's blanket
"remove stale masks".

**Verification of the reclassification (already run):** `npx playwright test` → **60 passed** with the
masks in place; the induced-error tests (SY-3, OV-1, TA-3, DC-1) are among the passing specs.

---

## 8. Phased closure plan

Each phase lists **acceptance criteria (AC)** and **verification commands** runnable on the worktree.
Order is by dependency + leverage, not by effort.

### Phase A — Test-truth foundation (do first; unblocks trust in every other gate)

Goal: make the 46 Apple tests run in one command, make the under-run impossible, and add the CI that
catches any future red. This is the cheapest way to make all later phases *verifiable*.

- **A1.** Add a canonical Apple test command that defeats stale test products. Two acceptable forms
  (pick one and document it in `README` + CI):
  (i) `cd apple && swift build --build-tests --scratch-path .build && swift test`, or
  (ii) a `tools/apple_test.sh` that `touch`es `apple/Tests/**` then `swift test --filter NerveSharedTests --parallel`,
  `--filter NerveRelayTests`, `--filter NerveAppsTests` and asserts each bundle's count.
  **AC:** a fresh/cold run reports exactly **13 / 31 / 2** and `All tests` exit 0.
  **Verify:** `bash tools/apple_test.sh` (or the one-liner) on a clean checkout → `46 tests, 0 failures`.
- **A2.** Add CI (`.github/workflows/ci.yml`) with three jobs:
  - `python`: `.venv`/uv → `python -m pytest tests/ -m "not integration" -q` (expect ≥601).
  - `web`: `npm ci && npm run build && npx playwright test --reporter=line` (expect ≥60).
  - `apple`: the Phase A1 command (expect 46).
  **AC:** all three green on a clean checkout; a deliberately red test fails the pipeline.
  **Verify:** push a branch; confirm the three check-runs; locally `act` or a manual dry-run.
- **A3.** Amend the stale Apple-count claims so the record is true:
  `gaps2.md:62`, `specs/phase0-integrity/tasks.md:147` (and the P0-06 log line :182) — "NerveSharedTests
  31" → "NerveRelayTests 31 / NerveSharedTests 13 / NerveAppsTests 2 (46 total)."
  **AC:** grep for `NerveSharedTests 31` returns 0 hits. **Verify:** `rg -n "NerveSharedTests 31" gaps2.md specs/` → empty.
- **A4.** Land the uncommitted `docs/testflight-runbook.md` (the "Signing & Validating stall" section —
  root cause `contentdelivery.apple.com` unresolvable from egress) as its own docs commit, and add the
  two `xcuserdata/` + `apple/.swiftpm/` paths to `.gitignore` (they are local IDE/SPM state).
  **AC:** `git status --short` clean of those paths. **Verify:** `git status --short`.

### Phase B — T1 reasoning brain + persistence (the largest, highest-value build)

Goal: turn the in-memory surface plane (§4) into a *persistent, reasoning* personal engine. Execute
`specs/personal-reasoning-policy-closure` R101→R105, R107→R113 in dependency order, and close the new
**G-1.10 persistence** gap as part of R101.

- **B1 (R101 + G-1.10).** Foundation schemas + persistence: `Person`, `Communication`, `Conversation`,
  plus `Commitment`, `RequestItem`, `Deadline`, `FollowUp`, `Draft` in `schemas/`; new
  `backend/persistence/models/personal.py` tables + migration; idempotent identity upsert; explicit
  merge. Re-home the surface singletons onto the durable store (or add a durability layer they write
  through). **AC:** `tests/backend/test_communications.py` green; a restarted API still returns the
  same people/attention items; `migrations` adds the tables.
  **Verify:** `.venv/bin/python -m pytest tests/backend/test_communications.py tests/backend/test_attention.py -q`;
  boot API, `POST /api/contacts/ingest`, restart, `GET /api/contacts` → still present.
- **B2 (R107).** Personal policy: context firewall (§35), sensitivity labels (§36), inference
  routing/privacy policy (§37–39), attention budget + quiet periods (§58–59). Six-case PDP matrix.
  **AC:** `tests/backend/test_personal_policy.py` green; a personal-context query to the engineering
  PDP is denied by the firewall.
- **B3 (R102 + G-1.2).** Normalize email/messages/voicemail ingest into the `communications` layer;
  cross-channel conversation join. **AC:** the §62 scenario (one person, three communications, one
  conversation) round-trips. **Verify:** `tests/backend/test_communications.py::test_cross_channel`.
- **B4 (R103 + G-1.1).** Deterministic commitment/request/deadline detection engines
  (`backend/communications/detection/`). ≥15 recall+precision tests. **AC:** `test_detection.py` green;
  detected items emit events + spawn `AttentionItem`s with an explainable source.
- **B5 (R104 + G-1.3).** Follow-up engine (`backend/followups/` + `endpoints/followups.py`): due
  scheduling, lifecycle events, snooze/resolve propagation. **AC:** `test_followups.py` green;
  `waiting_on` regression still passes (the stub is replaced, not duplicated).
- **B6 (R105 + G-1.4).** Drafts plane (`backend/drafts/` + `endpoints/drafts.py`): CRUD/discard,
  deterministic fallback drafter, policy-gated; **no API transition to `sent`** (AI drafts, human sends).
  **AC:** `test_drafts.py` green; `POST .../send` is absent/405.
- **B7 (R109/R110 + G-1.9).** §50 search + §51 memory extraction + §52 daily operational state + flat-API
  mapping doc (§63). **AC:** `test_personal_search.py` + `test_operational_state.py` green.
- **B8 (R112 + G-1.6).** §64 personal MCP tools: 18 → **29** with exact names/schemas/paths/methods.
  **AC:** `from mcp.tools_module import _SPEC; len(_SPEC) == 29`; `tests/mcp/` green.
- **B9 (R113 + G-1.7).** §65 personal CLI commands (attention/today/commitment/brief/waiting/people).
  **AC:** seeded-backend smoke for every command, `--json`, and exit codes.
- **B10 (R111).** Final evidence gate: full non-integration suite, 29-tool MCP suite, CLI smoke,
  §61/§62 scenarios, status record.

### Phase C — T3 core depth (OmniRoute + ask + synthetic-lab)

Execute `gap-closure-t003` T003-01…05.

- **C1 (T003-01 / G-3.4).** File the transport-decision ADR (HTTP = v0, gRPC deferred).
  **AC:** `docs/architecture/decisions/ADR-0010-inference-transport.md` present + referenced.
- **C2 (T003-03 / G-3.1).** Real OmniRoute transport in `backend/inference/`: env `NERVE_OMNIROUTE_URL`
  + auth, primary/fallback, telemetry into the routes plane, fixture/offline mode. **AC:** a live (or
  fixture) round-trip through the transport; unit tests for auth/timeout/fallback.
  **Verify:** `.venv/bin/python -m pytest tests/ -k "omniroute or transport" -q`.
- **C3 (T003-04 / G-3.2).** Ask pipeline: `backend/context/ask.py` (compiler → PDP pre-flight →
  OmniRoute → provenance answer). **202 only when inference is genuinely unavailable.** Wire a `context`
  service so `/api/context/ask` executes. **AC:** `POST /api/context/ask` returns a grounded,
  provenance-bearing answer (200) when OmniRoute is reachable; returns 202 `executed:false` only when
  it is not. **Verify:** with `NERVE_OMNIROUTE_URL` set → `curl -s -X POST :8000/api/context/ask -d
  '{"question":"..."}'` → 200 + `result`; unset → 202.
- **C4 (T003-05 / G-3.3).** Synthetic-lab eval corpus + replay + CI gate (detection, attention, ask QA).
  **AC:** `synthetic-lab/` non-empty; a replay script runs in CI and fails on regression.

### Phase D — T2 Apple personal surfaces (the mobile delta)

Execute `mobile-apps` M201→M205 (drives T2) and `gap-closure-t003` T003-06/07/08/09. Depends on Phase B
(personal API) being stable enough to feed the views, and on Phase A (Apple test command) for
verification.

- **D1 (M201).** `NervePhone` (iOS) + `NerveWatch` (watchOS) executable targets + root navigation.
  **AC:** `xcodebuild -scheme NervePhone build` + `-scheme NerveWatch build` green.
- **D2 (M202 / G-2.1).** iPhone personal surfaces: Today/Inbox/People/Waiting/Briefing/PersonDetail
  per §66, fed from `NerveAPI` + `NervePersistence` cache with loading/empty/error/stale states;
  **no hardcoded fixture rows**. **AC:** unit tests exercise all four states per view.
- **D3 (M205 + T003-08 / G-2.2).** Watch personal experience: §27/§67 "3 NEED YOU" + §60 wrist
  actions; complication in §68 priority order; cached-state staleness marking. **AC:** complication
  priority order unit-tested.
- **D4 (T003-09 / G-2.3).** §68/§69 personal widgets + timeline refresh cadence. **AC:** snapshot tests
  in `apple/Tests/`.
- **D5 (T003-06 / G-2.5).** Apple dev-sign/simulator verify path (XCUITest harness against local
  backend); log to `docs/implementation-status.yaml`. Note the `contentdelivery.apple.com` egress stall
  (uncommitted runbook) as a known TestFlight constraint.
- **D6 (T003-10/11 / G-2.4).** Relay email capture (+ optional voicemail/calendar scope-in) once
  provider access lands.

### Phase E — Hygiene closeout

- **E1 (G-4.2).** Migrate `wiring.py:352/357` `on_event("startup"/"shutdown")` → FastAPI `lifespan`;
  update the :348 comment. **AC:** `rg -n "on_event" backend/` → 0 hits; app boots identically
  (`/api/health` 200).
- **E2 (G-4.3).** Refresh `docs/implementation-status.yaml`: record the personal plane (§4), the Apple
  count correction (§3), and bump `updated:` to 2026-10-16. **AC:** file reflects current tree; dated
  today.
- **E3 (G-4.5).** Add a canonical **Testing** section to `README.md` (the exact commands from Phase A2,
  including `-m "not integration"` and the Apple test command). **AC:** `rg -n "pytest tests$"
  README.md` no longer shows a bare integration-hitting command.
- **E4 (G-4.4).** Confirm the JWT RFC7518-sized test keys are **committed** (gaps2 flagged them
  worktree-only). **AC:** `git log --oneline -- tests/` shows the commit; clean status.

### Suggested sequencing & owners

```
Phase A (1–2 days)  ──────────────►  unblocks all gates (CI + Apple test truth)
   │
   ├─► Phase B (T1 brain + persistence, R101→R113)  ──  largest; single backend owner
   │        │
   │        └─► Phase C (OmniRoute/ask/synthetic-lab, T003-01..05)  (C3 depends on C2)
   │
   └─► Phase D (T2 Apple surfaces, M201→M205 + T003-06..09)  (depends on B for personal API, A for verify)
Phase E (hygiene)  ──  can run in parallel with B/C/D; E2 last (records the final state)
```

Single-owner rule: `apple/**` has one worker (per `mobile-apps/tasks.md`); `backend/**` personal
plane has one worker (per `personal-reasoning-policy-closure`); `synthetic-lab/` + `backend/inference/`
are one worker (T003). Defects found in the read-only SDK contract packs (`mobile-quality-audit`
M101–M105) go back as follow-ups, not in-place edits.

---

## 9. Open questions / risks to resolve before closing

1. **Persistence model for the personal plane (G-1.10).** Should personal entities be first-class rows
   (new tables + migration) or events in the existing event-sourced fabric (`events` table)? The spec's
   "persistent" philosophy (§3/§14) favors event-sourcing, but the surface plane is currently
   single-entity rows. Decide in B1 before B2–B7 build on it.
2. **OmniRoute availability.** C3 (real ask) is blocked on a reachable OmniRoute (or a faithful
   fixture). Confirm the deploy target exposes `NERVE_OMNIROUTE_URL`; if not, the fixture mode in C2 is
   the only way to land a *verified* ask pipeline this cycle.
3. **Apple build tooling.** D1–D5 assume `xcodebuild` schemes exist for NervePhone/NerveWatch. The
   generated Xcode projects live under `apple/Distribution/Generated/`; confirm the schemes build on
   this host before committing to the D-series verification commands.
4. **TestFlight egress.** The uncommitted runbook documents `contentdelivery.apple.com` as
   unresolvable from this egress — device/TestFlight verification (D5) may be blocked by network
   policy, not code. Treat D5's "device" portion as best-effort; simulator verification is the
   portable gate.
5. **e2e-mask ownership.** §7 reclassifies the masks as load-bearing; confirm with the web owner that
   the induced-error tests (SY-3/OV-1/TA-3/DC-1) are the *intended* reason before annotating, so we
   don't "fix" a mask that was masking a still-wanted future behavior.

---

*Prepared by re-running every suite this session (601 py / 60 web / 46 apple) and re-verifying each
gaps2 "still missing" claim against the live `35d3e78` tree. No repo files were modified during this
review; only `.build/` artifacts were touched by the forced Apple test rebuild (untracked).*
