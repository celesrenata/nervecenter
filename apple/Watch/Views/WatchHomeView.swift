import SwiftUI

import NerveApps
import NerveDesign

// MARK: - §27/§64 Watch home screen (the `glance` goal)

/// The §27 attention-first home: the personal plane ("WHAT NEEDS ME?")
/// leads, and the §64 system state follows — full layout when the personal
/// plane is absent, one summary row when attention leads.
///
/// Renders only what the App Group snapshot actually contains: no
/// attention published → no attention rows; no §73 snapshot → an honest
/// "Not connected" state. Nothing is invented (spec §61).
public struct WatchHomeView: View {
    @ObservedObject var store: WatchDataStore
    @ObservedObject var attentionStore: WatchAttentionStore

    public init(store: WatchDataStore, attentionStore: WatchAttentionStore) {
        self.store = store
        self.attentionStore = attentionStore
    }

    public var body: some View {
        VStack(spacing: 6) {
            WatchWordmark()

            // §27 — the personal plane leads: "WHAT NEEDS ME?"
            if attentionStore.attentionPublished {
                attentionBlock
            }

            // §64 — the system plane. When the attention block already
            // leads the screen, the §64 layout compresses to one summary
            // row; when the personal plane is absent (older phone / not
            // published) the §64 layout is the screen, exactly as before.
            if let home = store.homeState {
                if attentionStore.attentionPublished {
                    systemSummaryRow(home)
                } else if case .healthy(let summary) = home {
                    healthyBlock(summary)
                } else if case .problem(let summary) = home {
                    problemBlock(summary)
                }
            } else if !attentionStore.attentionPublished {
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
        .onAppear {
            // The phone re-publishes cached-state.json while the Watch is
            // awake; re-reading both planes lets a newly-published
            // attention feed (or a re-published resolved item) surface
            // without a relaunch.
            store.refresh()
            attentionStore.refresh()
        }
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
                attentionStore.refresh()
            }
            .buttonStyle(.bordered)
            .font(.caption)
        }
        .accessibilityElement(children: .combine)
    }

    // MARK: §27 attention-first home (the personal plane leads)

    /// The §27 "WHAT NEEDS ME?" block.
    ///
    /// Honest states (spec §27/§61): published with live items → a
    /// ranked urgency row per item + wrist actions on the leading item;
    /// published with nothing live → a calm "All clear" row; not
    /// published → the block is absent (the body renders §64 instead).
    private var attentionBlock: some View {
        VStack(alignment: .leading, spacing: 5) {
            if attentionStore.live.isEmpty {
                Label("All clear", systemImage: "checkmark.circle.fill")
                    .font(.headline)
                    .foregroundStyle(Color.nerve(HealthColor.healthy.color))
                    .accessibilityIdentifier("watch.attention.all-clear")
            } else {
                Text(attentionStore.headerText)
                    .font(.headline)
                    .accessibilityIdentifier("watch.attention.header")

                VStack(alignment: .leading, spacing: 4) {
                    ForEach(Array(attentionStore.live.prefix(3)), id: \.id) { item in
                        AttentionItemRow(item: item)
                    }
                }

                AttentionWristActions(item: attentionStore.live[0], store: attentionStore)
                    .accessibilityIdentifier("watch.attention.wrist-actions")

                if let error = attentionStore.lastActionError {
                    Text(error)
                        .font(.caption2)
                        .foregroundStyle(Color.nerve(HealthColor.critical.color))
                        .lineLimit(2)
                        .accessibilityIdentifier("watch.attention.error")
                }
            }
        }
        .accessibilityIdentifier("watch.attention.block")
    }

    /// One compact §64 line so the attention block keeps the screen
    /// (spec §27: the personal plane answers first; the system state
    /// stays visible but quiet). The full healthy/problem layout still
    /// renders when the personal plane is absent.
    private func systemSummaryRow(_ home: WatchHomeState) -> some View {
        HStack(spacing: 5) {
            switch home {
            case .healthy:
                AttentionSeverityDot(urgency: 0)
                Text("System healthy")
            case .problem(let summary):
                AttentionSeverityDot(urgency: 1)
                Text("\(summary.incidentCount) \(summary.incidentCount == 1 ? "INCIDENT" : "INCIDENTS")")
            }
            Spacer(minLength: 0)
        }
        .font(.caption.weight(.semibold))
        .lineLimit(1)
        .accessibilityElement(children: .combine)
        .accessibilityIdentifier("watch.home.system-summary")
    }

    /// One attention row: urgency dot + entity/reason headline + action
    /// subline (spec §27 example: `🔴 Pharmacy / Call before 5 PM`).
    private struct AttentionItemRow: View {
        let item: AttentionRankedItem

        private var primary: String {
            item.item.entities.first ?? item.item.reason
        }

        private var secondary: String? {
            if let action = item.item.suggestedAction {
                return action
            }
            return item.item.entities.isEmpty ? nil : item.item.reason
        }

        var body: some View {
            HStack(spacing: 5) {
                AttentionSeverityDot(urgency: item.item.urgency)
                VStack(alignment: .leading, spacing: 1) {
                    Text(primary)
                        .font(.subheadline.weight(.semibold))
                        .lineLimit(1)
                        .minimumScaleFactor(0.75)
                    if let secondary {
                        Text(secondary)
                            .font(.caption2)
                            .foregroundStyle(.secondary)
                            .lineLimit(1)
                            .minimumScaleFactor(0.75)
                    }
                }
                Spacer(minLength: 0)
            }
            .accessibilityElement(children: .combine)
            .accessibilityLabel(accessibilityText)
            .accessibilityIdentifier("watch.attention.row.\(item.id)")
        }

        private var accessibilityText: String {
            var parts = [primary]
            if let secondary {
                parts.append(secondary)
            }
            if case .escalated = item.localState {
                parts.append("Sent to phone")
            }
            return parts.joined(separator: ". ")
        }
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
