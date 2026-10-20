import SwiftUI

import NerveApps
import NerveDesign

// MARK: - People tab (§66)

/// People — the canonical person list from `GET /api/people` (R101).
///
/// Each person shows their normalized identities (the §6 merged identity
/// links — emails / phones / iMessage handles / voicemail / contact
/// values) and their per-channel communication threads. The VIP toggle
/// writes through `POST /api/people/{id}/vip` (R101 step 5) with a small
/// optimistic state machine (`VipToggle` in `NerveApps`); a failed toggle
/// rolls back to the server-confirmed value.
///
/// Honest: the list is the backend's canonical persons, never a local
/// contacts dump, and an unattributed person (no display name) falls back
/// to their stable id, not a fabricated name.
public struct PeopleView: View {
    @ObservedObject public var store: PersonalDataStore
    @EnvironmentObject public var env: PhoneEnvironment

    public init(store: PersonalDataStore) {
        self.store = store
    }

    public var body: some View {
        NavigationStack {
            ScrollView {
                LazyVStack(alignment: .leading, spacing: Spacing.sm) {
                    switch store.peopleState {
                    case .loading:
                        LoadingRow(text: "Loading people…")
                    case .error(let message):
                        PersonalErrorRow(title: "Could not load People", message: message)
                    case .empty, .loaded:
                        if store.people.isEmpty {
                            HonestEmptyRow(
                                title: "No people",
                                message: "No canonical persons on any channel yet."
                            )
                        } else {
                            ForEach(store.people) { person in
                                PersonRowView(person: person, store: store)
                            }
                        }
                    }
                }
                .padding(.horizontal, Spacing.md)
                .padding(.top, Spacing.sm)
                .padding(.bottom, Spacing.lg)
            }
            .background(Color.phoneCanvas.ignoresSafeArea())
            .navigationTitle("People")
            .onAppear { refresh() }
            .refreshable { await refresh() }
            .accessibilityIdentifier("phone.tab.people")
        }
    }

    private func refresh() {
        guard let service = PersonalSurfaceRefresh.makeService(
            server: env.server,
            store: store
        ) else { return }
        Task { await service.fetchPeople() }
    }
}

// MARK: - Person row + detail

extension PeopleView {
    struct PersonRowView: View {
        let person: PersonRow
        @ObservedObject var store: PersonalDataStore
        @EnvironmentObject var env: PhoneEnvironment
        @State private var showingDetail = false

        var body: some View {
            Button {
                showingDetail = true
            } label: {
                VStack(alignment: .leading, spacing: 2) {
                    HStack {
                        Text(person.displayLabel)
                            .font(.body.weight(.medium))
                            .foregroundStyle(.primary)
                        if person.isVip {
                            Image(systemName: "star.fill")
                                .font(.caption)
                                .foregroundStyle(.yellow)
                                .accessibilityLabel("VIP")
                        }
                        Spacer()
                    }
                    if !person.identities.isEmpty {
                        Text(identitySummary(person.identities))
                            .font(.caption)
                            .foregroundStyle(.secondary)
                            .lineLimit(2)
                    }
                }
                .padding(Spacing.sm)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(Color.phoneCanvas)
            }
            .buttonStyle(.plain)
            .sheet(isPresented: $showingDetail) {
                PersonDetailView(
                    personID: person.id,
                    store: store
                )
                .environmentObject(env)
            }
        }

        private func identitySummary(_ identities: [IdentityRow]) -> String {
            identities.map { "\($0.kind): \($0.value)" }
                .joined(separator: " · ")
        }
    }

    /// One canonical person's detail: identities + per-channel
    /// communication threads. The person record is re-fetched from the
    /// list already in the store; identities come from the §6 merged
    /// identity links (no raw content).
    struct PersonDetailView: View {
        let personID: String
        @ObservedObject var store: PersonalDataStore
        @EnvironmentObject var env: PhoneEnvironment
        @Environment(\.dismiss) private var dismiss

        init(personID: String, store: PersonalDataStore) {
            self.personID = personID
            self.store = store
        }

