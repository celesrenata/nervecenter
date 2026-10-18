# NerveCenter — Apple Gap Review & Closure Plan (`gaps-apple1.md`)

**Date:** 2026-10-18
**Scope:** Apple plane only — `apple/` (iOS app, Watch app, Widgets, Relay daemon, shared packages, Distribution) — against `prompt1.md` (engineering plane, §56/§73) and `prompt2.md` (Personal Operations, §27/§60/§66–§70), using **live simulator evidence** rather than static reading.
**Method:** built `NervePhone` + `NerveWatch` for iOS 26.5 / watchOS 26.5 simulators; booted iPhone 17 Pro + Apple Watch (Series 11); captured a **baseline visual audit** (6 phone tabs + 6 watch screens, `docs/audit/2026-10-18-baseline/`); applied the **data-loading lift**; re-audited against a live backend on `:8000` via the new `--nerve-server` launch-arg override; ran `swift build` + `swift test`. Findings cite `file:line`.
**Companion docs:** `docs/apple-data-lift-2026-10-18.md` (what was fixed this session, with before/after evidence), `visual-lift-audit.md` (2026-10-15 read-only lift audit), `gaps1.md` (2026-10-14 full-stack gap review — item 3 there is this doc expanded).

---

## 1. Executive summary

The Apple plane was in two layers of incompleteness:

1. **Data plumbing (now FIXED this session):** the phone apps rendered from a hydrated cache only and the watch read an App Group nothing had written to. Every phone surface was `NerveEmpty`; the watch said "Not connected". Lifted: fetch-on-load wired into the store, `--nerve-server` test override, and phone→watch App Group `cached-state.json` publish (verified with live values: 14 agents / healthy / "1s ago").
2. **Personal Operations surfaces (NOT started — this doc):** the iPhone has only the six §56 *engineering* tabs; the §66 personal views (Today/Inbox/People/Waiting/Briefing), the §66 `Today | Engineering | Ask` reorg, §67 Watch complications, §68/§69 personal widget content, and §27/§60 Watch attention/escalation are all absent. The Relay daemon captures **iMessage/SMS only** (no email, voicemail, calendar, contacts) — so even the *backend-side* personal data the Apple surfaces would consume is only partially flowing to the phones/watch.

Also flagged: **theme sync** (C-4, carried from `visual-lift-audit.md`), a **watch-simulator verification limit** (standalone watch sim doesn't share the phone sim's App Group), and **hygiene** (mixed uncommitted tree, thin UI-layer test coverage).

Everything below is ordered by impact. Each item: what's missing (spec §), evidence, work needed, acceptance criteria, size (S ≤ ~1h, M ~half-day, L ≥ day).

---

## 2. Verified current state (evidence, 2026-10-18)

