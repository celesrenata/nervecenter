# Install the NerveCenter iPhone app with the Apple Health bridge

Status: the app compiles for iOS (`xcodebuild ... -destination 'generic/platform=iOS'`
reports BUILD SUCCEEDED, unsigned). A signed build has **not** been made and the app
is **not installed** on any phone. The steps below are for the owner.

## 0. One-time Apple account step (blocks signing today)

The last signed build attempt failed with two errors:

1. `PLA Update available ... agree to the latest Program License Agreement`.
2. The team provisioning profile for `com.celesrenata.NervePhone` has no HealthKit capability.

Fix: sign in at <https://developer.apple.com/account>, click **Agreements** (or the yellow banner),
and accept the updated Program License Agreement. Xcode then regenerates the profile with HealthKit
when you build with automatic signing (step 2). If Xcode says the HealthKit background-delivery
entitlement is not available for your team, remove the line
`com.apple.developer.healthkit.background-delivery: true` from
`apple/Distribution/xcodegen/NervePhone.xcodegen.yml`, run step 1 again and rebuild. Sync then
happens when you open the app instead of in the background.

## 1. Regenerate the Xcode project (only if you changed the xcodegen spec)

```bash
brew install xcodegen
xcodegen generate --spec apple/Distribution/xcodegen/NervePhone.xcodegen.yml \
  --project apple/Distribution/Generated
```

## 2. Build and install from Xcode

1. Plug the iPhone in with a cable, unlock it, tap **Trust This Computer** and enter the passcode.
2. On the iPhone: **Settings > Privacy & Security > Developer Mode > On**, then restart when asked.
3. On the Mac open `apple/Distribution/Generated/NervePhone.xcodeproj` in Xcode.
4. In the left file list click the blue **NervePhone** project, then the **NervePhone** target
   (not the widget extension).
5. Open the **Signing & Capabilities** tab. Tick **Automatically manage signing**. Set **Team** to
   your personal team (`NQV7T36W69`, Apple Development: celes@frameshift.net).
6. Repeat steps 4 and 5 for the **NerveCenterWidgetExtension** target.
7. Check that **HealthKit** is listed under the NervePhone capabilities. If it is not, click
   **+ Capability** and double-click **HealthKit**.
8. In the toolbar device menu (next to the Run button) choose your iPhone.
9. Click **Product > Run** (or press Cmd+R). Wait for "Running NervePhone".
10. First launch only: on the iPhone open **Settings > General > VPN & Device Management**, tap your
    Apple Development profile, tap **Trust**, then open the app again.

Apple Development builds expire after 7 days on a free team (1 year on a paid team); repeat step 9
to renew.

## 3. Create a device token for the phone (on the Mac)

The phone uses its own pairing token. The token is shown once, so do not paste it into chats or logs.

```bash
kubectl -n nervecenter port-forward svc/api 8000:8000 &
curl -s -X POST localhost:8000/api/relay/pair \
  -H 'content-type: application/json' -d '{"device_name":"iPhone Health"}'
kill %1
```

Copy `device_id` (starts with `relay-`) and `token` from the reply. Move them to the phone with
Universal Clipboard (copy on the Mac, paste on the phone) or AirDrop a note, then delete the note.

## 4. Pair, allow Health access, sync (on the iPhone)

1. Open **Nerve Center**. Tap the heart icon at the top right.
2. Under **Pairing** paste the device id and the token, then tap **Save pairing**. The token goes
   into the Keychain and the field clears.
3. Under **Apple Health** tap **Allow Health access**. In the Health sheet tap **Turn On All**
   (or switch on Body Mass, Sleep, Steps, Heart Rate, Resting Heart Rate, Heart Rate Variability,
   Active Energy) and tap **Allow**. The app only reads; nothing is written to Health.
4. Tap **Sync now**. **Last sync** shows the time and counts. The first sync reads the last 30 days.
5. Check the server side: open <https://nervecenter.celestium.life/health> (Physical Health).

The server address the phone uses is the one in the app's server settings
(default `nerv.celestium.life`). It must resolve to the same cluster as
`nervecenter.celestium.life`; if **Sync now** shows "Could not reach the server", change the host
there to `nervecenter.celestium.life`.

## Troubleshooting

| Message | Meaning |
| --- | --- |
| The server rejected this device token | Wrong device id or token. Mint a new one (step 3) and save again. |
| The server answered 413 / 429 | Body too large (limit 2 MiB) or more than 5 requests/s. Wait and sync again; replays are safe. |
| Health access was not granted | Open **Settings > Health > Data Access & Devices > Nerve Center** and switch categories on. |
