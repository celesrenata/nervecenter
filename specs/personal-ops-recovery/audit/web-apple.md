# Web + Apple Audit — Personal-Ops Recovery

**Transcribed** from worker-2's inline audit result (`audit-web-apple`) returned in the parallel
batch manifest.

| Field | Value |
| --- | --- |
| Batch id | `1ada822c-6ef8-426f-ae92-18a6294e7438` |
| Task name | `audit-web-apple` |
| Worker | worker-2 (read-only, project-reader) |
| Date | 2026-10-27 |
| Method | Read-only source audit. Worker-2 hit its read-only constraint, so it returned findings inline in the batch `manifest.json` (`tasks[].result`) instead of writing a file. This report faithfully transcribes that inline result; no files were modified by the worker. |

Scope covered by worker-2: web frontend (SettingsView, router, state, API client), apple/Relay
install/pairing, apple/iOS, apple/Watch, Widgets, Shared sources.

---

## Classification table

Worker-2's inline result includes an explicit "required classifications/gaps" block
(bullets 9–15 below); those classifications are transcribed verbatim. Additional rows use
worker-2's own statements only.

| Area | Classification | Evidence path:line | Data flow | Gap |
| --- | --- | --- | --- | --- |
| Web routing/navigation | Route exists but default is not `/now` | `web/src/App.tsx:41-48`; `web/src/components/Layout.tsx:37-47` | `/` renders `OverviewView`; `/now` is a separate route, not the default landing page | No `/connections` entry in navigation config |
| Connection Center | `Missing` | `web/src/components/Layout.tsx:37-47` | N/A (no route) | No `/connections` route/navigation entry |
| Guided Mac Relay install/pairing | `Missing` | `apple/Relay/INSTALL.md:10-118` | Manual install flow | Setup is manual (build, Full Disk Access, token or `relay.conf`, LaunchAgent) |
| `/now` default landing | `Implemented/disconnected` | `web/src/App.tsx:41-48` | `/` defaults to `OverviewView` | Route exists but is not the default landing page |
| Web mail-connector live UI | `Missing` | `web/src/views/SettingsView.tsx:150-192` | Settings renders documented instructions | No live connector flow in web |
| iOS attention items with actions | `Implemented/usable` | `apple/iOS/App/Views/TodayView.swift:38-42` | Today aggregates `/api/attention`, `/api/followups?status=OPEN`, `/api/waiting-on?status=open` | None stated |
| Watch attention/approval actions | `Missing` in-Watch | `apple/Shared/Sources/NerveApps/WatchApprovalPolicy.swift:24-27` | Approval deep-links to iPhone via `nerv://phone/approval` | Approval delegated to iPhone |
| Widget actions | `Missing` | `apple/Widgets/SmallWidgetView.swift:167-172` | Widgets are display-only | Stale indicators: `Implemented/usable` (same evidence) |
| Web incident flows | Implemented | `web/src/views/IncidentsView.tsx:7-12` | Incident creation and resolve/close wiring, plus legacy `/investigate` | None stated |
| Web Overview API handling | Implemented (with caveat) | `web/src/views/OverviewView.tsx:70-72` | Overview calls `/api/tasks` | A 500 is converted into an empty task result |
| Web auth/briefing/connector evidence | Implemented (previously gathered sources) | `web/src/auth/AuthContext.tsx:19-26`; `web/src/views/LoginView.tsx:80-124`; `web/src/views/BriefingView.tsx:64-66`; `web/src/views/SettingsView.tsx:150-192` | Auth context, login, briefing, settings UI | See mail-connector row |
| Relay forwarding endpoint | Implemented | `apple/Relay/INSTALL.md:3-6` | `POST /api/messages/ingest` | None stated |
| Mail connector discovery (Relay) | Implemented | `apple/Relay/CLI/main.swift:142-149`; `apple/Relay/Core/ChannelModels.swift:366-390` | Real `GET /api/mail-connectors` call | Unconfigured state represented as an empty list |
| Email ingestion (Relay test target) | Implemented (tested) | `apple/Tests/RelayTests/ChannelEngineTests.swift:135-137` | `/api/email/ingest` | None stated |
| iOS Today aggregation | Implemented/usable | `apple/iOS/App/Views/TodayView.swift:8-14`; `apple/iOS/App/Views/TodayView.swift:23-42`; `apple/iOS/App/Services/PersonalFetchService.swift:322-362` | Aggregates `/api/attention`, `/api/followups?status=OPEN`, `/api/waiting-on?status=open` | Reports honest empty/error states; rows render with actions |
| iOS Briefing | Implemented/usable | `apple/iOS/App/Services/PersonalFetchService.swift:290-296`; `apple/iOS/App/Views/BriefingView.swift:26-64` | Read-only aggregation using `/api/briefing?kind=` | None stated |
| iOS server setup | Manual | `apple/iOS/App/Views/ServerConfigSheet.swift:109-109`; `apple/iOS/App/Views/ServerConfigSheet.swift:148-197` | Manual server configuration; persists settings | Verifies with `GET /api/health` |
| Stale indicators (iOS/Watch/Widgets) | Implemented | `apple/iOS/App/Support/PhoneDesign.swift:69-71`; `apple/Watch/Views/WatchHomeView.swift:49-51`; `apple/Widgets/NerveCenterWidgetProvider.swift:24-27` | Explicit stale UI in each surface | None stated |
| Widget data integrity | Implemented | `apple/Widgets/WidgetSnapshot.swift:13-49` | Widget snapshots do not fabricate tile data | None stated |
| Widgets stale policy | Implemented | `apple/Widgets/NerveCenterWidgetProvider.swift:24-27`; `apple/Widgets/NerveCenterWidgetProvider.swift:42-44`; `apple/Widgets/SmallWidgetView.swift:167-172` | Shared 90-second stale policy; stale captions when applicable | None stated |

