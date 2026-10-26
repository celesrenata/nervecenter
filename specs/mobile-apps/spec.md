# Spec: iPhone & Watch App Targets

**Feature ID:** mobile-apps
**Created:** 2026-10-01
**Status:** Done (2026-10-26 — M201–M205 shipped; see tasks.md verification log)
**Branch target:** main (repo root: nervcenter)
**Upstream:** [prompot.md](../../prompot.md) §65–73 (Phone/Watch surfaces, §68 priority order, §69 compact state, §70 local connectivity, §71 pairing, §72 approval, §73 staleness), §48–51 (Web UI reference IA)
**Companions:** [specs/mobile-quality-audit/spec.md](../mobile-quality-audit/spec.md) (AC-M01–AC-M06 SDK contracts), [specs/sso-integration/spec.md](../sso-integration/spec.md) (AC-S04 pairing, AC-S05 approval binding, AC-S07 Apple RP)

## Problem

The shared Apple SDK compiles (AC009/AC-M06 gate) but **no application exists**:

- [apple/iOS](../../apple/iOS) contains only `.gitkeep` — there is no iPhone
  app target, no app entry point, no UI.
- No Watch directory beyond `apple/Watch/.gitkeep` — no watchOS app, no
  complication, no cached-state view.
- prompot §65–69 requires the phone to be the primary interaction surface
  (status, approvals, agents, goals, incidents) and the watch to remain useful
  from cached compact state with §68's priority order (urgent incident →
  approval → active incident → investigation → goal → general status).
- §71 pairing and §72 biometric-bound approval exist only as SDK primitives
  ([NerveAuth](../../apple/Shared/Sources/NerveAuth/CredentialStore.swift));
  no app consumes them.
- §70 local connectivity: manual server configuration must exist (Bonjour
  discovery optional).

## Goals

1. An iPhone app target exists in `apple/iOS/**` implementing §65–66: live
   status (systems, incidents, agents, goals), approval inbox with §72
   biometric-bound submission, agent activity, goals with progress, incidents
   with detail, and manual server configuration (§70).
2. A watchOS app target exists in `apple/Watch/**` implementing §68–69: a
   complication showing the §69 compact state in §68 priority order, and a
   cached-state view that stays useful offline with clear staleness marking
   (§73).
3. Both targets compile green via `xcodebuild` on the M5 Max (AC009-style
   gate) and pass unit tests; no real-device requirement (constraint inherited
   from nervecenter-deploy).
4. All UI follows the design-system tokens from
   [NerveDesign/Tokens.swift](../../apple/Shared/Sources/NerveDesign/Tokens.swift)
   and the web-quality rules (§5 semantic tokens, §8 responsive/adaptive,
   visible states, no fabricated data).

## Scope

**In scope:** `apple/iOS/**` (app target, sources, assets), `apple/Watch/**`
(app target, sources, complication), `apple/Package.swift` (target wiring),
`apple/Shared/**` additions only where a target needs a small shared helper
(e.g., LAContext implementation of `BiometricAuthenticator` lives in the iOS
target, not the shared SDK).

**Out of scope:** backend changes (S101), web changes (S102/W101–W103),
shared-SDK contract changes (M101–M105 own those), Widgets extension
(`apple/Widgets/.gitkeep` stays dormant), App Store distribution, real-device
testing.

## Constraints

- `xcodebuild -scheme NervePhone -destination 'generic/platform=iOS
  Simulator' build` and `xcodebuild -scheme NerveWatch -destination
  'generic/platform=watchOS Simulator' build` green on the M5 Max after every
  change.
- SwiftUI only; no third-party UI dependencies (SDK stays dependency-free).
- Data flows exclusively through `NerveAPI` client + `NervePersistence` cache
  + `NerveAuth` (SSO/pairing/Keychain/biometric); no fabricated demo data in
  shipped views.
- §71: challenge expiry ≤ 5 min; credential lands in Keychain.
- §72: approval submission requires biometric authorization bound to the exact
  `PlannedChange`; any modification invalidates approval.
- §73: stale information marked clearly (age, "stale" label, refresh affordance).
- §68: complication priority order is fixed: urgent incident → approval →
  active incident → active investigation → goal progress → general status.
- §70: manual server configuration always available; Bonjour discovery best-
  effort and optional.

## Acceptance Criteria

- AC-MP01: iPhone app target `NervePhone` builds green (`xcodebuild -scheme
  NervePhone … build` on M5 Max); app entry point + root navigation exist; 0
  compile warnings introduced by app sources.
- AC-MP02: iPhone status surfaces implement §65–66: systems health list,
  incidents (active/resolved with severity), agents (running/paused), goals
  (progress), each fed from `NerveAPI`/`NervePersistence` with loading, empty,
  error, and stale states; no hardcoded fixture rows.
- AC-MP03: iPhone approval inbox: pending approvals listed; submission runs
  the §72 biometric gate bound to the exact `PlannedChange` (AC-M04 SDK
  contract); denial and expiry paths visible in UI.
- AC-MP04: iPhone pairing + server config: pairing initiated against S101
  endpoints (AC-M03 SDK contract), credential stored in Keychain (AC-M02);
  manual server entry (§70) with connection test; break-glass absent on phone.
- AC-MP05: Watch app target `NerveWatch` builds green (`xcodebuild -scheme
  NerveWatch … build` on M5 Max); complication shows §69 compact state in §68
  priority order; cached-state view marks stale data clearly (§73) and stays
  usable offline.
- AC-MP06: Evidence logged in [docs/implementation-status.yaml](../../docs/implementation-status.yaml)
  and this file's tasks verification log: `xcodebuild` outputs, test results,
  explicit gaps (no device testing, no live SSO until S104 lands).

## Verification

Each AC maps to a task in [tasks.md](./tasks.md) (M201–M206) with exact
`xcodebuild` commands and evidence paths; simulator screenshots (if captured)
stored under `apple/e2e-artifacts/`; gaps stated explicitly.
