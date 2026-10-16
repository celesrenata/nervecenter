# TestFlight runbook (FrameShift team `FH987LQGX9`)

Self-host distribution path for **Nerve Center** on TestFlight, using the
paid **FrameShift** Apple Developer Program account (`celes@frameshift.net`).

> The free personal team (`NQV7T36W69`) cannot upload to App Store Connect.
> All TestFlight work must be signed with team `FH987LQGX9`.

## Prerequisites (one-time, already done)

1. FrameShift Apple ID logged into **Xcode → Settings → Accounts**
   (the GUI holds the session; the `xcodebuild` CLI may still report
   "No Account for Team FH987LQGX9" — use the GUI for archive/upload).
2. **Apple Distribution** certificate created (Accounts → Manage
   Certificates → + → Apple Distribution, App Store).
3. App Store Connect app record **Nerve Center**
   (`com.celesrenata.NervePhone`) created at appstoreconnect.apple.com.

## Per-build flow

1. **Verify widget dependencies are in the xcodegen spec** — the widget
   target needs `dependencies: [package: NerveCenter]` or it fails to
   compile (`Unable to resolve module dependency: NerveApps`). See
   `apple/Distribution/xcodegen/NervePhone.xcodegen.yml`.
2. **Xcode GUI** (not CLI): open `apple/Distribution/Generated/NervePhone.xcodeproj`
   - Target `NervePhone` + `NerveCenterWidgetExtension` → Signing &
     Capabilities → **Team: FrameShift**, Automatic signing.
   - The entitlements carry both app groups
     (`group.nerv.center`, `group.com.celestium.nervecenter`) so widget /
     watch state sharing works — do not use the stripped `/tmp/nc-personal`
     personal entitlements for this path.
3. **Product → Archive** (scheme destination: Any iOS Device).
4. Organizer → **Distribute App → App Store Connect → Upload**
   (signing Automatic, export method App Store, "Don't upload metadata").
5. App Store Connect → **My Apps → Nerve Center → App Store** → wait for
   the build to finish processing (10–30 min) → click the build number →
   **Save** (attach).
6. **TestFlight tab → Internal** → (+) build → add
   `celes@frameshift.net` as internal tester → Send.
7. iPhone: TestFlight app → sign in with the same Apple ID → accept →
   Install.

## Notes / gotchas

- Internal testers must be members of the paid team (the admin account is).
- `xcodebuild -exportArchive` with `upload` has historically failed with
  "No profiles … were found" on this machine; the Organizer GUI upload works.
- The web Downloads page (`/downloads/NervePhone.ipa`) serves a separate
  **development** build for sideload; it expires ~7 days out and needs a
  re-archive. TestFlight builds last **90 days** and need no profiles.
- App ID / app groups live under App Store Connect → Users and Access →
  App Information.