| Plane | State | Evidence |
|---|---|---|
| Phone data loading | ✅ **Fixed** — all six tabs fetch on load and render live data | Re-audit screenshot: Agents = "NEURAL FLEET — 14 Active Agents", "2 Active Incidents · 14 Running · 1 Pending", Systems "healthy", "1s ago" (baseline was `No agents yet` / "never updated"). Before: `docs/audit/2026-10-18-baseline/phone/*` |
| Phone→Watch shared snapshot | ✅ Publish verified; ⚠️ read blocked in standalone watch-sim env | `group.nerv.center/cached-state.json` written by phone with live values; watch read code correct (`WatchSnapshotSource.swift:17` `identifier = "group.nerv.center"`); see G3 below |
| Phone build + tests | ✅ `xcodebuild -scheme NervePhone` SUCCEEDED, 0 errors; `swift test --package-path apple` 2/2 pass | session log 2026-10-18 |
| E2E harness | ✅ T003-06: launch-arg router (`--nerve-tab`/`--nerve-view`/`--nerve-server`), accessibility ids, boot data-dump | `apple/e2e/XCUITestSupport/NerveE2E.swift`, `apple/e2e/scripts/baseline_audit.sh` (untracked) |
| Phone personal views (§66) | ❌ absent | `apple/iOS/App/Views/` = exactly {Overview, Goals, Incidents, Agents, Systems, Ask}.View.swift |
| Watch attention (§27/§67/§60) | ❌ absent — watch shows engineering snapshots (goals/incidents/approvals/stop) or "Not connected", never "n NEED YOU" | `apple/Watch/Views/` = {WatchHome, WatchGoal, WatchApprovals, WatchStop}.View.swift; no attention/escalation code |
| Relay channels | ⚠️ iMessage/SMS only | `apple/Relay/Core/MessageNormalizer.swift:74-81` (imessage/sms only); `MessageStore.swift:14,21` — no email/voicemail/calendar/contacts ingest; `MessageNormalizer.swift:65` "local contact resolution arrives with the contacts MVP" |
| Widgets | ⚠️ engineering content only (§68/§69 personal content missing) | `apple/Widgets/WidgetSnapshot.swift`, `LargeWidgetView.swift` show health/goals/incidents/agents/compute tiles — no "n need you" / "TODAY: actions/waiting/calendar" |
| Theme sync | ⚠️ Apple side exists, no cross-platform bridge | `apple/Shared/Sources/NerveDesign/Theme.swift:97-105` — `@AppStorage("nerveTheme")`; web uses `localStorage['nerve-theme']`; no bridge, web toggle can't pick "System", Apple apps expose no picker (`visual-lift-audit.md` §1) |

---

## 3. Gaps (ordered by impact)

### G1 — iPhone Personal Operations surfaces + `Today | Engineering | Ask` reorg — **L** (spec §66, §21–§30)

- **Missing:** the five personal views **Today / Inbox / People / Waiting / Briefing** and the reorg from 6 subsystem silos to the recommended top-level **Today | Engineering | Ask** ("rather than exposing every subsystem as a separate app-like silo", `prompt2.md:1984-1992`). The six engineering tabs would fold under an "Engineering" container.
- **Evidence:** `apple/iOS/App/Views/` has only the six engineering views; `NervePhoneApp.swift` `TabView` is exactly those six. Backend endpoints for the data *do* exist (`backend/api/endpoints/briefing.py`, `backend/waiting_on/`, messages/email/voicemail/calendar endpoints) — the gap is purely the Apple front-end + reorg + wire models.
- **Work:** (a) add wire models + fetch methods for attention/today, inbox (messages), people (contacts/conversations), waiting-on, briefing to `PhoneFetchService`/`WireModels`; (b) build the five views using existing `NerveDesign` components + honest-empty patterns; (c) restructure the tab bar into Today | Engineering | Ask (Engineering = container for the six existing tabs, e.g. secondary segmented control or nested scroll) keeping existing `phone.tab.*` accessibility ids for the harness; (d) wire the T003 harness launch args to the new tabs.
- **Acceptance:** audit harness can navigate each personal tab via `--nerve-tab`; each shows live data from `:8000` or an honest empty state; re-audit screenshots show Today/Inbox/People/Waiting/Briefing populated; engineering tabs still reachable; `xcodebuild` green; `swift test` green.
- **Note:** depends on G2 (data must exist to display) for Inbox/People content; Today/Waiting/Briefing can proceed now.

### G2 — Relay channel coverage: email, voicemail, calendar, contacts — **L** (spec §70 local-first, §21–§26)

