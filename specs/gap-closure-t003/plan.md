# Gap Closure T003 — Phased Plan

**Task ID:** T003 (gap-closure round) · **Spec:** [`spec.md`](./spec.md)
**Upstream:** [gaps1.md](../../gaps1.md) §4/§5 (T2 Apple personal surfaces + T3 prompt1 core depth)

> **gaps2 delta (2026-10-14 evening).** Two premises in this pack changed:
> (1) **T003-02 now covers a *third* masked file** — `web/tests/e2e/views-a.spec.ts:41`
> also carries a stale `KNOWN_MISSING_BACKEND_PATHS` (and its mask additionally
> includes `/api/tasks`, which exists). T003-02's owned files below include it.
> `views-b.spec.ts:41` is **not** a stale mask — it deliberately fulfils
> 409/501/503 responses to exercise the error UI; leave it. (2) **The Phase 0
> mail-connector commit is no longer "staged/uncommitted"** (gaps2 §3.1): the
> plane was committed broken at `a3c07da` and its fix + tests now land via
> [`specs/phase0-integrity`](../phase0-integrity/) P0-04 — T003-10's read-only
> dependency is that pack's final commit, not a pending stage.
> **Import-chain gate (gaps2 §7 Q6, standing rule):** no backend commit touching
> an import chain ships without `python -c "import backend.api.app"` verified in
> a detached worktree of the resulting commit.

