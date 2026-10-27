# Personal Operations Recovery — e2e Acceptance Spec (degraded backend)

**Status:** Normative acceptance contract for the N-workstream e2e suite
(`web/tests/e2e/personal-flow.spec.ts`). It records *exactly* what the
playwright suite asserts, against which wire contract, and — where a success
criterion cannot be literally demonstrated on the dependency-free
**degraded** backend — the **honest degraded / in-memory surface** that is
asserted instead, plus the durable-only unit test that covers the gap.

This spec is the source of truth for selector strings and wire expectations.
Every assertion in `personal-flow.spec.ts` traces to a row below.

## 0. Test topology (fixed)

- Harness: `web/playwright.config.ts` — `testDir ./tests/e2e`, `workers: 1`,
  `fullyParallel: false`, chromium-only, `baseURL http://localhost:3000`.
- Backend: `global-setup.mjs` spawns
  `.venv/bin/python -m uvicorn backend.api.app:app --port 8000` with
  `NERVE_MOCK_INFER=1` and **no** `NERVE_DB_*` → the app boots in the
  dependency-free **degraded** mode (`/api/health` → `{"status":"degraded", …}`).
  **No** `NERVE_SEED_ATTENTION`/`NERVE_SEED_…` are set, so no demo records are
  seeded (success criterion **14** — the suite is deterministic empty-then-populate).
- Frontend: the `webServer` hook runs `npx vite --port 3000 --strictPort` with
  `VITE_API_BASE=/api`, so `useMockMode === false` and the browser client uses
  the **real** backend (via the vite `/api` proxy to :8000) instead of the
  in-browser mock fixtures.
- Auth: the dependency-free backend has no `/api/auth/login` to exercise, so the
  suite seeds `localStorage.authToken` via `page.addInitScript` (same convention
  as `views.spec.ts` / `ask.spec.ts`). The app is ungated (`App.tsx:28-29`).
- Console-error policy (identical to `ask.spec.ts`): fail on any `console`
  `error`, any `pageerror`, any `requestfailed`. The **only** masked resource
  error is a `Failed to load resource` whose `msg.location().url` ends in
  `/api/decisions` (that endpoint answers a real 501 and DecisionsView degrades
  to its empty state without an application error). **Every** route this suite
  touches answers 2xx, so the filter never fires on it.

Shared in-memory backend state is mutated by exactly one spec file in this run
(this one); no sibling spec mutates attention/calendar/connections/bills state,
so empty-then-populate assertions are deterministic within a full-suite run.

## 1. Success-criterion coverage

| # | Criterion (spec §2) | Demonstrated on the degraded backend? | What the e2e asserts | Durable-only gap → unit test |
|---|---|---|---|---|
| 7 | Calendar appointment appears on Today | **No** (in-memory calendar does not feed `GET /api/today`) | CalendarView: ingest → event renders in **Upcoming** (live) | `tests/backend/test_today.py::test_coming_up_calendar_window` (durable calendar within 48h → `coming_up`) |
| 8 | Bill reminder escalates near due date | **No** (bills need durable storage; writes are `executed:false`) | BillsView: empty states + degraded **create** surfaces the honest "demo mode — not persisted" badge; `confirm-paid` → **202** `{"executed":false,…}` → "Not recorded — bills storage is not configured." | `tests/backend/test_bills.py::TestEscalationAttention`, `TestConfirmPaid` |
| 11 | Snooze / complete / dismiss | **Partially** (in-memory attention is live) | NowView: create → item in NOW feed; **Snooze** → leaves NOW, status `TODAY`, `due` = now+1d | `tests/backend/test_today.py::test_needs_you_ordering_importance_then_deadline`; `tests/backend/test_attention_v2.py` |
| 13 | Failed connector → actionable diagnostics | **Partially** (degraded write) | ConnectionsView: wizard pick→credentials → degraded write **201** `executed:false` → step error renders the backend `detail` | `tests/backend/test_connectors_registry.py::test_record_sync_failure_escalates_degraded_then_error` |
| 14 | Whole flow works without sample/demo records | **Yes** | Every section starts at its honest **empty state** (no seeded demo records) before the test populates it | `tests/backend/test_restart_persistence.py` (restart path); harness env in §0 |
| 15 | UI distinguishes the five connection states | **Partially** (only `not_configured` reachable degraded) | ConnectionsView: **empty** list "No connections yet" + zero fabricated cards after a failed write | `tests/backend/test_connections_api.py::TestCreate.test_create_201_with_derived_state` (five-state mapping) |

