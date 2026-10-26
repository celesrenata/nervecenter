import SwiftUI

import NerveApps
import NerveDesign

/// Overview tab — §57 "System health + agents + compute + incidents + goals".
public struct OverviewView: View {
    @ObservedObject public var store: PhoneDataStore
    @EnvironmentObject public var env: PhoneEnvironment

    public init(store: PhoneDataStore) {
        self.store = store
    }

    public var body: some View {
        VStack(spacing: Spacing.lg) {
            Header(store: store)
            ComputeSection()
            AgentsSection()
            IncidentsSection()
            GoalsSection()
        }
        .padding(.horizontal, Spacing.md)
        .padding(.top, Spacing.sm)
        .background(Color.phoneCanvas.ignoresSafeArea())
    }
}

// MARK: - §57 Header

extension OverviewView {
    struct Header: View {
        let store: PhoneDataStore
        @EnvironmentObject var env: PhoneEnvironment
        @EnvironmentObject var themeStore: ThemeStore
        @State private var showingServerConfig = false

        var body: some View {
            HStack {
                VStack(alignment: .leading, spacing: Spacing.xxs) {
                    Text("NERVECENTER")
                        .font(.headline.weight(.bold))
                    Button {
                        showingServerConfig = true
                    } label: {
                        Text(env.server.host)
                            .font(.subheadline)
                            .foregroundStyle(.secondary)
                    }
                    .accessibilityLabel("Server: \(env.server.host). Open server configuration.")
                    .accessibilityIdentifier("overview.serverConfig")
                }
                Spacer()
                Picker("Theme", selection: themeStore.preferenceBinding) {
                    Text("System").tag(ThemePreference.system)
                    Text("Light").tag(ThemePreference.light)
                    Text("Dark").tag(ThemePreference.dark)
                }
                .pickerStyle(.menu)
                .labelsHidden()
                .accessibilityIdentifier("overview.theme")
                HealthBadge(systemHealth: env.cacheState.systemHealth)
            }
            .sheet(isPresented: $showingServerConfig) {
                ServerConfigSheet(store: store)
                    .environmentObject(env)
            }
        }

        struct HealthBadge: View {
            let systemHealth: String
            var body: some View {
                Label(
                    systemHealth.uppercased(),
                    systemImage: systemHealthBadge(systemHealth)
                )
                .font(.caption2)
                .padding(.horizontal, Spacing.xs)
                .padding(.vertical, Spacing.xxs)
                .background(healthColor(systemHealth).opacity(0.15))
                .foregroundStyle(healthColor(systemHealth))
                .clipShape(Capsule())
            }

            func systemHealthBadge(_ h: String) -> String {
                switch h.lowercased() {
                case "healthy": return "checkmark.seal"
                case "degraded": return "exclamationmark.triangle"
                case "critical", "error": return "xmark.seal"
                default: return "questionmark.circle"
                }
            }

            func healthColor(_ h: String) -> Color {
                let l = h.lowercased()
                if l == "healthy" { return .green }
                if ["degraded", "warning"].contains(l) { return .orange }
                return .red
            }
        }
    }
}

// MARK: - Compute section

extension OverviewView {
    struct ComputeSection: View {
        @EnvironmentObject var env: PhoneEnvironment

        var body: some View {
            PhoneSection("Compute") {
                LazyHStack(spacing: Spacing.sm) {
                    ForEach(0 ..< env.cacheState.runningAgents, id: \.self) { _ in
                        VStack(spacing: Spacing.xs) {
                            Rectangle()
                                .fill(Color.green)
                                .frame(width: 48, height: 8)
                            Text("esnixi")
                                .font(.caption2)
                            Text("5090")
                                .font(.caption)
                        }
                    }
                    Spacer(minLength: Spacing.xs)
                    ForEach(0 ..< max(1, env.cacheState.activeIncidents), id: \.self) { _ in
                        VStack(spacing: Spacing.xs) {
                            Rectangle()
                                .fill(Color.red)
                                .frame(width: 48, height: 8)
                            Text("gremlin-1")
                                .font(.caption2)
                            Text("4070")
                                .font(.caption)
                        }
                    }
                }
            }
        }
    }
}

// MARK: - Agents section

extension OverviewView {
    struct AgentsSection: View {
        @EnvironmentObject var env: PhoneEnvironment

        var body: some View {
            PhoneSection("Agents") {
                Text("\(env.cacheState.runningAgents) running")
                    .font(.body)
                Text("\(max(0, 10 - env.cacheState.runningAgents)) waiting")
                    .font(.body)
            }
        }
    }
}

// MARK: - Incidents section

extension OverviewView {
    struct IncidentsSection: View {
        @EnvironmentObject var env: PhoneEnvironment

        var body: some View {
            PhoneSection("Incidents") {
                if env.cacheState.activeIncidents > 0 {
                    Text("\(env.cacheState.activeIncidents) active")
                        .font(.body)
                } else {
                    Text("No active incidents")
                        .font(.body)
                        .foregroundStyle(.secondary)
                }
            }
        }
    }
}

// MARK: - Goals section

extension OverviewView {
    struct GoalsSection: View {
        @EnvironmentObject var env: PhoneEnvironment

        var body: some View {
            PhoneSection("Goals") {
                let progress = env.cacheState.primaryGoalProgress
                if progress > 0 {
                    HStack {
                        Text("NerveCenter MVP")
                            .font(.body)
                        Spacer()
                        Text("\(Int(progress * 100))%")
                            .font(.body)
                            .monospacedDigit()
                    }
                    ProgressView(value: progress)
                        .progressViewStyle(CircularProgressViewStyle())
                } else {
                    Text("No primary goal")
                        .font(.body)
                        .foregroundStyle(.secondary)
                }
            }
        }
    }
}

#Preview {
    OverviewView(store: PhoneDataStore())
        .environmentObject(PhoneEnvironment(server: ServerConfig(host: "nerv.celestium.life")))
}