Phases mirror gaps1.md §5 (Phase 3 "Core brain" and Phase 4 "Apple personal
surfaces") and run as **two parallel tracks**: the inference track (Phase A)
depends on nothing in-repo besides the existing routes plane; the Apple track
(Phase B) depends on the Phase 1 personal APIs (G-1 tasks, separate workers —
read-only dependency). The gRPC decision (G-3.4) and the e2e mask removal
(G-3.5) are quick close-outs (Phase C).

> **ID note:** T003 here is the *gap-closure* task ID (gaps1.md §5). It is not
> the aws-platform `T003` (v1 schema pack); cross-references say "aws-platform
> T003" where that is meant.

---

## Ground Truth (2026-10-14)

| Area | State | Evidence |
|------|-------|----------|
| Inference | `OmniRouteAdapter` = injected-client framework; no HTTP transport; no `NERVE_OMNIROUTE_URL` | `backend/inference/omniroute.py:60-138`, gaps1 G-3.1 |
| Ask | `POST /api/context/ask` → `202 {accepted:true, executed:false}` ACK stub | `backend/api/endpoints/context.py:16`, gaps1 G-3.2 |
| Context compiler | Exists (`backend/context/compiler.py`) but no firewall/partition (G-1.8 out of scope) | `backend/context/` |
| Routes plane | `InferenceRoute` / `OmniRouteTelemetry` schemas exist; `models_history` + `get_route_history` endpoints exist but nothing feeds them from real calls | `schemas/routes.py`, gaps1 G-3.1 |
| Synthetic lab | `synthetic-lab/` = `.gitkeep` only | gaps1 G-3.3 |
| e2e masks | `KNOWN_MISSING_BACKEND_PATHS = ['/api/systems', '/api/decisions']` stale — both endpoints exist + tested | `web/tests/e2e/ask.spec.ts:19`, `views.spec.ts:22`, gaps1 G-3.5 |
| iPhone views | Only the six engineering tabs | `apple/iOS/App/Views/`, gaps1 G-2.1 |
| Watch | Engineering only (`WatchHomeView/WatchApprovalsView/WatchGoalViews/WatchStopView`) | `apple/Watch/Views/`, gaps1 G-2.2 |
| Widgets | Small/Medium/Large engineering-oriented; `WidgetSnapshot` tested | `apple/Widgets/`, gaps1 G-2.3 |
| Relay | iMessage only (`MessageStore` tails `chat.db`); 31 tests green; mail-connector plane uncommitted | `apple/Relay/`, `backend/connectors/mail/`, gaps1 G-2.4/G-0.2 |
| Device path | No signing config (`apple/Distribution/credentials/` = README only) | gaps1 G-2.5 |
| gRPC | HTTP-only; spec says "HTTP/gRPC interface" | gaps1 G-3.4 |

---

## Task Board

IDs are stable: **T003-01 … T003-12**. AC cross-refs are to
[spec.md](./spec.md).

| ID | Title | AC | Deps | Files owned | Size |
|----|-------|----|------|-------------|------|
| T003-01 | gRPC transport decision (ADR; HTTP = v0, gRPC deferred) | T003.AC11 | — | `docs/architecture/decisions/ADR-*` | S |
| T003-02 | e2e: remove stale known-404 masks; assert real responses | T003.AC12 | — | `web/tests/e2e/ask.spec.ts`, `web/tests/e2e/views.spec.ts`, `web/tests/e2e/views-a.spec.ts` (gaps2: 3rd masked file; do NOT touch `views-b.spec.ts` — deliberate error-UI fixtures) | S |
| T003-03 | OmniRoute real transport (env URL + auth; primary/fallback; telemetry into routes plane; fixture mode) | T003.AC8 | — | `backend/inference/**`, `backend/api/endpoints/models_history.py` (telemetry write path) | L |
| T003-04 | Ask pipeline (compiler → PDP pre-flight → OmniRoute → provenance answer; 202 ACK only on unavailable) | T003.AC9 | T003-03 | `backend/context/ask.py` (new), `backend/api/endpoints/context.py`, `backend/context/compiler.py` | L |
| T003-05 | Synthetic-lab evaluation corpus + replay + CI gate (detection, attention, ask QA) | T003.AC10 | T003-04 (ask pairs); G-1.1 detection engines (read-only; degrade to ask-only corpus until they land) | `synthetic-lab/**`, `tests/` fixtures | M |
| T003-06 | Apple dev-sign / simulator verification path (XCUITest harness against local backend) | T003.AC7 | — | `apple/` (test path only), `apple/e2e-artifacts/**` (evidence) | M |
| T003-07 | iPhone personal views + Today\|Engineering\|Ask reorg | T003.AC1, T003.AC2 | T003-06 (verify); G-1 planes (read-only) | `apple/iOS/**`, `apple/Shared/Sources/NerveAPI/**`, `apple/Shared/Sources/NerveModels/**` | L |
| T003-08 | Watch personal experience (§27/§67 "3 NEED YOU" + §60 wrist actions) | T003.AC3 | T003-06 (verify) | `apple/Watch/**` | M |
| T003-09 | Widgets §68/§69 personal content + timeline refresh cadence | T003.AC4 | T003-06 (verify) | `apple/Widgets/**`, `apple/Tests/**` (snapshot tests) | M |
| T003-10 | Relay email capture (mail-connector plane, `token_ref`, cursor/backlog reuse) | T003.AC5 | Phase 0 mail-connector commit (G-0.2, read-only) | `apple/Relay/**`, `apple/Tests/RelayTests/**` | L |
| T003-11 | Relay voicemail + calendar scope-in (capability-gap skeletons; implement if provider access lands) | T003.AC6 | T003-10 (shared engine); provider-access decision (open question §7.3) | `apple/Relay/**` | M |
| T003-12 | Evidence pass: `docs/implementation-status.yaml` + explicit gap log | T003.AC1–AC12 | all | `docs/implementation-status.yaml`, this file's verification log | S |

## Initial todos

```text
[x] Draft spec.md + plan.md (gap-closure T003)
[x] T003-01  ADR: HTTP-as-v0 transport, gRPC deferred                — 9a51c59 (ADR-0010)
[x] T003-02  Remove stale e2e 404 masks; re-green the suite          — c34712b (+ 2026-10-26 ask.spec.ts T003-04 contract update; 60/60 green)
[x] T003-03  OmniRoute transport + telemetry into routes plane       — 9a51c59
[x] T003-04  Ask pipeline with provenance (202 ACK = degradation only) — 64ba432
[x] T003-05  synthetic-lab corpus + replay + CI regression gate      — 357b327 (2 ask cases honestly pending: ask_fixture.json unpopulated — no real OmniRoute transport per R107)
[x] T003-06  Apple dev-sign / XCUITest verification path             — harness + patch_uitests.py + docs/audit/2026-10-18-baseline committed
[x] T003-07  iPhone personal views + tab reorg (T003.AC1/AC2)        — ba27754
[x] T003-08  Watch personal experience + wrist actions (T003.AC3)    — 7b5eded
[x] T003-09  Widgets §68/§69 + timeline cadence (T003.AC4)           — 8d935eb + fcafccb
[x] T003-10  Relay email capture via mail-connector plane (T003.AC5) — c6d2def
[x] T003-11  Relay voicemail/calendar scope-in (T003.AC6)            — c6d2def
[ ] T003-12  Evidence pass + gap log (AC1–AC12)
```

## Dependency Graph

```text
                        ┌─────────────────────────────┐
   Phase 0 (G-0.2)      │ T003-06 Apple verify path   │
   mail-connector ──────┤        (T003.AC7)           │
   commit (read-only)    └──────┬──────┬──────┬───────┘
         │                      │      │      │
         ▼                      ▼      ▼      ▼
   T003-10 (AC5)          T003-07   T003-08 T003-09
   Relay email            (AC1/2)  (AC3)   (AC4)
         │                      │
         ▼                      │
   T003-11 (AC6)               │
                               │
   ── Phase A (inference) ──────────────────────
   T003-03 (AC8) ──► T003-04 (AC9) ──► T003-05 (AC10)
                               │
                               │  (G-1.1 detection engines, read-only:
                               │   until they land, AC10 corpus ships
                               │   ask+attention cases only)
   ── quick close-outs ─────────────────────────
   T003-01 (AC11)  ·  T003-02 (AC12)   [independent]
                               │
   ────────────────────────────┴──► T003-12 (evidence, last)
```

- **Phase A and Phase B run in parallel** (disjoint file ownership:
  `backend/**` + `synthetic-lab/**` + `web/tests/e2e/**` vs `apple/**`).
- **T003-06 before T003-07/08/09** — without the verify path, T2 evidence is
  compile-only (gaps1 G-2.5 rationale).
- **T003-03 before T003-04** — the ask pipeline needs the transport to route
  through; the 202-ACK degradation path keeps T003-04 testable until
  `NERVE_OMNIROUTE_URL` exists.
- **T003-05 after T003-04** — ask QA pairs need a live pipeline; detection
  cases land when G-1.1 engines do (read-only dependency, logged in T003-12).

## File Ownership

| Owner task | Paths |
|-----------|-------|
| T003-01 | `docs/architecture/decisions/ADR-*` (gRPC) |
| T003-02 | `web/tests/e2e/ask.spec.ts`, `web/tests/e2e/views.spec.ts`, `web/tests/e2e/views-a.spec.ts` (mask removal only) |
| T003-03 | `backend/inference/**`, telemetry write path in `backend/api/endpoints/` |
| T003-04 | `backend/context/**`, `backend/api/endpoints/context.py`, MCP/e2e updates for ask shape |
| T003-05 | `synthetic-lab/**`, `tests/` (fixtures only) |
| T003-06 | `apple/` test harness + `apple/e2e-artifacts/**` (evidence) |
| T003-07 | `apple/iOS/**`, `apple/Shared/Sources/NerveAPI/**`, `apple/Shared/Sources/NerveModels/**` (endpoint types only) |
| T003-08 | `apple/Watch/**` |
| T003-09 | `apple/Widgets/**`, `apple/Tests/**` |
| T003-10/11 | `apple/Relay/**`, `apple/Tests/RelayTests/**` |
| T003-12 | `docs/implementation-status.yaml`, verification log (evidence only) |

Cross-referenced (read-only for T003): `backend/attention/**`,
`backend/briefing/**`, `backend/communications/**` (G-1.2),
`backend/contacts/**`, `backend/messages/**`, `backend/waiting_on/**`,
`backend/connectors/mail/**` (G-0.2 owner), `policy/pdp.py` + `guardrails.py`
(G-1.8 owner).

## Gate (T003 done)

- `.venv/bin/python -m pytest tests/ -m "not integration" -q` green.
- `cd web && npm run e2e` green (masks removed, real 200s asserted).
- `cd apple && swift build` + all three test bundles green + XCUITest/simulator
  run of the personal views against the local backend (T003-06 path).
- `nerve ask "what broke?"` returns a grounded answer (fixtures mode counts)
  with resolvable provenance ids; `get_route_history` shows the telemetry row;
  with `NERVE_OMNIROUTE_URL` unset, the endpoint returns the honest 202 ACK.
- `synthetic-lab` regression gate runs in CI and reports recall/precision.
- gRPC ADR landed; no gRPC code.

## Risks & Mitigations

| Risk | Mitigation |
|------|-----------|
| No real OmniRoute endpoint from this host | Recorded-fixture mode first (AC8 covers both); `NERVE_MOCK_INFER`-style env switch; gap logged in T003-12 |
| G-1 personal APIs not landed when T2 views build | Views ship honest empty states; gap logged; no stub data (house rule) |
| Mail-connector plane still uncommitted when T003-10 starts | Relay email task parks behind the G-0.2 commit; capability-gap fallback in the meantime |
| Apple signing credentials unavailable | XCUITest on simulator is the default verify path; dev-sign documented as the upgrade |
| Voicemail/calendar provider access unknown | AC6 "scoped in-tree" branch; explicit capability-gap errors, no silent no-ops |
| Ask response shape change breaks MCP/e2e | T003-04 owns the MCP/e2e update in the same task; e2e asserts the new shape or the degradation shape, never both silently |
| Synthetic-lab flakiness (non-determinism) | Fixed seeds + versioned fixtures; CI gate fails on regression only, not on new-corpus additions |
