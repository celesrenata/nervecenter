import SwiftUI

import NerveApps
import NerveDesign

// MARK: - §62 medium tile
//
// ```text
// 5090       72%
// 4070       14%
// M5         58%
//
// Agents      7
// Incidents   0
// ```

public struct MediumWidgetView: View {
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
        let medium = tile.medium
        let health = WidgetHealthState(tile.systemHealth)
        let stale = StaleState.evaluate(updatedAt: tile.updatedAt).isStale

        VStack(alignment: .leading, spacing: WidgetMetrics.rowPadding) {
            HStack(spacing: WidgetMetrics.rowPadding) {
                Circle()
                    .fill(Color.nerve(health.healthColor.color))
                    .frame(width: 10, height: 10)
                Text(tile.systemHealth.uppercased())
                    .font(.system(size: 12, weight: .bold))
                    .monospacedDigit()
                    .lineLimit(1)
                    .minimumScaleFactor(0.7)
                Spacer(minLength: 0)
                if stale {
                    StaleCaption()
                }
            }

            // Compute rows: top 3, node name left + utilization right with
            // monospaced digits so percentages line up. Long node names are
            // truncated rather than wrapping.
            if medium.computeRows.isEmpty {
                Text("No compute data")
                    .font(.system(size: 13))
                    .foregroundStyle(Color.nerveWidgetSecondary)
                    .lineLimit(1)
            } else {
                ForEach(medium.computeRows.prefix(3)) { row in
                    ComputeRowLabel(row: row)
                }
            }

            // §68/§69 personal lines, rendered only when the phone published
            // them — a fresh install renders none (no fabricated rows).
            if let personal = state.personal, personal.hasData {
                VStack(alignment: .leading, spacing: Spacing.xxs) {
                    if let attentionLine = personal.attentionLine {
                        Text(attentionLine)
                            .font(.system(size: 13, weight: .medium))
                            .monospacedDigit()
                            .lineLimit(1)
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
                        Text("Next: \(title) \(due)")
                            .font(.system(size: 12))
                            .monospacedDigit()
                            .foregroundStyle(Color.nerveWidgetSecondary)
                            .lineLimit(1)
                            .truncationMode(.tail)
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            }

            Spacer(minLength: 0)

            HStack(alignment: .firstTextBaseline, spacing: WidgetMetrics.rowPadding) {
                Text("Agents")
                    .font(.system(size: 13))
                    .foregroundStyle(Color.nerveWidgetSecondary)
                    .lineLimit(1)
                Text("\(medium.agentCount)")
                    .font(.system(size: 13, weight: .semibold))
                    .monospacedDigit()
                Spacer(minLength: 0)
                Text("Incidents")
                    .font(.system(size: 13))
                    .foregroundStyle(Color.nerveWidgetSecondary)
                    .lineLimit(1)
                Text("\(medium.incidentCount)")
                    .font(.system(size: 13, weight: .semibold))
                    .monospacedDigit()
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .padding(WidgetMetrics.rowPadding)
    }
}

/// One compute row: `nodeID  hardware … utilization%`.
///
/// Long node names get a hard truncation (no wrapping) so a pathological
/// name can't push the tile's layout apart. The utilisation column is fixed
/// width and right-aligned with monospaced digits.
struct ComputeRowLabel: View {
    let row: ComputeWidgetRow

    private var utilization: String { "\(row.utilizationPercent)%" }

    var body: some View {
        HStack(spacing: WidgetMetrics.rowPadding) {
            Text(row.nodeID)
                .font(.system(size: 13, weight: .medium))
                .lineLimit(1)
                .truncationMode(.middle)
                .minimumScaleFactor(0.7)
            Text(row.hardware)
                .font(.system(size: 11))
                .foregroundStyle(Color.nerveWidgetSecondary)
                .lineLimit(1)
                .truncationMode(.tail)
                .minimumScaleFactor(0.7)
            Spacer(minLength: WidgetMetrics.rowPadding)
            Text(utilization)
                .font(.system(size: 13, weight: .semibold))
                .monospacedDigit()
                .lineLimit(1)
        }
    }
}
