import SwiftUI
import Foundation

import NerveAPI
import NerveApps
import NerveDesign

// MARK: - iPhone app entry point

/// The `@main` app for the `NervePhone` target.
///
/// Wires:
/// - `PhoneEnvironment` (service container: cache, credentials, shared state,
///   biometric gate)
/// - `PhoneDataStore` (per-tab data; hydrated from cache at bootstrap, then
///   refreshed from the live API)
/// - The six §56 tabs (Overview, Goals, Incidents, Agents, Systems, Ask)
@main
struct NervePhoneApp: App {
    /// §70 manual server configuration. Defaults to the live NerveCenter
    /// server; overridable at launch with `--nerve-server=<scheme://host:port>`
    /// (T003-06 harness / local backend) and persistently in production.
    @State private var server = ServerConfig(host: "nerv.celestium.life")
    @StateObject private var environment = PhoneEnvironment(
        server: ServerConfig(host: "nerv.celestium.life")
    )
    @StateObject private var store = PhoneDataStore()
    @StateObject private var themeStore = ThemeStore()
    @Environment(\.colorScheme) private var colorScheme

    var body: some Scene {
        WindowGroup {
            AppShell()
                .environmentObject(environment)
                .environmentObject(themeStore)
                .theme(themeStore.resolvedTheme(for: colorScheme))
                .onAppear {
                    if let override = launchServerOverride {
                        server = override
                        environment.server = override
                    }
                }
                .task {
                    await bootstrap()
                }
        }
    }

    /// Load persisted credentials, hydrate the cache snapshot into the store,
    /// then fetch live data from the configured server (C-3). The fetch
    /// publishes a fresh snapshot into the shared App Group so the Watch and
    /// Widgets can render real data (spec §69/§73).
    @MainActor
    private func bootstrap() async {
        await environment.bootstrap()
        store.apply(environment.cacheState)
        await environment.fetchInto(store)
        store.apply(environment.cacheState)
    }

    /// Parse `--nerve-server=<scheme://host:port>` from launch arguments, if
    /// present. Returns `nil` when the argument is absent or unparseable.
    private var launchServerOverride: ServerConfig? {
        let args = ProcessInfo.processInfo.arguments
        guard let idx = args.firstIndex(where: { $0.hasPrefix("--nerve-server=") }) else {
            return nil
        }
        let value = String(args[idx].dropFirst("--nerve-server=".count))
        return ServerConfig.parse(value)
    }
}

// MARK: - App shell with six tabs

/// The app shell hosts the tab view that renders the six §56 tabs.
///
/// Testability seam (T003-06 / AC7): the selected tab is bound and can be
/// set at launch with `--nerve-tab=<Overview|Goals|Incidents|Agents|Systems|Ask>`
/// so the visual-audit / UI-test harness can navigate deterministically in a
/// headless simulator. Each tab also carries a stable
/// `accessibilityIdentifier` (`phone.tab.<name>`) for UI-test + AX assertions.
struct AppShell: View {
    @EnvironmentObject private var environment: PhoneEnvironment
    @StateObject private var store = PhoneDataStore()
    @State private var selectedTab: PhoneTab = PhoneDebug.tab ?? .overview

    var body: some View {
        TabView(selection: $selectedTab) {
            OverviewView(store: store)
                .tabItem {
                    Label("Overview", systemImage: PhoneTab.overview.symbolName)
                }
                .tag(PhoneTab.overview)
                .accessibilityIdentifier("phone.tab.overview")

            GoalsView(store: store)
                .tabItem {
                    Label("Goals", systemImage: PhoneTab.goals.symbolName)
                }
                .tag(PhoneTab.goals)
                .accessibilityIdentifier("phone.tab.goals")

            IncidentsView(store: store)
                .tabItem {
                    Label("Incidents", systemImage: PhoneTab.incidents.symbolName)
                }
                .tag(PhoneTab.incidents)
                .accessibilityIdentifier("phone.tab.incidents")

            AgentsView(store: store)
                .tabItem {
                    Label("Agents", systemImage: PhoneTab.agents.symbolName)
                }
                .tag(PhoneTab.agents)
                .accessibilityIdentifier("phone.tab.agents")

            SystemsView(store: store)
                .tabItem {
                    Label("Systems", systemImage: PhoneTab.systems.symbolName)
                }
                .tag(PhoneTab.systems)
                .accessibilityIdentifier("phone.tab.systems")

            AskView(store: store)
                .tabItem {
                    Label("Ask", systemImage: PhoneTab.ask.symbolName)
                }
                .tag(PhoneTab.ask)
                .accessibilityIdentifier("phone.tab.ask")
        }
        .tint(.blue)
        .accessibilityIdentifier("phone.shell")
        .task {
            // Data-dump seam: when launched with --nerve-dump, serialize the
            // currently-rendered store to Documents/nerve-audit-dump.json so a
            // headless auditor can corroborate the screenshots.
            guard PhoneDebug.dumpEnabled else { return }
            try? await Task.sleep(nanoseconds: 700_000_000) // let first frame settle
            try? await store.writeAuditDump()
        }
    }
}

// MARK: - Testability / launch-arg plumbing (T003-06)

/// Parses the harness launch arguments. Kept in the app target (not the shared
/// SDK) so it never ships into production surfaces.
enum PhoneDebug {
    private static var args: [String] { ProcessInfo.processInfo.arguments }

    /// The tab requested via `--nerve-tab=<name>` (nil = default Overview).
    static var tab: PhoneTab? {
        guard let flag = args.first(where: { $0.hasPrefix("--nerve-tab=") }) else { return nil }
        let name = String(flag.dropFirst("--nerve-tab=".count))
        return PhoneTab(rawValue: name.capitalized)
    }

    /// Whether the data-dump seam is requested via `--nerve-dump[=1]`.
    static var dumpEnabled: Bool {
        args.contains { $0.hasPrefix("--nerve-dump") }
    }
}

// MARK: - Data dump

extension PhoneDataStore {
    /// Serialize the rendered store state to `Documents/nerve-audit-dump.json`.
    ///
    /// Truthful by construction: it reports exactly the counts, load states and
    /// rows the store currently holds (no fabrication). Used by the headless
    /// visual-audit harness to corroborate screenshots.
    @MainActor
    public func writeAuditDump() throws {
        let url = FileManager.default
            .urls(for: .documentDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("nerve-audit-dump.json")

        let ts = ISO8601DateFormatter().string(from: updatedAt)
        let dump: [String: Any] = [
            "systemHealth": systemHealth,
            "activeIncidents": activeIncidents,
            "runningAgents": runningAgents,
            "pendingApprovals": pendingApprovals,
            "primaryGoalProgress": primaryGoalProgress as Any,
            "updatedAt": ts,
            "goalsState": String(describing: goalsState),
            "incidentsState": String(describing: incidentsState),
            "agentsState": String(describing: agentsState),
            "systemsState": String(describing: systemsState),
            "askState": String(describing: askState),
            "goalCount": goals.count,
            "incidentCount": incidents.count,
            "agentCount": agents.count,
            "systemCount": systems.count,
            "askAnswerCount": askAnswers.count,
            "agents": agents.map { ["id": $0.id, "name": $0.name, "status": $0.status, "currentTask": $0.currentTask as Any] },
            "systems": systems.map { ["id": $0.id, "name": $0.name, "health": $0.health] },
        ]
        let data = try JSONSerialization.data(withJSONObject: dump, options: [.prettyPrinted, .sortedKeys])
        try data.write(to: url, options: .atomic)
    }
}
