import SwiftUI

import NerveApps
import NerveDesign

// MARK: - §62 large tile
//
// ```text
// goal progress
// compute state
// incidents
// agent activity
// ```

public struct LargeWidgetView: View {
    let state: WidgetDataState

    public init(state: WidgetDataState) {
        self.state = state
    }

    public var body: some View {
        ZStack {
            Color.nerveWidgetCanvas
            switch state {
            case .connected(let snapshot):
                if let tile = snapshot.tile {
                    content(for: tile)
                } else {
                    HonestEmptyView(kind: .noData)
                }
            case .notConnected:
                HonestEmptyView(kind: .notConnected)
            case .noData:
                HonestEmptyView(kind: .noData)
            }
        }
    }

    @ViewBuilder
    private func content(for tile: NerveApps.WidgetTile) -> some View {
        let large = tile.large
        let health = WidgetHealthState(tile.systemHealth)
        let stale = StaleState.evaluate(updatedAt: tile.updatedAt).isStale

        VStack(alignment: .leading, spacing: WidgetMetrics.sectionGap) {
            // Header: health dot + label + stale caption.
            HStack(spacing: WidgetMetrics.rowPadding) {
                Circle()
                    .fill(Color.nerve(health.healthColor.color))
                    .frame(width: 12, height: 12)
                Text(tile.systemHealth.uppercased())
                    .font(.system(size: 15, weight: .bold))
                    .monospacedDigit()
                    .lineLimit(1)
                    .minimumScaleFactor(0.7)
                Spacer(minLength: 0)
                if stale {
                    StaleCaption()
                }
            }

            // §68/§69 Personal: rendered only when the phone actually
            // published personal state. A fresh install renders no section
            // here (honest empty), so the large tile falls back to its
            // engineering layout unchanged.
            if let personal = state.personal, personal.hasData {
                SectionBlock(title: "Personal") {
                    VStack(alignment: .leading, spacing: Spacing.xxs) {
                        if let attentionLine = personal.attentionLine {
                            HStack(spacing: Spacing.sm) {
                                Text(attentionLine)
                                    .font(.system(size: 13, weight: .medium))
                                    .monospacedDigit()
                                    .lineLimit(1)
                                if let title = personal.topAttentionTitle {
                                    Text("· \(title)")
                                        .font(.system(size: 12))
                                        .monospacedDigit()
                                        .foregroundStyle(Color.nerveWidgetSecondary)
                                        .lineLimit(1)
                                        .truncationMode(.tail)
                                }
                                if let urgency = personal.urgencyLabel {
                                    Text(urgency)
                                        .font(.system(size: 10, weight: .bold))
                                        .monospacedDigit()
                                        .foregroundStyle(Color.nerveWidgetSecondary)
                                        .lineLimit(1)
                                }
                                Spacer(minLength: 0)
                            }
                        }
                        if let waitingLine = personal.waitingLine {
                            Text(waitingLine)
                                .font(.system(size: 12))
                                .monospacedDigit()
                                .foregroundStyle(Color.nerveWidgetSecondary)
                                .lineLimit(1)
                        }
                        if let title = personal.topCommitmentTitle,
                           let due = personal.dueLabel() {
                            Text("Next: \(title) — \(due)")
                                .font(.system(size: 12))
                                .monospacedDigit()
                                .foregroundStyle(Color.nerveWidgetSecondary)
                                .lineLimit(1)
                                .truncationMode(.tail)
                        }
                    }
                }
            }

            // 1) Goal progress.
            SectionBlock(title: "Goal") {
                if let label = large.goalLabel, !label.isEmpty {
                    Text(label)
                        .font(.system(size: 13, weight: .medium))
                        .lineLimit(1)
                        .truncationMode(.tail)
                        .minimumScaleFactor(0.8)
                    if let progress = large.goalProgress {
                        ProgressView(value: progress)
                            .tint(Color.nerve(health.healthColor.color))
                    }
                } else {
                    HonestSectionText("No goal")
                }
            }

            // 2) Compute state (top 3 rows; honest "No compute data" when absent).
            SectionBlock(title: "Compute") {
                if large.computeRows.isEmpty {
                    HonestSectionText("No compute data")
                } else {
                    ForEach(large.computeRows.prefix(4)) { row in
                        ComputeRowLabel(row: row)
                    }
                }
            }

            // 3) Incidents (+ pending approvals, surfaced when non-zero).
            SectionBlock(title: "Incidents") {
                if let summary = large.incidentsSummary, !summary.isEmpty {
                    Text(summary)
                        .font(.system(size: 12))
                        .foregroundStyle(Color.nerveWidgetSecondary)
                        .lineLimit(2)
                        .minimumScaleFactor(0.8)
                } else {
                    Text("\(tile.incidents) active")
                        .font(.system(size: 12))
                        .monospacedDigit()
                        .foregroundStyle(Color.nerveWidgetSecondary)
                        .lineLimit(1)
                }
                if large.pendingApprovals > 0 {
                    Text("\(large.pendingApprovals) approvals pending")
                        .font(.system(size: 12, weight: .medium))
                        .monospacedDigit()
                        .foregroundStyle(Color.nerveHealth(tile.systemHealth))
                        .lineLimit(1)
                }
            }

            // 4) Agent activity.
            SectionBlock(title: "Agents") {
                if let activity = large.agentActivitySummary, !activity.isEmpty {
                    Text(activity)
                        .font(.system(size: 12))
                        .foregroundStyle(Color.nerveWidgetSecondary)
                        .lineLimit(2)
                        .minimumScaleFactor(0.8)
                } else {
                    Text("\(tile.agents) running")
                        .font(.system(size: 12))
                        .monospacedDigit()
                        .foregroundStyle(Color.nerveWidgetSecondary)
                        .lineLimit(1)
                }
            }

            Spacer(minLength: 0)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .padding(WidgetMetrics.rowPadding)
    }
}

/// A titled section with a fixed label column so section names align.
private struct SectionBlock<Content: View>: View {
    let title: String
    @ViewBuilder let content: Content

    var body: some View {
        VStack(alignment: .leading, spacing: Spacing.xxs) {
            Text(title.uppercased())
                .font(.system(size: 10, weight: .bold))
                .foregroundStyle(Color.nerveWidgetSecondary)
                .lineLimit(1)
            content
        }
    }
}

/// Honest placeholder copy inside a section (no invented values).
private struct HonestSectionText: View {
    let text: String

    init(_ text: String) { self.text = text }

    var body: some View {
        Text(text)
            .font(.system(size: 12))
            .foregroundStyle(Color.nerveWidgetSecondary)
            .lineLimit(1)
    }
}
