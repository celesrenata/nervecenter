import SwiftUI
import Foundation

import NerveApps
import NerveDesign

// MARK: - Watch app entry point (spec §63–§68)

/// The `@main` app for the `NerveWatch` target.
///
/// Watch apps use a single NavigationStack (no TabView) — the §63 goal
/// flows are reached from the home screen's goal navigation:
///
/// ```text
/// glance          → WatchHomeView (root)
/// alert           → WatchAlertView
/// query           → WatchQueryView
/// acknowledge     → WatchAcknowledgeView
/// approve/reject  → WatchApprovalsView (one inbox for both goals)
/// stop            → WatchStopView (explicit §67 confirmation surface)
/// ```
///
/// Data flows from the App Group container (`WatchAppGroup.identifier`,
/// files `cached-state.json` + `approvals.json`) through
/// ``WatchDataStore``. With no snapshot the app renders an honest
/// "Not connected" state — it never fabricates counts.
@main
struct NerveWatchApp: App {
    @StateObject private var store = WatchDataStore()
    @StateObject private var themeStore = ThemeStore()
    @Environment(\.colorScheme) private var colorScheme
    @State private var path = NavigationPath()

    var body: some Scene {
        WindowGroup {
            NavigationStack(path: $path) {
                WatchHomeView(store: store)
                    .accessibilityIdentifier("watch.home")
                    .navigationDestination(for: WatchDestination.self) { destination in
                        destinationView(destination)
                    }
            }
            .environmentObject(store)
            .environmentObject(themeStore)
            .theme(themeStore.resolvedTheme(for: colorScheme))
            .task {
                // Testability seam (T003-06): when launched with
                // --nerve-view=<alert|query|acknowledge|approvals|stop>, push
                // that screen so a headless simulator audit can capture it
                // without taps.
                if let view = WatchDebug.view {
                    path.append(view)
                }
                if WatchDebug.dumpEnabled {
                    try? await Task.sleep(nanoseconds: 700_000_000)
                    try? await store.writeAuditDump()
                }
            }
        }
    }

    private func destinationView(_ destination: WatchDestination) -> AnyView {
        switch destination {
        case .alert:
            return AnyView(WatchAlertView(store: store).accessibilityIdentifier("watch.alert"))
        case .query:
            return AnyView(WatchQueryView(store: store).accessibilityIdentifier("watch.query"))
        case .acknowledge:
            return AnyView(WatchAcknowledgeView(store: store).accessibilityIdentifier("watch.acknowledge"))
        case .approvals:
            return AnyView(WatchApprovalsView(store: store).accessibilityIdentifier("watch.approvals"))
        case .stop:
            return AnyView(WatchStopView(store: store).accessibilityIdentifier("watch.stop"))
        }
    }
}

// MARK: - Testability / launch-arg plumbing (T003-06)

/// Parses the harness launch arguments. Kept in the app target (not the shared
/// SDK) so it never ships into production surfaces.
enum WatchDebug {
    private static var args: [String] { ProcessInfo.processInfo.arguments }

    private static let destinationMap: [String: WatchDestination] = [
        "alert": .alert, "incidents": .alert,
        "query": .query,
        "acknowledge": .acknowledge, "ack": .acknowledge,
        "approvals": .approvals, "approve": .approvals,
        "stop": .stop,
    ]

    /// The screen requested via `--nerve-view=<name>` (nil = home).
    static var view: WatchDestination? {
        guard let flag = args.first(where: { $0.hasPrefix("--nerve-view=") }) else { return nil }
        let name = String(flag.dropFirst("--nerve-view=".count)).lowercased()
        return destinationMap[name]
    }

    /// Whether the data-dump seam is requested via `--nerve-dump[=1]`.
    static var dumpEnabled: Bool {
        args.contains { $0.hasPrefix("--nerve-dump") }
    }
}

// MARK: - Data dump

extension WatchDataStore {
    /// Serialize the rendered store state to `Documents/nerve-audit-dump.json`.
    ///
    /// Truthful by construction: it reports the snapshot's presence, staleness,
    /// approval count and home state — no fabrication. Used by the headless
    /// visual-audit harness to corroborate screenshots.
    @MainActor
    public func writeAuditDump() throws {
        let url = FileManager.default
            .urls(for: .documentDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("nerve-audit-dump.json")

        var dump: [String: Any] = [:]
        if let snap = snapshot {
            dump["snapshot"] = [
                "systemHealth": snap.systemHealth,
                "activeIncidents": snap.activeIncidents,
                "runningAgents": snap.runningAgents,
                "pendingApprovals": snap.pendingApprovals,
            ]
        } else {
            dump["snapshot"] = "nil (Not connected)"
        }
        dump["staleness"] = staleness.map { ["isStale": $0.isStale] } as Any
        dump["approvalCount"] = approvals.count
        dump["homeState"] = homeState.map { String(describing: $0) } as Any
        let data = try JSONSerialization.data(withJSONObject: dump, options: [.prettyPrinted, .sortedKeys])
        try data.write(to: url, options: .atomic)
    }
}

// MARK: - §68 Complication support

/// Entry point for feeding the Watch complications (spec §68).
///
/// The §68 ladder decision is computed *outside* SwiftUI so it is testable
/// and reusable:
///
/// ```swift
/// let inputs = store.complicationInputs   // nil when offline
/// let item = inputs.map { ComplicationLadder.render(for: $0) }
/// ```
///
/// No live-complication entry point is implemented in this target:
/// `AccessoryWidgetBundle` / `LiveComplicationController` require the
/// watchOS SDK and a separate widget-extension target. Per the task
/// contract a live entry is not required when the API is unavailable; the
/// §68 decision logic is fully available above for the extension target to
/// consume. (Keeping `WidgetKit`/`SwiftUI` accessory APIs out of this file
/// also keeps the target building on the macOS host.)
public enum NerveWatchComplication {
    /// Render the single §68 rung the current snapshot supports, or `nil`
    /// when no snapshot exists (the complication then shows its
    /// unavailable configuration — an honest "no data", not a fake 0).
    public static func render(from store: WatchDataStore) -> ComplicationItem? {
        store.complicationInputs.map { ComplicationLadder.render(for: $0) }
    }
}
