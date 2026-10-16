# NerveCenter — self-host signing credentials

This directory is **gitignored**. It holds the ad-hoc signing material needed
to archive/export NervePhone (iOS) and NerveWatch (watchOS) **without a paid
Apple Developer account** — i.e. with a personal / free team.

## What to place here

| File | What it is | How to get it |
|------|-----------|--------------|
| `nervecenter.mobileprovision` | The ad-hoc (or "Development") provisioning profile for `com.celesrenata.NervePhone` (and the `com.celesrenata.nervewatch` bundle). | Register the App ID(s) in your Apple ID → developer section, then create a profile with **Distribution** type (ad-hoc) using your personal team. Download it. For the watchOS app you need a profile for `com.celesrenata.nervewatch` too — place it as `nervecenter-watch.mobileprovision`. |
| `nervecenter.p12` | Your **Distribution** (or "App Sign") private key + cert exported from the Keychain. | In Xcode → Settings → Account → keychain, click *Download*. You'll be prompted for a password; remember it. Save the file here. |

## Registering the apps / bundle IDs

Because there is no App Store submission, you still need to register the two
App IDs so Xcode can sign them:

- `com.celesrenata.NervePhone`
- `com.celesrenata.NervePhone.widgets` (widget extension)
- `com.celesrenata.nervewatch`
- `com.celesrenata.nervewatch.widgets` (widget extension)

Register each under **App IDs** with **App** capability (the widget extension
uses `NSExtension`). For **App Groups**, register **both** `group.nerv.center`
and `group.com.celestium.nervecenter` — the entitlements grant both so the
phone/watch/widget sources (which currently reference different group IDs)
share one container.

## Using with the build

`export_app.sh` and the `Fastfile` read `NERVE_TEAM_ID` from `../fastlane/.env`
(or the environment). Set it to your **personal team ID** (a 10-char string
like `ABCDEFGH12`) shown on the "Manage App IDs" page.

## Security

Never commit anything in this directory. The `.gitignore` entry
`apple/Distribution/credentials/*` (except `README.md`) keeps it out of the
repo. Rotate the key if it is ever exposed.
