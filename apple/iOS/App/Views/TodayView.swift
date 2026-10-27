import SwiftUI

import NerveApps
import NerveDesign

// MARK: - Today tab (§66)

/// Today — the attention-ranked rollup of "what is on me today".
///
/// Rows come ONLY from real endpoints (`/api/attention`,
/// `/api/followups?status=OPEN`, `/api/waiting-on?status=open`). The
/// composition (rank + dedup) is `TodayComposer` in `NerveApps`. A
/// missing source is reported honestly in the section label; nothing is
/// invented to fill a gap (§61/§62).
public struct TodayView: View {
    @ObservedObject public var store: PersonalDataStore
    @EnvironmentObject public var env: PhoneEnvironment

    public init(store: PersonalDataStore) {
        self.store = store
    }

    public var body: some View {
        ScrollView {
            LazyVStack(alignment: .leading, spacing: Spacing.sm) {
                switch store.todayState {
                case .loading:
                    LoadingRow(text: "Loading today…")
                case .error(let message):
                    PersonalErrorRow(title: "Could not load Today", message: message)
                case .empty, .loaded:
                    if store.todayRows.isEmpty {
                        HonestEmptyRow(
                            title: "Nothing needs you today",
                            message: "No open attention, follow-ups, or waiting requests."
                        )
                    } else {
                        ForEach(sectionedRows, id: \.title) { section in
                            SectionHeader(title: section.title, count: section.rows.count)
                            ForEach(section.rows) { row in
                                TodayRowView(row: row)
                            }
                        }
                    }
                    if !store.todaySourceErrors.isEmpty {
                        SourceErrorFootnote(errors: store.todaySourceErrors)
                    }
                }
            }
            .padding(.horizontal, Spacing.md)
            .padding(.top, Spacing.sm)
            .padding(.bottom, Spacing.lg)
        }
        .background(Color.phoneCanvas.ignoresSafeArea())
        .navigationTitle("Today")
        .onAppear { refresh() }
        .refreshable { await refresh() }
        .accessibilityIdentifier("phone.tab.today")
    }

    private func refresh() {
        guard let service = PersonalSurfaceRefresh.makeService(
            server: env.server,
            store: store
        ) else { return }
        Task {
            await service.fetchToday()
            // §68/§69: publish the personal plane to the App Group so the
            // widget (and watch) render real personal state on this fetch's
            // cadence. Honest: a failed source simply omits its fields
            // (never a fabricated 0-count); a fetch that failed *before
            // producing any rows* publishes nothing at all (the environment
            // then keeps whatever personal state was last published).
            env.publishPersonalState(derivedPersonalState())
        }
    }

    /// Derive the §68/§69 ``PersonalStateWire`` from the real personal store
    /// (Today rollup: attention queue, open follow-ups, waiting-on requests).
    ///
    /// Fields are published only from sources that fetched successfully
    /// (a failed source omits its fields rather than reporting 0); the top
    /// attention item is the highest-ranked attention row, the top
    /// commitment is the soonest-due open follow-up (the only personal
    /// source with a real ``due_at``), and the waiting count is the number
    /// of open waiting rows. Returns `nil` — publish nothing — when no
    /// source fetched successfully (never a fabricated 0-count). §61.
    private func derivedPersonalState() -> PersonalStateWire? {
        let rows = store.todayRows
        let failed = store.todaySourceErrors
        let attention = rows.filter { $0.origin == .attention }
        let followups = rows.filter { $0.origin == .followup }
        let waiting = rows.filter { $0.origin == .waiting }
        var wire = PersonalStateWire()
        var populated = false
        if failed[TodayOrigin.attention.rawValue] == nil {
            wire.attentionCount = attention.count
            // The full feed (not just the count) is what the watch renders —
            // `PersonalFetchService` stored the wire projection next to the
            // rows on this fetch; a failed source left it `nil`, so the
            // published wire omits the `attention` key (honest, §61).
            wire.attention = store.attentionWireItems
            if let top = attention.first {
                wire.topAttentionTitle = top.label
                wire.topAttentionUrgency = top.priority
            }
            populated = true
        }
        if failed[TodayOrigin.followup.rawValue] == nil {
            if let top = topCommitment(from: followups) {
                wire.topCommitmentTitle = top.label
                wire.topCommitmentDueAt = top.date
            }
            populated = true
        }
        if failed[TodayOrigin.waiting.rawValue] == nil {
            wire.waitingOnCount = waiting.count
            populated = true
        }
        return populated ? wire : nil
    }

    /// The soonest-due row among open follow-ups (deterministic tiebreak on
    /// row id), or `nil` when there are no dated follow-ups — the only
    /// personal source whose rows carry a real `due_at`.
    private func topCommitment(from rows: [TodayRow]) -> TodayRow? {
        rows
            .filter { $0.date != nil }
            .min { lhs, rhs in
                let a = lhs.date ?? .distantFuture
                let b = rhs.date ?? .distantFuture
                guard a == b else { return a < b }
                return lhs.id < rhs.id
            }
    }

