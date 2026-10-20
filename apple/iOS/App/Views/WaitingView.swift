import SwiftUI

import NerveApps
import NerveDesign

// MARK: - Waiting tab (§66)

/// Waiting — the waiting-on queue from `GET /api/waiting-on` with a
/// status filter (`?status=open|resolved|cancelled|all`).
///
/// Each row is an outgoing request that has not received a response
/// (prompt2 §19): "WAITING ON / {person} / {description} / {n} days /
/// [Follow up]". The backend orders open results most-waiting-first, so
/// we render in that order. The follow-up action is read-only here — the
/// backend's follow-up draft is AI-drafted, human-sent (§13: never
/// auto-sent), and sending is a separate surface, so the row shows the
/// draft count but does not send.
public struct WaitingView: View {
    @ObservedObject public var store: PersonalDataStore
    @EnvironmentObject public var env: PhoneEnvironment

    public init(store: PersonalDataStore) {
        self.store = store
    }

    public var body: some View {
        NavigationStack {
            ScrollView {
                LazyVStack(alignment: .leading, spacing: Spacing.sm) {
                    // Status filter picker
                    StatusFilterPicker(store: store)
                        .padding(.top, Spacing.xs)

                    switch store.waitingState {
                    case .loading:
                        LoadingRow(text: "Loading waiting queue…")
                    case .error(let message):
                        PersonalErrorRow(title: "Could not load Waiting", message: message)
                    case .empty, .loaded:
                        if store.waitingRows.isEmpty {
                            HonestEmptyRow(
                                title: waitingEmptyTitle(for: store.waitingFilter),
                                message: waitingEmptyMessage(for: store.waitingFilter)
                            )
                        } else {
                            ForEach(store.waitingRows) { row in
                                WaitingRowView(row: row)
                            }
                        }
                    }
                }
                .padding(.horizontal, Spacing.md)
                .padding(.top, Spacing.sm)
                .padding(.bottom, Spacing.lg)
            }
            .background(Color.phoneCanvas.ignoresSafeArea())
            .navigationTitle("Waiting")
            .onAppear { refresh() }
            .refreshable { await refresh() }
            .accessibilityIdentifier("phone.tab.waiting")
        }
    }

    private func refresh() {
        guard let service = PersonalSurfaceRefresh.makeService(
            server: env.server,
            store: store
        ) else { return }
        Task { await service.fetchWaiting(status: store.waitingFilter) }
    }

    private func waitingEmptyTitle(for filter: WaitingStatusFilter) -> String {
        switch filter {
        case .open: return "Nothing waiting"
        case .resolved: return "Nothing resolved"
        case .cancelled: return "Nothing cancelled"
        case .all: return "No waiting requests"
        }
    }

    private func waitingEmptyMessage(for filter: WaitingStatusFilter) -> String {
        switch filter {
        case .open:
            return "No open requests that have gone unanswered."
        case .resolved:
            return "No requests marked resolved yet."
        case .cancelled:
            return "No requests cancelled yet."
        case .all:
            return "No waiting-on requests on any channel."
        }
    }
}

// MARK: - Rows

extension WaitingView {
    struct WaitingRowView: View {
        let row: WaitingRow

        var body: some View {
            VStack(alignment: .leading, spacing: 2) {
                HStack(alignment: .firstTextBaseline) {
                    Text(row.personDisplayName)
                        .font(.body.weight(.medium))
                        .foregroundStyle(.primary)
                    Spacer()
                    Label(
                        TodayComposer.daysPhrase(row.daysElapsed),
                        systemImage: "hourglass"
                    )
                    .font(.caption)
                    .foregroundStyle(.secondary)
                }
                Text(row.description)
                    .font(.body)
                    .foregroundStyle(.primary)
                    .lineLimit(2)
                HStack(spacing: Spacing.xs) {
                    Label(
                        sourceLabel(row.source),
                        systemImage: sourceSymbol(row.source)
                    )
                    .font(.caption2)
                    .foregroundStyle(.tertiary)
                    if row.followUpCount > 0 {
                        Text("\(row.followUpCount) follow-up\(row.followUpCount == 1 ? "" : "s") drafted")
                            .font(.caption2)
                            .foregroundStyle(.tertiary)
                    }
                    if row.status != "open" {
                        Text(statusLabel(row.status))
                            .font(.caption2)
                            .foregroundStyle(.tertiary)
                    }
                }
            }
            .padding(Spacing.sm)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(Color.phoneCanvas)
        }

        private func sourceLabel(_ source: String) -> String { source }
        private func statusLabel(_ status: String) -> String { status.capitalized }
        private func sourceSymbol(_ source: String) -> String {
            switch source.lowercased() {
            case "email": return "envelope"
            case "messages": return "message"
            case "voicemail": return "phone"
            case "manual": return "hand.point.up"
            default: return "ellipsis.bubble"
            }
        }
    }
}

// MARK: - Status filter picker

struct StatusFilterPicker: View {
    @ObservedObject var store: PersonalDataStore
    @EnvironmentObject var env: PhoneEnvironment

    var body: some View {
        Picker("Status", selection: Binding(
            get: { store.waitingFilter },
            set: { filter in
                store.waitingFilter = filter
                if let service = PersonalSurfaceRefresh.makeService(server: env.server, store: store) {
                    Task { await service.fetchWaiting(status: filter) }
                }
            }
        )) {
            ForEach(WaitingStatusFilter.allCases, id: \.self) { filter in
                Text(filter.displayName).tag(filter)
            }
        }
        .pickerStyle(.segmented)
    }
}