- **Missing:** the Relay daemon ingests only iMessage/SMS. No email, voicemail, calendar, or contacts capture — so the phone/watch personal surfaces (G1) have no personal-channel data at all, and §70's local-first default ("messages, voicemail, personal email content, contacts remain on user-controlled systems") is only met for iMessage/SMS.
- **Evidence:** `apple/Relay/Core/MessageNormalizer.swift:74-81` handles only `imessage`/`sms`/`mms`; `MessageStore.swift:14,21` models conversations per handle for iMessage/SMS only; `MessageNormalizer.swift:65` defers contact resolution to "the contacts MVP".
- **Work:** extend `MessageNormalizer`/`MessageStore`/`RelayEngine` with channel adapters (email via IMAP/local Mail bundle snapshot, voicemail, calendar via EventKit, contacts via Contacts framework) behind the same normalizer interface; respect §70 (content stays local; only normalized metadata/attention factors uploaded); add `RelayTests` per channel.
- **Acceptance:** `RelayTests` cover each channel's normalize→store→outbox path; `swift test` green; a seeded test device produces attention items visible via `/api/attention` (or local cache) per channel; §70 auditability metadata retained (§71).
- **Size note:** calendar+contacts (EventKit/Contacts) is S–M each; email+voicemail M–L. Can be split: G2a calendar+contacts (unblocks People/Today), G2b email+voicemail (unblocks Inbox depth).

### G3 — Watch: attention-first surface (§27), "n NEED YOU" complication (§67), escalation (§28/§29/§60) + display verification — **M** (attention surface) / **S** (verification)

- **Missing:** the watch answers engineering questions (goals, incidents, approvals, stop) but never **"WHAT NEEDS ME?"** — no open-AttentionItems list, no `3 NEED YOU` / `✓ CLEAR` complication, no Done/Snooze/Reply/Call/Open-on-Phone actions, no escalation path (§60: watch is "the final escalation surface"; "Do not use Watch alerts for routine summaries").
- **Evidence:** `apple/Watch/Views/WatchHomeView.swift:30,94-101` renders snapshot tiles or honest "Not connected"; no attention items in `WatchDataStore`/`WatchHomeStateProvider`; no `WidgetKit` complication provider in `apple/Watch/` or `apple/Widgets/`; no local-notification escalation in either app.
- **Work:** (a) phone publishes open AttentionItems (+ linked waiting-on elapsed) into `group.nerv.center` (extend `publishSharedSnapshot`); (b) watch home reorders to attention-first with the §27 card layout + actions (snooze/done can POST via companion or phone deep-link); (c) §67 complication (`3 NEED YOU` / `✓ CLEAR`); (d) §60 escalation: phone triggers watch notification for escalated items only.
- **Display verification (S):** run phone+watch **paired** (XCUITest iPhone→Watch pairing or hardware) so `group.nerv.center` is genuinely shared; capture watch home showing live attention. Standalone watch-sim limitation documented in `docs/apple-data-lift-2026-10-18.md` (per-device App Group UUIDs; host-placed files not visible to the sandboxed sim app) — this is an environment limit, not a code defect.
- **Acceptance:** paired-sim (or hardware) screenshot shows watch rendering the live snapshot + attention; complication shows `n NEED YOU` when items open, `✓ CLEAR` when none; escalation produces a watch notification only for promoted items; re-audit watch dumps show `snapshot != nil`.

### G4 — Widgets: personal content (§68 Home Screen, §69 Morning Brief) — **M**

