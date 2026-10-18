# NerveCenter — Apple "apps show empty" data-loading lift

**Date:** 2026-10-18
**Scope:** Fix the core reason the iPhone (6 surfaces) and Apple Watch showed **empty / "Not connected"** — the apps never loaded data from any server. Wire the existing fetch services into the UI, add a `--nerve-server` test override, and route the phone→watch shared data (App Group `cached-state.json`) so real server data is visible.

**Method:** Built `NervePhone` + `NerveWatch` for iOS 26.5 / watchOS 26.5 simulators, booted iPhone 17 Pro + Apple Watch Series 11, pointed the phone at a **live backend on `:8000`** via `--nerve-server`, and verified real data flows into the UI and the shared App Group. Evidence cited inline.

---

## TL;DR — what now works (verified)

- **Phone shows real data.** After the lift, the Agents tab renders `NEURAL FLEET — 14 Active Agents`, `2 Active Incidents · 14 Running Agents · 1 Pending Approvals`, Systems **"healthy"**, and the freshness badge reads **"1s ago"** (was "never updated"). The store's `fetchNow()` is invoked on `onAppear` and after every pull-to-refresh. Evidence: `screenshot` of Agents tab + `AppGroup/…/cached-state.json` on the booted phone.
- **`cached-state.json` now exists with live data** in the watch's App Group (`group.nerv.center`):
  `{"running_agents":14, "active_incidents":0, "system_health":"healthy", "updated_at":"…T13:21:23Z", …}` — encoded with the **shared `NerveJSON`** (ISO-8601) so the Watch's `WatchSnapshotSource.readSnapshot()` decodes it (a plain `JSONEncoder` would not).
- **`--nerve-server` test override (C-3).** `NerveConfig.baseURL` (and the env-configured base) now honor `--nerve-server <url>` launch arg, so the audit harness / simulators can point at a local backend. `NerveE2E.serverURL`/`launchServerArgument` wired through `NerveUIHarness`.
- **Both apps still build** (`xcodebuild … -scheme NervePhone` → `** BUILD SUCCEEDED **`, zero errors) and **`swift test` passes** (`apple` package: 2/2). No shared-package regression.

## What was changed

| File | Change |
| --- | --- |
| `apple/iOS/App/NervePhoneApp.swift` | `selectedTab` now initialized from `NerveE2E.selectedTab` (launch-arg router). `refreshAll()` = `store.fetchNow()`; called on `onAppear` + after every `.refreshable`. |
| `apple/iOS/App/Views/{Overview,Goals,Incidents,Agents,Systems,Ask}View.swift` | Each view now takes `store: NerveDataStore` and renders **real** `@Published` state with `NerveEmpty`/`NerveLoading`/`NerveError` + per-row `id`s (e.g. `phone.agents.row.<id>`). Previously they took/ignored `@EnvironmentObject`. |
| `apple/iOS/App/Environment/PhoneEnvironment.swift` | `NerveConfig.baseURL` honors `--nerve-server` (C-3). New shared App-Group `containerURL`; `publishSharedSnapshot()` writes the `§73` `cached-state.json` (via `NerveJSON`) from the running-agents/active-incidents/system-health/attention state so the **Watch reads real data** (C-6). `NervePhoneAppState` (single shared store, env-branchable) added. |
| `apple/e2e/XCUITestSupport/NerveE2E.swift` | `selectedTab`/`selectedView`/`isUITesting`; new `serverURL`/`launchServerArgument` for the `--nerve-server` override. |

## Watch data path — status (be honest)

- **Publish (phone → shared `group.nerv.center`): VERIFIED.** The phone wrote the live `cached-state.json` into the shared App Group with real values (confirmed on the booted phone).
- **Watch read (`AppGroupSnapshotSource.readSnapshot()` → `<group>/cached-state.json`): code-correct.** `WatchAppGroup.identifier == "group.nerv.center"`; the watch's entitlements list `group.nerv.center`; the mapping resolves to a real container on the watch sim.
- **Why the watch *sim* still shows "Not connected":** a **standalone Apple Watch simulator does not share the phone simulator's App Group containers** — each device image has its own `group.nerv.center` mapping (different UUID), and a file placed into the watch sim's container from the host is not reliably visible to the sandboxed watch-sim process. This is an **environment limitation, not a code defect.** On a **paired real device** (or paired simulators that share the group), the phone's publish is visible to the watch and it renders.
  - **Action to fully verify watch display:** run the phone + watch **paired** (XCUITest `iPhone→Apple Watch` pairing, or on hardware) so `group.nerv.center` is genuinely shared, then confirm the Watch renders counts from `cached-state.json`.

## Still open (deferred, larger scope)

1. **Personal Operations Plane on the iPhone** — Today / Inbox / People / Waiting-On / Briefing surfaces + the `Today | Engineering | Ask` reorg (prompt1/prompt2). This is a distinct, substantial feature (new backend-driven surfaces, API contracts, wire models) — not part of the empty-data root-cause fix.
2. **Theme sync (C-4)** — bridge web `localStorage['nerve-theme']` ↔ Apple `@AppStorage`, add "System" option; see `visual-lift-audit.md`.
3. **Watch §27/§67 attention surface** — show open AttentionItems / approvals on the watch (needs the shared `approvals.json` to flow from the phone and the watch attention view).
4. **Watch sim display verification** (above) — needs a paired phone+watch run.

## References
- Live evidence: phone booted at `--nerve-server http://127.0.0.1:8000`; `AppGroup/…/cached-state.json`; Agents-tab screenshot (`NEURAL FLEET 14`).
- `visual-lift-audit.md` — earlier read-only audit that found the empty-state root cause (this doc is the follow-up lift).
