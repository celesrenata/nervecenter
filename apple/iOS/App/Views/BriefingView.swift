import SwiftUI

import NerveApps
import NerveDesign

// MARK: - Briefing tab (§30)

/// Briefing — the daily briefing from `GET /api/briefing?kind=` (prompt2 §30).
///
/// A read-only, deterministic aggregation from the personal-operations
/// planes: attention, waiting-on, calendar, email, messages, voicemail.
/// The screen renders the full composed brief or an honest closing line
/// when nothing needs attention ("Nothing else requires attention.")
///
/// The clock is pinned by the compose call (defaults to the wall clock)
/// so the brief is always honest and the screen shows exactly the
/// closing line when nothing needs action (§30).
public struct BriefingView: View {
    @ObservedObject public var store: PersonalDataStore
    @EnvironmentObject public var env: PhoneEnvironment

    public init(store: PersonalDataStore) {
        self.store = store
    }

    public var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: Spacing.md) {
                    // Kind picker
                    KindPicker(store: store)
                        .padding(.top, Spacing.xs)

                    switch store.briefingState {
                    case .loading:
                        LoadingRow(text: "Loading briefing…")
                    case .error(let message):
                        PersonalErrorRow(title: "Could not load Briefing", message: message)
                    case .empty, .loaded:
                        if let briefing = store.briefing {
                            if briefing.empty {
                                emptyState(briefing)
                            } else {
                                renderBriefing(briefing)
                            }
                        } else {
                            HonestEmptyRow(
                                title: "No data",
                                message: "No briefing loaded."
                            )
                        }
                    }
                }
                .padding(.horizontal, Spacing.md)
                .padding(.top, Spacing.sm)
                .padding(.bottom, Spacing.lg)
            }
            .background(Color.phoneCanvas.ignoresSafeArea())
            .navigationTitle("Briefing")
            .onAppear { refresh() }
            .refreshable { await refresh() }
            .accessibilityIdentifier("phone.tab.briefing")
        }
    }

    private func refresh() {
        guard let service = PersonalSurfaceRefresh.makeService(
            server: env.server,
            store: store
        ) else { return }
        Task { await service.fetchBriefing(kind: store.briefingKind) }
    }

    private func emptyState(_ briefing: BriefingData) -> some View {
        VStack(alignment: .leading, spacing: Spacing.xs) {
            Text("Today")
                .font(.headline)
            if let closing = briefing.closingLine {
                Text(closing)
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
            }
        }
        .padding(Spacing.md)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.phoneCanvas)
    }

    private func renderBriefing(_ briefing: BriefingData) -> some View {
        VStack(alignment: .leading, spacing: Spacing.md) {
            // Actions (things need action)
            if !briefing.actions.isEmpty {
                SectionHeader(title: "Needs action", count: briefing.actions.count)
                ForEach(briefing.actions) { action in
                    ActionRow(action: action)
                }
            }

            // Waiting-on (you're waiting on)
            if !briefing.waitingOn.isEmpty {
                SectionHeader(title: "You're waiting on", count: briefing.waitingOn.count)
                ForEach(briefing.waitingOn) { waiting in
                    WaitingRow(waiting: waiting)
                }
            }

            // Today (calendar events)
            if !briefing.today.isEmpty {
                SectionHeader(title: "Today", count: briefing.today.count)
                ForEach(briefing.today) { event in
                    EventRow(event: event)
                }
            }

            // Overnight activity (how much new stuff arrived yesterday)
            SectionHeader(title: "Overnight", count: 0)
            HStack {
                Text("New emails")
                    .font(.body)
                    .foregroundStyle(.primary)
                Spacer()
                Text("\(briefing.overnightEmails)")
                    .font(.body.monospacedDigit())
                    .foregroundStyle(.secondary)
            }
            HStack {
                Text("New messages")
                    .font(.body)
                    .foregroundStyle(.primary)
                Spacer()
                Text("\(briefing.overnightMessages)")
                    .font(.body.monospacedDigit())
                    .foregroundStyle(.secondary)
            }
            HStack {
                Text("New voicemails")
                    .font(.body)
                    .foregroundStyle(.primary)
                Spacer()
                Text("\(briefing.overnightVoicemails)")
                    .font(.body.monospacedDigit())
                    .foregroundStyle(.secondary)
            }

            // Closing line
            if let closing = briefing.closingLine {
                Text(closing)
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
                    .padding(.top, Spacing.md)
            }
        }
    }
}

// MARK: - Rows for the sections

extension BriefingView {
    struct ActionRow: View {
        let action: BriefingActionRow

        var body: some View {
            VStack(alignment: .leading, spacing: 2) {
                Text(action.label)
                    .font(.body)
                    .foregroundStyle(.primary)
                    .lineLimit(2)
                if let due = action.due {
                    Text("Due \(DateFormatter.localizedString(from: due, dateStyle: .short, timeStyle: .short))")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
            .padding(Spacing.sm)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(Color.phoneCanvas)
        }
    }

    struct WaitingRow: View {
        let waiting: BriefingWaitingRow

        var body: some View {
            VStack(alignment: .leading, spacing: 2) {
                Text(waiting.description)
                    .font(.body)
                    .foregroundStyle(.primary)
                    .lineLimit(2)
                Text("\(waiting.person): \(TodayComposer.daysPhrase(waiting.daysElapsed))")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            .padding(Spacing.sm)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(Color.phoneCanvas)
        }
    }

    struct EventRow: View {
        let event: BriefingEventRow

        var body: some View {
            VStack(alignment: .leading, spacing: 2) {
                Text(event.title)
                    .font(.body)
                    .foregroundStyle(.primary)
                if let start = event.start {
                    Text(DateFormatter.localizedString(from: start, dateStyle: .short, timeStyle: .short))
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
            .padding(Spacing.sm)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(Color.phoneCanvas)
        }
    }
}

// MARK: - Kind picker

struct KindPicker: View {
    @ObservedObject var store: PersonalDataStore
    @EnvironmentObject var env: PhoneEnvironment

    var body: some View {
        Picker("Time of day", selection: Binding(
            get: { store.briefingKind },
            set: { kind in
                store.briefingKind = kind
                if let service = PersonalSurfaceRefresh.makeService(server: env.server, store: store) {
                    Task { await service.fetchBriefing(kind: kind) }
                }
            }
        )) {
            ForEach(BriefingKind.allCases, id: \.self) { kind in
                Text(kind.displayName).tag(kind)
            }
        }
        .pickerStyle(.segmented)
    }
}