Criteria **1–6, 9–10, 12** are durable-only (mail ingestion, relay,
commitments, device push, privacy policy) and are out of scope for the degraded
e2e path; they are covered by the durable unit tests cited above and the
`privacy-review.md` policy audit. This suite deliberately does **not** fabricate
durable behavior.

## 2. Wire contract (empirically verified on the degraded backend)

All of the following were probed against a live
`NERVE_MOCK_INFER=1` degraded backend (port 8000/8123) before this spec was
frozen:

- `POST /api/attention` (reason + urgency/importance, **no** `due`, **no**
  `status`) → **201** `{attention_id:"att-…", status:"NOW", due:null, priority,
  urgency, importance, why:[…]}`. New items default to `NOW`
  (`backend/attention/service.py:88`, `backend/api/endpoints/attention.py:115`).
- `GET /api/attention/now` → `{data:[…], meta:{…}}` (NOW bucket only).
- `POST /api/attention/{id}/snooze` (empty body) → **200** `{status:"TODAY",
  due:<now+1 day ISO 'Z'>}`; the item **disappears** from `GET /api/attention/now`
  and appears under `GET /api/attention?status=TODAY`
  (`backend/attention/service.py:195-207`).
- `POST /api/calendar/ingest` → **201** (idempotent **200** on replay);
  `GET /api/calendar` (default window = next 7 days) →
  `{data:[{event_id,title,start,end,location,…}], meta:{…}}`
  (`backend/api/endpoints/calendar.py:120-136`, `85-99`).
- `POST /api/connections` → **201** but **degraded**:
  `{accepted:true, executed:false, request_id:"req-…", timestamp, detail:"durable
  storage is not configured"}` (`backend/api/endpoints/connections.py:73-83`).
  `GET /api/connections` → `{data:[], meta:{…}}` — **zero fabricated cards**
  (`_degraded_read`, `connections.py:68-70`).
- `POST /api/bills/{bill_id}/confirm-paid` → **202** `{executed:false,
  reason:"persistence_not_configured"}`; `GET /api/bills/` →
  `{bills:[], durable:false}` (`backend/api/endpoints/bills.py:126-129,
  229-272`).
- `GET /api/today` → **200** all-empty `{needs_you:[], coming_up:[],
  handled:[], low_energy:false, simplified:false}` on the degraded backend
  (its providers read the durable `registry`, which is `None` without a DB).
  `PATCH /api/today/mode` → **200** `{accepted:true, executed:false,
  low_energy:false, detail:"durable storage is not configured"}`
  (`backend/api/endpoints/today.py:19-36`).

## 3. DOM contract — exact selectors

### ConnectionsView (`web/src/views/ConnectionsView.tsx`)
- Header `Add connection` button (`ConnectionsView.tsx:201-208`).
- Empty state: `No connections yet` (`:251`); empty-state button
  `Add your first connection` (`:261-265`).
- No `role="alert"`/`role="status"` banner in this degraded read — the mock-mode
  banner is gated on `useMockMode` (`:211-217`), which is **false** here.

### AddConnectionWizard (`web/src/components/connections/AddConnectionWizard.tsx`)
- Mounts as `role="dialog"` `aria-label="Add connection wizard"` (`:258-259`);
  heading `Add a connection` on `pick_kind` (`:249-250`).
- Pick kind: `role="radiogroup"` `aria-label="Connection kind"`; relay option is
  `role="radio"` text `Apple Messages relay` (`:290-310`; presets in
  `wizard.ts:21-26`).
- Credentials (relay): a single field labelled `Device name`
  (`wizard.ts:86-88`).
