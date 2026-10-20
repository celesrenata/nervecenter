import SwiftUI

import NerveApps
import NerveDesign

// MARK: - §27/§60/§67 attention surface design tokens (watch-only)

/// Watch-only color mapping for the §21 urgency scale. The shared design
/// package holds platform-free tokens; this target maps urgency → SwiftUI
/// colors the same way `WatchDesign` maps health.
public extension Color {
    /// The urgency color for an item's §21 `urgency` (0...1):
    ///
    /// - `>= 0.7` → critical red (matches the backend `URGENT_THRESHOLD`)
    /// - `>= 0.4` → warning orange (the backend `LOW_PRIORITY_THRESHOLD`
    ///   boundary)
    /// - below → healthy green (visible but calm)
    static func attentionUrgency(_ urgency: Double) -> Color {
        if urgency >= 0.7 {
            return Color.nerve(HealthColor.critical.color)
        } else if urgency >= 0.4 {
            return Color.nerve(HealthColor.degraded.color)
        } else {
            return Color.nerve(HealthColor.healthy.color)
        }
    }
}

/// §27 attention severity dot: a small colored circle leading each row.
public struct AttentionSeverityDot: View {
    public let urgency: Double

    public init(urgency: Double) {
        self.urgency = urgency
    }

    public var body: some View {
        Circle()
            .fill(Color.attentionUrgency(urgency))
            .frame(width: 8, height: 8)
            .accessibilityHidden(true)
    }
}

/// §60 per-item wrist action row (snooze / resolve / escalate).
///
/// The watch writes *records* (App Group `watch-actions.json`); the phone
/// executes them against the backend attention actions on its next sync.
public struct AttentionWristActions: View {
    public let item: AttentionRankedItem
    public let store: WatchAttentionStore

    public init(item: AttentionRankedItem, store: WatchAttentionStore) {
        self.item = item
        self.store = store
    }

    public var body: some View {
        HStack(spacing: 6) {
            wristButton("Snooze", systemImage: "zzz", action: { store.snooze(item) })
                .accessibilityIdentifier("watch.attention.snooze.\(item.id)")
            wristButton("Done", systemImage: "checkmark", action: { store.resolve(item) })
                .accessibilityIdentifier("watch.attention.resolve.\(item.id)")
            wristButton("Escalate", systemImage: "arrow.up.circle", action: { store.escalate(item) })
                .accessibilityIdentifier("watch.attention.escalate.\(item.id)")
        }
    }

    private func wristButton(
        _ label: String,
        systemImage: String,
        action: @escaping () -> Void
    ) -> some View {
        Button(action: action) {
            Label(label, systemImage: systemImage)
                .font(.caption2.weight(.semibold))
        }
        .buttonStyle(.bordered)
        .accessibilityLabel(label + " " + item.item.reason)
    }
}
