import SwiftUI

import NerveApps
import NerveDesign

// MARK: - §62 small tile
//
// ```text
// ● HEALTHY
// 7 agents
// 0 incidents
// ```

public struct SmallWidgetView: View {
    /// The connected snapshot (or the honest empty state).
    let state: WidgetDataState

    public init(state: WidgetDataState) {
        self.state = state
    }

    public var body: some View {
        ZStack {
            Color.nerveWidgetCanvas
            switch state {
            case .connected(let snapshot):
                #if os(watchOS)
                if let attentionText = snapshot.attentionText {
                    // §67: on the wrist, the complication prefers "n NEED
                    // YOU" / "✓ CLEAR" over raw counts. Rendered only when
                    // the phone actually published an attention feed; a
                    // nil falls through to the system tile below (honest
                    // absence — spec §61).
                    AttentionComplicationView(text: attentionText)
                } else if let tile = snapshot.tile {
                    content(for: tile)
                } else {
                    HonestEmptyView(kind: .noData)
                }
                #else
                if let tile = snapshot.tile {
                    content(for: tile)
                } else {
                    HonestEmptyView(kind: .noData)
                }
                #endif
            case .notConnected:
                HonestEmptyView(kind: .notConnected)
            case .noData:
                HonestEmptyView(kind: .noData)
            }
        }
    }

    @ViewBuilder
    private func content(for tile: NerveApps.WidgetTile) -> some View {
        let small = tile.small
        let health = WidgetHealthState(tile.systemHealth)
        let stale = snapshotIsStale()
        VStack(alignment: .leading, spacing: WidgetMetrics.rowPadding) {
            HStack(spacing: WidgetMetrics.rowPadding) {
                Circle()
                    .fill(Color.nerve(health.healthColor.color))
                    .frame(width: 12, height: 12)
                Text(small.healthLabel)
                    .font(.system(size: 15, weight: .bold))
                    .monospacedDigit()
                    .lineLimit(1)
                    .minimumScaleFactor(0.7)
                Spacer(minLength: 0)
                if stale {
                    StaleCaption()
                }
            }
            HStack(spacing: Spacing.sm) {
                Text("\(small.agentCount) agents")
                Text("\(small.incidentCount) incidents")
            }
            .font(.system(size: 13))
            .monospacedDigit()
            .foregroundStyle(Color.nerveWidgetSecondary)
            .lineLimit(1)
            .minimumScaleFactor(0.8)
            Spacer(minLength: 0)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .padding(WidgetMetrics.rowPadding)
    }

    private func snapshotIsStale() -> Bool {
        guard case .connected(let snapshot) = state,
              let updatedAt = snapshot.effectiveUpdatedAt else {
            return false
        }
        return StaleState.evaluate(updatedAt: updatedAt).isStale
    }
}

#if os(watchOS)
/// §67 complication content: "3 NEED YOU" / "✓ CLEAR", color-coded by
/// meaning (critical red / healthy green).
private struct AttentionComplicationView: View {
    let text: String

    var body: some View {
        Text(text)
            .font(.system(size: 12, weight: .bold))
            .monospacedDigit()
            .multilineTextAlignment(.center)
            .lineLimit(2)
            .minimumScaleFactor(0.6)
            .foregroundStyle(
                text.contains("CLEAR")
                    ? Color.nerve(HealthColor.healthy.color)
                    : Color.nerve(HealthColor.critical.color)
            )
            .padding(4)
            .accessibilityLabel(text)
    }
}
#endif

// MARK: - Honest empty state (shared by all sizes)

/// The text shown when there is nothing real to render. No invented numbers.
enum HonestEmptyKind: Equatable {
    case notConnected
    case noData
}

struct HonestEmptyView: View {
    let kind: HonestEmptyKind

    var body: some View {
        VStack(alignment: .leading, spacing: Spacing.xxs) {
            Spacer(minLength: 0)
            Text(kind == .notConnected ? "Not connected" : "No data yet")
                .font(.system(size: 14, weight: .medium))
                .foregroundStyle(Color.nerveWidgetPrimary)
                .lineLimit(2)
                .minimumScaleFactor(0.8)
            if kind == .notConnected {
                Text("Open Nerve Center to sync")
                    .font(.system(size: 12))
                    .foregroundStyle(Color.nerveWidgetSecondary)
                    .lineLimit(2)
                    .minimumScaleFactor(0.8)
            }
            Spacer(minLength: 0)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .padding(WidgetMetrics.rowPadding)
    }
}

/// A small "stale" caption shown when the tile's `updatedAt` is older than
/// ``StaleState/StalenessPolicy/default`` (90 s).
struct StaleCaption: View {
    var body: some View {
        Text("stale")
            .font(.system(size: 10, weight: .semibold))
            .foregroundStyle(Color.nerveWidgetSecondary)
            .lineLimit(1)
    }
}