---

## Web frontend findings

Transcription of worker-2's web bullets.

- **Web routing/navigation:** `/` renders `OverviewView`, while `/now` is a separate route and
  is not the default landing page: [`web/src/App.tsx`](../../../web/src/App.tsx:41-48). The
  navigation configuration has no `/connections` entry:
  [`web/src/components/Layout.tsx`](../../../web/src/components/Layout.tsx:37-47).
- **Web API coverage:** Incident creation and resolve/close wiring, plus legacy `/investigate`,
  are present: [`web/src/views/IncidentsView.tsx`](../../../web/src/views/IncidentsView.tsx:7-12).
  Overview calls `/api/tasks`, but a 500 is converted into an empty task result:
  [`web/src/views/OverviewView.tsx`](../../../web/src/views/OverviewView.tsx:70-72). Auth,
  briefing, and connector UI evidence is in the previously gathered sources
  [`web/src/auth/AuthContext.tsx`](../../../web/src/auth/AuthContext.tsx:19-26),
  [`web/src/views/LoginView.tsx`](../../../web/src/views/LoginView.tsx:80-124),
  [`web/src/views/BriefingView.tsx`](../../../web/src/views/BriefingView.tsx:64-66), and
  [`web/src/views/SettingsView.tsx`](../../../web/src/views/SettingsView.tsx:150-192).

## Apple Relay findings

- **Mac Relay:** Installation remains manual: build, Full Disk Access, token or `relay.conf`,
  and LaunchAgent setup are required:
  [`apple/Relay/INSTALL.md`](../../../apple/Relay/INSTALL.md:10-118). Relay forwarding uses
  `POST /api/messages/ingest`: [`apple/Relay/INSTALL.md`](../../../apple/Relay/INSTALL.md:3-6).
  Mail connector discovery is a real `GET /api/mail-connectors` call, with an unconfigured state
  represented as an empty list:
  [`apple/Relay/CLI/main.swift`](../../../apple/Relay/CLI/main.swift:142-149),
  [`apple/Relay/Core/ChannelModels.swift`](../../../apple/Relay/Core/ChannelModels.swift:366-390).
  Email ingestion is tested at `/api/email/ingest`:
  [`apple/Tests/RelayTests/ChannelEngineTests.swift`](../../../apple/Tests/RelayTests/ChannelEngineTests.swift:135-137).

## iOS/Watch/Widgets findings

- **iOS:** Today aggregates `/api/attention`, `/api/followups?status=OPEN`, and
  `/api/waiting-on?status=open`, reports honest empty/error states, and renders rows with
  actions: [`apple/iOS/App/Views/TodayView.swift`](../../../apple/iOS/App/Views/TodayView.swift:8-14),
  [`apple/iOS/App/Views/TodayView.swift`](../../../apple/iOS/App/Views/TodayView.swift:23-42),
  [`apple/iOS/App/Services/PersonalFetchService.swift`](../../../apple/iOS/App/Services/PersonalFetchService.swift:322-362).
  Briefing is a read-only aggregation using `/api/briefing?kind=`:
  [`apple/iOS/App/Services/PersonalFetchService.swift`](../../../apple/iOS/App/Services/PersonalFetchService.swift:290-296),
  [`apple/iOS/App/Views/BriefingView.swift`](../../../apple/iOS/App/Views/BriefingView.swift:26-64).