    /// Group the flat rollup into the three §25 sections the Today screen
    /// presents: Needs you (attention), Due (follow-ups), Waiting on
    /// (waiting requests). The backend already returns each source in
    /// rank order, so we preserve that order within each section.
    private var sectionedRows: [(title: String, rows: [TodayRow])] {
        let attention = store.todayRows.filter { $0.origin == .attention }
        let followups = store.todayRows.filter { $0.origin == .followup }
        let waiting = store.todayRows.filter { $0.origin == .waiting }
        return [
            ("Needs you", attention),
            ("Due", followups),
            ("Waiting on", waiting),
        ]
    }
}

// MARK: - Row

extension TodayView {
    struct TodayRowView: View {
        let row: TodayRow

        var body: some View {
            VStack(alignment: .leading, spacing: 2) {
                HStack(alignment: .firstTextBaseline) {
                    Text(row.label)
                        .font(.body)
                        .foregroundStyle(.primary)
                    Spacer()
                    if row.priority > 0 {
                        Text(String(format: "%.0f", row.priority * 100))
                            .font(.caption.monospacedDigit())
                            .foregroundStyle(.secondary)
                            .accessibilityLabel("priority \(Int(row.priority * 100))")
                    }
                }
                if let detail = row.detail {
                    Text(detail)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                }
                HStack(spacing: Spacing.xxs) {
                    Label(
                        row.origin.displayName,
                        systemImage: originSymbol(row.origin)
                    )
                    .font(.caption2)
                    .foregroundStyle(.tertiary)
                    if let date = row.date {
                        Text(dateLabel(date))
                            .font(.caption2)
                            .foregroundStyle(.tertiary)
                    }
                }
            }
            .padding(.vertical, Spacing.xs)
            .padding(.horizontal, Spacing.xs)
        }

        private func originSymbol(_ origin: TodayOrigin) -> String {
            switch origin {
            case .attention: return "exclamationmark.circle"
            case .followup: return "arrow.triangle.2.circlepath"
            case .waiting: return "hourglass"
            }
        }

        private func dateLabel(_ date: Date) -> String {
            let f = DateFormatter()
            f.dateStyle = .medium
            f.timeStyle = .short
            return f.string(from: date)
        }
    }
}

// MARK: - Shared personal-surface row states

/// A small, honest loading placeholder.
public struct LoadingRow: View {
    let text: String
    public init(text: String) { self.text = text }
    public var body: some View {
        HStack(spacing: Spacing.xs) {
            ProgressView()
            Text(text)
                .font(.subheadline)
                .foregroundStyle(.secondary)
        }
        .padding(.top, Spacing.lg)
    }
}

/// A typed error state (the surface failed to load).
public struct PersonalErrorRow: View {
    let title: String
    let message: String
    public init(title: String, message: String) {
        self.title = title
        self.message = message
    }
    public var body: some View {
        VStack(alignment: .leading, spacing: Spacing.xs) {
            Text(title)
                .font(.subheadline.weight(.semibold))
                .foregroundStyle(.primary)
            Text(message)
                .font(.caption)
                .foregroundStyle(.secondary)
        }
        .padding(Spacing.sm)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(.red.opacity(0.08))
        .padding(.top, Spacing.lg)
    }
}

/// An honest empty state (the surface loaded and has no rows).
public struct HonestEmptyRow: View {
    let title: String
    let message: String
    public init(title: String, message: String) {
        self.title = title
        self.message = message
    }
    public var body: some View {
        VStack(spacing: Spacing.xs) {
            Text(title)
                .font(.subheadline)
                .foregroundStyle(.secondary)
            Text(message)
                .font(.caption)
                .foregroundStyle(.tertiary)
        }
        .padding(.top, Spacing.lg)
    }
}

/// A section header with an optional count.
public struct SectionHeader: View {
    let title: String
    let count: Int
    public init(title: String, count: Int) {
        self.title = title
        self.count = count
    }
    public var body: some View {
        HStack {
            Text(title)
                .font(.headline)
            Text("\(count)")
                .font(.caption.monospacedDigit())
                .foregroundStyle(.secondary)
            Spacer()
        }
        .padding(.top, Spacing.xs)
    }
}

/// The Today screen's footnote when a source failed to load.
public struct SourceErrorFootnote: View {
    let errors: [String: String]
    public init(errors: [String: String]) { self.errors = errors }
    public var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            Text("Some sources could not be loaded:")
                .font(.caption.weight(.semibold))
                .foregroundStyle(.secondary)
            ForEach(Array(errors.keys.sorted()), id: \.self) { key in
                Text("\(key): \(errors[key] ?? "")")
                    .font(.caption2)
                    .foregroundStyle(.tertiary)
                    .lineLimit(1)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.top, Spacing.sm)
    }
}