        var body: some View {
            NavigationStack {
                ScrollView {
                    if let person = store.people.first(where: { $0.id == personID }) {
                        VStack(alignment: .leading, spacing: Spacing.md) {
                            VipToggleButton(store: store, person: person)

                            // Identity block
                            VStack(alignment: .leading, spacing: 2) {
                                Text("Identities")
                                    .font(.headline)
                                if person.identities.isEmpty {
                                    Text("No normalized identity links recorded.")
                                        .font(.caption)
                                        .foregroundStyle(.secondary)
                                } else {
                                    ForEach(person.identities) { identity in
                                        HStack {
                                            Label(
                                                identity.value,
                                                systemImage: identitySymbol(identity.kind)
                                            )
                                            .font(.body)
                                            Spacer()
                                            Text(identity.kind)
                                                .font(.caption2)
                                                .foregroundStyle(.tertiary)
                                        }
                                    }
                                }
                            }

                            // Conversation threads (grouped from the store's
                            // inbox rows when the inbox is loaded).
                            VStack(alignment: .leading, spacing: 2) {
                                Text("Conversation threads")
                                    .font(.headline)
                                let threads = threadsFor(personID)
                                if threads.isEmpty {
                                    Text("No communications on any channel for this person.")
                                        .font(.caption)
                                        .foregroundStyle(.secondary)
                                } else {
                                    ForEach(threads) { thread in
                                        VStack(alignment: .leading, spacing: 2) {
                                            HStack {
                                                Text(thread.channel)
                                                    .font(.subheadline.weight(.medium))
                                                Spacer()
                                                Text("\(thread.communications.count) message\(thread.communications.count == 1 ? "" : "s")")
                                                    .font(.caption2)
                                                    .foregroundStyle(.tertiary)
                                            }
                                            ForEach(thread.communications.prefix(3)) { row in
                                                Text(row.summary.isEmpty ? row.channel : row.summary)
                                                    .font(.caption)
                                                    .foregroundStyle(.secondary)
                                                    .lineLimit(1)
                                            }
                                        }
                                        .padding(Spacing.xs)
                                    }
                                }
                            }
                        }
                        .padding(Spacing.md)
                    } else {
                        HonestEmptyRow(
                            title: "Person not found",
                            message: "This person is no longer in the local list."
                        )
                        .padding(Spacing.md)
                    }
                }
                .navigationTitle(personLabel)
                .inlineNavigationTitle()
                .toolbar {
                    ToolbarItem(placement: .primaryAction) {
                        Button("Done") { dismiss() }
                    }
                }
            }
        }

        private var personLabel: String {
            store.people.first(where: { $0.id == personID })?.displayLabel ?? "Person"
        }

        private func identitySymbol(_ kind: String) -> String {
            switch kind {
            case "email": return "envelope"
            case "phone": return "phone"
            case "imessage": return "message"
            case "voicemail": return "phone.badge.waveform"
            case "contact": return "person.crop.circle"
            default: return "person"
            }
        }

        private func threadsFor(_ personID: String) -> [CommunicationThread] {
            InboxGrouping.group(
                store.inboxRows.filter { $0.person == personID }
            )
        }
    }
}

// MARK: - VIP toggle button

/// A small VIP toggle that writes through `POST /api/people/{id}/vip`.
///
/// Optimistic: flips the local row immediately, then calls the
/// endpoint. On failure the row rolls back to the server-confirmed
/// value (the fetch service does not mutate the list on a failed
/// toggle). The toggle is disabled while in flight.
struct VipToggleButton: View {
    @ObservedObject var store: PersonalDataStore
    let person: PersonRow
    @EnvironmentObject var env: PhoneEnvironment

    init(store: PersonalDataStore, person: PersonRow) {
        self.store = store
        self.person = person
    }

    var body: some View {
        Button {
            toggle()
        } label: {
            HStack(spacing: Spacing.xs) {
                Image(systemName: person.isVip ? "star.fill" : "star")
                    .foregroundStyle(person.isVip ? .yellow : .secondary)
                Text(person.isVip ? "VIP" : "Mark VIP")
                    .font(.subheadline)
            }
        }
        .buttonStyle(.bordered)
        .tint(person.isVip ? .yellow : .secondary)
        .disabled(store.vipInFlight.contains(person.id))
        .accessibilityLabel(person.isVip ? "Remove VIP" : "Mark as VIP")
    }

    private func toggle() {
        guard let service = PersonalSurfaceRefresh.makeService(
            server: env.server,
            store: store
        ) else { return }
        store.markVipInFlight(person.id)
        let target = VipToggle.payloadValue(for: person)
        Task {
            let confirmed = await service.setVIP(personID: person.id, isVip: target)
            // On failure, the fetch service already removed the in-flight
            // marker and left the row untouched (server value is
            // authoritative). On success, it applied the confirmed value.
            _ = confirmed
        }
    }
}