- **Watch:** Watch approval is not performed in-Watch; it deep-links to the iPhone using
  `nerv://phone/approval`:
  [`apple/Shared/Sources/NerveApps/WatchApprovalPolicy.swift`](../../../apple/Shared/Sources/NerveApps/WatchApprovalPolicy.swift:24-27),
  [`apple/Shared/Sources/NerveApps/WatchApprovalPolicy.swift`](../../../apple/Shared/Sources/NerveApps/WatchApprovalPolicy.swift:84-86).
- **Widgets:** Widgets are display-only, with no actions. They use a shared 90-second stale
  policy and show stale captions when applicable:
  [`apple/Widgets/NerveCenterWidgetProvider.swift`](../../../apple/Widgets/NerveCenterWidgetProvider.swift:24-27),
  [`apple/Widgets/NerveCenterWidgetProvider.swift`](../../../apple/Widgets/NerveCenterWidgetProvider.swift:42-44),
  [`apple/Widgets/SmallWidgetView.swift`](../../../apple/Widgets/SmallWidgetView.swift:167-172).
  Widget snapshots do not fabricate tile data:
  [`apple/Widgets/WidgetSnapshot.swift`](../../../apple/Widgets/WidgetSnapshot.swift:13-49).
- **Stale indicator answer:** Yes—iOS, Watch, and Widgets each expose explicit stale UI:
  [`apple/iOS/App/Support/PhoneDesign.swift`](../../../apple/iOS/App/Support/PhoneDesign.swift:69-71),
  [`apple/Watch/Views/WatchHomeView.swift`](../../../apple/Watch/Views/WatchHomeView.swift:49-51),
  [`apple/Widgets/NerveCenterWidgetProvider.swift`](../../../apple/Widgets/NerveCenterWidgetProvider.swift:24-27).
- **Server setup:** iOS server configuration is manual, persists settings, and verifies with
  `GET /api/health`:
  [`apple/iOS/App/Views/ServerConfigSheet.swift`](../../../apple/iOS/App/Views/ServerConfigSheet.swift:109-109),
  [`apple/iOS/App/Views/ServerConfigSheet.swift`](../../../apple/iOS/App/Views/ServerConfigSheet.swift:148-197).

---

## Gap list for Connection Center + relay install + Today lift

Worker-2's inline result ends with an explicit "Required classifications/gaps for the final
report" block, transcribed verbatim:

- Connection Center: `Missing`—no `/connections` route/navigation entry:
  [`web/src/components/Layout.tsx`](../../../web/src/components/Layout.tsx:37-47).
- Guided Mac Relay install/pairing: `Missing`—setup is manual:
  [`apple/Relay/INSTALL.md`](../../../apple/Relay/INSTALL.md:10-118).
- `/now` default landing: `Implemented/disconnected`—route exists, but `/` defaults to
  `OverviewView`: [`web/src/App.tsx`](../../../web/src/App.tsx:41-48).
- Web mail-connector live UI: `Missing`—Settings contains documented instructions rather than a
  live connector flow:
  [`web/src/views/SettingsView.tsx`](../../../web/src/views/SettingsView.tsx:150-192).
- iOS attention items with actions: `Implemented/usable`:
  [`apple/iOS/App/Views/TodayView.swift`](../../../apple/iOS/App/Views/TodayView.swift:38-42).
- Watch attention/approval actions: `Missing` in-Watch; approval is delegated to iPhone:
  [`apple/Shared/Sources/NerveApps/WatchApprovalPolicy.swift`](../../../apple/Shared/Sources/NerveApps/WatchApprovalPolicy.swift:24-27).
- Widget actions: `Missing`; stale indicators: `Implemented/usable`:
  [`apple/Widgets/SmallWidgetView.swift`](../../../apple/Widgets/SmallWidgetView.swift:167-172).

Worker-2's result notes that the output file was still to be created by an implementation-capable
worker — this report satisfies that note.
