import SwiftUI

import NerveApps
import NerveDesign

// MARK: - §64 Watch home screen (the `glance` goal)

/// The §64 home screen: calm `.healthy` or actionable `.problem`.
///
/// Renders only what the §73 snapshot actually contains; when no snapshot
/// exists it shows an honest "Not connected" state (no fabricated data).
public struct WatchHomeView: View {
    @ObservedObject var store: WatchDataStore

    public init(store: WatchDataStore) {
        self.store = store
    }

    public var body: some View {
        VStack(spacing: 6) {
            WatchWordmark()

            if let home = store.homeState {
                if case .healthy(let summary) = home {
                    healthyBlock(summary)
                } else if case .problem(let summary) = home {
                    problemBlock(summary)
                }
            } else {
                notConnectedBlock
            }

            if let report = store.staleness, report.isStale {
                WatchStaleBadge(report: report)
            }

            goalNav
        }
        .padding(6)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)
        .background(Color.watchCanvas.ignoresSafeArea())
        .navigationTitle("")
    }

    // MARK: Healthy (calm) layout — spec §64

    private func healthyBlock(_ s: WatchHomeState.HealthySummary) -> some View {
        VStack(spacing: 6) {
            Label("Healthy", systemImage: "circle.fill")
                .font(.title3.weight(.semibold))
                .foregroundStyle(Color.nerve(HealthColor.healthy.color))
                .accessibilityLabel("System healthy")

            VStack(spacing: 3) {
                WatchStatRow(label: "Agents", value: "\(s.agents)")
                WatchStatRow(label: "Incidents", value: "\(s.incidents)")
                WatchStatRow(label: "Approvals", value: "\(s.approvals)")
                WatchStatRow(label: s.leadingComputeNode, value: "\(s.leadingComputePercent)%")
            }
        }
    }

    // MARK: Problem layout — spec §64

    private func problemBlock(_ s: WatchHomeState.ProblemSummary) -> some View {
        VStack(spacing: 4) {
            Label(
                "\(s.incidentCount) \(s.incidentCount == 1 ? "INCIDENT" : "INCIDENTS")",
                systemImage: "exclamationmark.circle.fill"
            )
            .font(.title3.weight(.semibold))
            .foregroundStyle(Color.nerve(HealthColor.critical.color))
            .accessibilityLabel("\(s.incidentCount) active incidents")

            Text(s.goalLabel)
                .font(.headline)
            Text(s.topEntityLabel)
                .font(.subheadline)
                .foregroundStyle(.secondary)
            Text(s.ageLabel)
                .font(.caption)
                .foregroundStyle(.secondary)

            NavigationLink(value: WatchDestination.alert) {
                Text("Details")
                    .font(.caption.weight(.semibold))
            }
            .buttonStyle(.borderedProminent)
            .tint(Color.nerve(HealthColor.critical.color))
            .accessibilityIdentifier("watch.nav.details")
        }
    }

    // MARK: Honest "not connected" state (no snapshot — never invent data)

    private var notConnectedBlock: some View {
        VStack(spacing: 6) {
            Image(systemName: "wifi.slash")
                .font(.title2)
                .foregroundStyle(.secondary)
            Text("Not connected")
                .font(.headline)
            Text("No cached state from the iPhone yet. Open the Nerve app on your phone, then refresh.")
                .font(.caption)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
            Button("Refresh") {
                store.refresh()
            }
            .buttonStyle(.bordered)
            .font(.caption)
        }
        .accessibilityElement(children: .combine)
    }

    // MARK: §63 goal navigation

    /// The five off-home goals as a compact row. The two home-anchored goals
    /// are not links here: *glance* is this screen itself, and *stop* is its
    /// own emergency entry below (spec §67 demands an explicit confirmation
    /// surface, so it stays out of a quiet menu).
    private var goalNav: some View {
        VStack(spacing: 4) {
            HStack(spacing: 10) {
                goalLink(.alert, label: "Incidents", symbol: "exclamationmark.triangle")
                goalLink(.query, label: "Query", symbol: "magnifyingglass")
                goalLink(.acknowledge, label: "Ack", symbol: "checkmark.seal")
                goalLink(.approvals, label: "Approve", symbol: "checkmark.circle")
            }
            .padding(.top, 4)

            // §67 emergency STOP — always visible, always last, always red.
            NavigationLink(value: WatchDestination.stop) {
                Label("STOP", systemImage: "stop.circle.fill")
                    .font(.caption.weight(.bold))
            }
            .buttonStyle(.borderedProminent)
            .tint(Color.nerve(HealthColor.critical.color))
            .accessibilityIdentifier("watch.nav.stop")
        }
    }

    private func goalLink(
        _ destination: WatchDestination,
        label: String,
        symbol: String
    ) -> some View {
        NavigationLink(value: destination) {
            VStack(spacing: 2) {
                Image(systemName: symbol)
                    .font(.body)
                Text(label)
                    .font(.caption2)
            }
        }
        .accessibilityLabel(label)
        .accessibilityIdentifier("watch.nav." + destination.id)
    }
}