- Primary button labels: `Continue` (pick_kind) → `Save & start sign-in`
  (credentials) (`:70-79`). Relay flow is
  `pick → credentials → auth → import → done` (skips `verify`)
  (`wizard.ts:94-104`).
- Degraded write: `isDegradedWrite(res)` → `throw new Error(res.detail)` so the
  step error renders the backend `detail` in a `role="alert"` paragraph
  (`AddConnectionWizard.tsx:133-149, 283-290`). `isDegradedWrite(r) ===
  r.executed === false` (`web/src/api/connections.ts:161-167`).

### NowView (`web/src/views/NowView.tsx`)
- Default tab is `NOW` (`NowView.tsx:72,18`); item card
  `data-testid="attention-{attention_id}"` (`:202`); card shows `item.reason`
  (`:218-219`).
- Actions per item: `Resolve` (`:254-262`), `Snooze` (`:263-271`), `Suppress`
  (`:272-277`).
- Create form: reason input `data-testid="now-reason-input"` (`:305`); submit
  button `Add item` / `Adding…` (`:350-356`).
- Empty state: `Nothing in the <NOW> bucket right now.` (`:184-190`).
- Error banner `role="alert"` `Attention feed unavailable` (`:174-182`).

### CalendarView (`web/src/views/CalendarView.tsx`)
- `h1` = `Calendar` (`:144`); Upcoming event `<li>` shows `ev.title` + range
  (`:178-191`).
- Empty state: `Nothing on the calendar in the next 7 days — availability
  checks against an empty schedule.` (`:171-176`).
- Reasoning buttons: `Check availability`, `Find free slot`, `Suggest (§43)`
  (`:228-252`).

### TodayView (`web/src/views/TodayView.tsx`)
- `h1` = `Today` (`:164`). Sections `today-needs-you` / `today-coming-up` /
  `today-handled` (`:248,293,321`).
- Empty states: `Nothing needs you right now.` (`:254`), `Nothing scheduled in
  the next 48 hours.` (`:299`), `Nothing handled in the last 24 hours.`
  (`:327`).
- Connections section: `No connections configured yet.` when the list is empty
  (`:362-368`).
- Low Energy Mode: `role="switch"` labelled `Low Energy Mode` (`:168-182`); on a
  degraded `PATCH /mode` the view surfaces `Low Energy Mode is not available
  right now — no durable storage configured.` (`:117-128, 189-196`).

### BillsView (`web/src/views/BillsView.tsx`)
- `h1` = `Bills` (`:436`); degraded badge `demo mode — not persisted` when
  `durable === false` (`:449-451`).
- Empty states: `No bills yet…` (`:480`), `Nothing due in the next two weeks.`
  (`:547`).
- Add-bill form `aria-label="Add a bill"` (`:191`); submit `Add bill` /
  `Adding…` (`:263-265`).
- Degraded confirm: `confirmPaid` flashes `Not recorded — bills storage is not
  configured.` when `res.executed === false` (`:401-411`), shown in a
  `role="status"` notice (`:476-479`).

## 4. Honest-surface rationale (the durable-only divergence)

A "calendar appointment appears on **Today**" (criterion 7) and a "bill reminder
escalates" (criterion 8) cannot be *literally* rendered on the degraded backend:
`GET /api/today` and the bills plane read the durable `repository_registry`,
which is `None` when no DB is configured, so in-memory attention/calendar data
does **not** surface on Today. This spec therefore asserts the surfaces that
*are* live on the degraded backend:

- **Live attention/calendar** → the in-memory **Now feed** (`/api/attention/now`)
  and **Calendar Upcoming** (`/api/calendar`), not the Today card.
- **Connections** → the **degraded-write contract** (`executed:false` + honest
  empty list + zero fabricated cards), not the five durable states.
- **Bills** → the **degraded create/confirm** surfaces, not durable escalation.

This mirrors the task's own sanctioned "assert the degraded state and cite the
unit test" pattern (criterion 8) and is applied uniformly. The durable-only
behavior is pinned by the unit tests cited in §1, so the e2e suite never
fabricates a durable record or claims a Today projection it did not observe.