- **Missing:** widgets show engineering tiles only (health/goals/incidents/agents/compute, `apple/Widgets/LargeWidgetView.swift:52-124`). No `"n need you"` home-screen widget and no "TODAY: actions / waiting / calendar + next deadline" morning-brief widget.
- **Work:** add a personal `WidgetSnapshot` section (top-N open attention items with color-coded severity; next calendar item) sourced from the App Group (which already carries shared state — extend, don't fork); wire into `WidgetSnapshot`/providers; keep honest-empty text pattern.
- **Acceptance:** widget previews + device render show attention items + next item; empty state honest; `WidgetSnapshot` unit test covers personal section.

### G5 — Theme sync across platforms (C-4) — **M** (carried from `visual-lift-audit.md`)

- **Missing:** no bridge between web `localStorage['nerve-theme']` and Apple `@AppStorage("nerveTheme")` (`apple/Shared/Sources/NerveDesign/Theme.swift:105`); web toggle can't select "System"; Apple apps expose no theme picker at all, so phone/watch users are locked to `.system`.
- **Work:** (a) add a theme picker in the phone app (Settings/Overview header) writing `nerveTheme`; (b) sync via the existing relay/shared-state channel: phone uploads preference, relay/web reads it, and vice-versa (single source of truth = user preference stored in cache snapshot or a dedicated endpoint); (c) add "System" to the web toggle.
- **Acceptance:** picking a theme on web changes the phone within one refresh; picking on phone changes web; all three options (light/dark/system) selectable everywhere.

### G6 — Ask depth (prompt1 carry-over) — **M–L**

- **Missing:** `AskView` renders the Ask surface, but `/api/context/ask` returns a 202 ACK rather than an answered question (per `gaps1.md` item 4); no streaming answer, evidence citations UI, or provenance display in the app.
- **Work:** poll/stream the ack→result; render answer + `evidence` citations + provenance (§72-style) using `WireModels` ask result shape; add Ask e2e case to the harness.
- **Acceptance:** asking a question on the simulator shows an answered card with cited evidence; harness data-dump includes the ask result.

### G7 — Hygiene / verification gaps — **S** each

1. **Mixed uncommitted tree:** the data-lift, T003-06 harness (`apple/e2e/`, untracked), and prior-session Apple work (widgets/watch/distribution/fastlane/deploy docs) are all interleaved uncommitted; `NervePhoneApp.swift` depends on the untracked `apple/e2e/XCUITestSupport/NerveE2E.swift`, so the lift cannot be committed in isolation without including the harness. → Decide a commit scope with the user (recommended: one "apple: data-lift + e2e harness + T003-06 audit" commit, then separate commits for the prior-session distribution work).
2. **UI-layer test coverage:** `swift test` runs `NerveSharedTests`/`RelayTests`/`NerveAppsTests` (unit) but there is **no unit coverage for `PhoneDataStore` fetch/publish logic or the Views' empty/loading/error branches**; only the XCUITest audit harness exercises them manually. → Add `NerveAppsTests` cases for store fetch success/failure/empty and `publishSharedSnapshot` round-trip through `NerveJSON`.
3. **`swift test` bundle selection:** `gaps1.md` noted only 1 of 3 test bundles runs by default — re-verify after the lift (`swift test --package-path apple` ran 2/2 on 2026-10-18; confirm all packages/bundles run in CI).
4. **`docs/audit/` artifacts** (baseline screenshots + data-dumps) are useful evidence; keep them (committed) — they are the baseline for future visual regressions.

---

## 4. Suggested sequence

| Order | Item | Why | Size |
|---|---|---|---|
| 1 | G7.1 commit scope decision + G7.2 unit tests | stop the integrity bleed; lock in the verified lift | S |
| 2 | G1 (Today/Waiting/Briefing first, then Inbox/People as G2a lands) | the headline §66 gap; data for 3 of 5 surfaces already exists | L |
| 3 | G2a calendar+contacts → G2b email+voicemail | unblocks People/Today/Inbox depth; §70 local-first | L (split) |
| 4 | G3 watch attention + §67 + escalation + paired verification | completes the cross-platform story end-to-end | M+S |
| 5 | G4 widgets personal content | rides on G3's shared attention data | M |
| 6 | G5 theme sync | cross-platform polish | M |
| 7 | G6 Ask depth | needs backend ack→answer completion | M–L |

---

## 5. Explicitly verified NOT gaps (to prevent re-reporting)

- Phone empty-state root cause — **fixed and re-verified** (fetch-on-load + `--nerve-server` + App Group publish; §2 table above).
- `NerveJSON` ISO-8601 mismatch between phone publish and watch read — the publish uses the shared `NerveJSON`, so the existing watch read path decodes it; no fix needed.
- Watch `group.nerv.center` entitlement — present in `apple/Distribution/Generated/NerveWatch.entitlements` (both `group.nerv.center` and `group.com.celestium.nervecenter`) and mapped on the booted watch sim; the "Not connected" on the standalone watch sim is the per-device App Group isolation (G3 verification step), not a missing entitlement.
