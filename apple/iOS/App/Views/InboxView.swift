import SwiftUI

import NerveApps
import NerveDesign

// MARK: - Inbox tab (§66)

/// Inbox — cross-channel communication history from
/// `GET /api/communications` (person / channel filters), grouped into
/// per-(person, channel) threads. Tapping a row loads the §7 record
/// behind the list row's `content_ref` — the list itself never carries
/// raw content, and the detail screen shows the normalized metadata +
/// the `content_ref` itself. We never render a body that was never
/// fetched (§61/§62 honesty rule).
public struct InboxView: View {
    @ObservedObject public var store: PersonalDataStore
    @EnvironmentObject public var env: PhoneEnvironment
    @State private var showingDetail = false
    @State private var selectedCommunicationID: String?

    public init(store: PersonalDataStore) {
        self.store = store
    }

    public var body: some View {
        NavigationStack {
            ScrollView {
                LazyVStack(alignment: .leading, spacing: Spacing.sm) {
                    switch store.inboxState {
                    case .loading:
                        LoadingRow(text: "Loading inbox…")
                    case .error(let message):
                        PersonalErrorRow(title: "Could not load Inbox", message: message)
                    case .empty, .loaded:
                        if store.inboxRows.isEmpty {
                            HonestEmptyRow(
                                title: "No communications",
                                message: "No normalized messages on any channel yet."
                            )
                        } else {
                            ForEach(threads) { thread in
                                ThreadHeader(thread: thread)
                                ForEach(thread.communications) { row in
                                    CommunicationRowView(row: row)
                                        .onTapGesture {
                                            selectedCommunicationID = row.id
                                            showingDetail = true
                                            loadDetail(for: row.id)
                                        }
                                }
                            }
                        }
                    }
                }
                .padding(.horizontal, Spacing.md)
                .padding(.top, Spacing.sm)
                .padding(.bottom, Spacing.lg)
            }
            .background(Color.phoneCanvas.ignoresSafeArea())
            .navigationTitle("Inbox")
            .toolbar {
                ToolbarItem(placement: .primaryAction) {
                    ChannelFilterMenu(store: store)
                }
            }
            .onAppear { refresh() }
            .refreshable { await refresh() }
            .sheet(isPresented: $showingDetail) {
                if let id = selectedCommunicationID,
                   let detail = store.communicationDetail,
                   detail.id == id {
                    CommunicationDetailView(
                        detail: detail,
                        detailState: store.communicationDetailState
                    )
                }
            }
            .accessibilityIdentifier("phone.tab.inbox")
        }
    }

    private func refresh() {
        guard let service = PersonalSurfaceRefresh.makeService(
            server: env.server,
            store: store
        ) else { return }
        Task {
            await service.fetchInbox(
                personID: store.inboxPersonFilter,
                channel: store.inboxChannelFilter
            )
        }
    }

    private func loadDetail(for id: String) {
        guard let service = PersonalSurfaceRefresh.makeService(
            server: env.server,
            store: store
        ) else { return }
        Task { await service.fetchCommunication(id) }
    }

    /// Group the flat row list into per-(person, channel) threads,
    /// most-recent thread first (platform-free logic in `NerveApps`).
    private var threads: [CommunicationThread] {
        InboxGrouping.group(store.inboxRows)
    }
}

// MARK: - Rows

extension InboxView {
    struct ThreadHeader: View {
        let thread: CommunicationThread
        var body: some View {
            HStack {
                Text(thread.person ?? "Unattributed")
                    .font(.headline)
                Spacer()
                Label(thread.channel, systemImage: channelSymbol(thread.channel))
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            }
            .padding(.top, Spacing.xs)
        }

        private func channelSymbol(_ channel: String) -> String {
            switch channel.lowercased() {
            case "email": return "envelope"
            case "imessage": return "message"
            case "sms": return "message.fill"
            case "rcs": return "bubble.left.bubble.right"
            case "voicemail": return "phone"
            default: return "ellipsis.bubble"
            }
        }
    }

    struct CommunicationRowView: View {
        let row: CommunicationRow

        var body: some View {
            VStack(alignment: .leading, spacing: 2) {
                HStack(alignment: .firstTextBaseline) {
                    Text(row.summary.isEmpty ? row.channel : row.summary)
                        .font(.body)
                        .foregroundStyle(.primary)
                        .lineLimit(1)
                    Spacer()
                    Text(directionLabel(row.direction))
                        .font(.caption2)
                        .foregroundStyle(row.direction == "incoming" ? AnyShapeStyle(.secondary) : AnyShapeStyle(.blue))
                    if let at = row.at {
                        Text(atLabel(at))
                            .font(.caption2)
                            .foregroundStyle(.tertiary)
                    }
                }
                HStack(spacing: Spacing.xxs) {
                    Label(
                        "ref \(row.contentRef)",
                        systemImage: "link"
                    )
                    .font(.caption2)
                    .foregroundStyle(.tertiary)
                    .lineLimit(1)
                }
            }
            .padding(.vertical, Spacing.xs)
            .padding(.horizontal, Spacing.xs)
            .contentShape(Rectangle())
        }

        private func directionLabel(_ direction: String) -> String {
            direction == "incoming" ? "in" : "out"
        }

        private func atLabel(_ date: Date) -> String {
            let f = DateFormatter()
            f.dateStyle = .short
            f.timeStyle = .short
            return f.string(from: date)
        }
    }
}

// MARK: - Detail sheet

/// The tap-through detail for one communication. Shows the normalized §7
/// metadata + the `content_ref` pointer. The raw payload lives behind
/// `content_ref` (encrypted, out-of-graph, §7); no content-fetch path is
/// wired, so we show the reference and never a body we did not fetch.
public struct CommunicationDetailView: View {
    let detail: CommunicationDetail
    let detailState: PersonalLoadState
    @EnvironmentObject var env: PhoneEnvironment
    @Environment(\.dismiss) private var dismiss

    public init(detail: CommunicationDetail, detailState: PersonalLoadState) {
        self.detail = detail
        self.detailState = detailState
    }

    public var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: Spacing.md) {
                    switch detailState {
                    case .loading:
                        LoadingRow(text: "Loading detail…")
                    case .error(let message):
                        PersonalErrorRow(title: "Could not load detail", message: message)
                    case .empty:
                        HonestEmptyRow(
                            title: "No detail",
                            message: "Nothing to show."
                        )
                    case .loaded:
                        DetailField(label: "From", value: detail.person ?? "Unattributed")
                        DetailField(label: "Channel", value: detail.channel)
                        DetailField(label: "Direction", value: detail.direction)
                        if let at = detail.at {
                            DetailField(label: "When", value: atLabel(at))
                        }
                        DetailField(label: "Summary", value: detail.summary)
                        DetailField(label: "Classification", value: detail.classification.isEmpty ? "Not recorded" : detail.classification)
                        DetailField(label: "Sensitivity", value: detail.sensitivity)
                        VStack(alignment: .leading, spacing: 2) {
                            Text("Content reference")
                                .font(.caption)
                                .foregroundStyle(.secondary)
                            Text(detail.contentRef)
                                .font(.body.monospaced())
                                .textSelection(.enabled)
                            Text("The raw payload is stored behind this reference and is not fetched here.")
                                .font(.caption2)
                                .foregroundStyle(.tertiary)
                        }
                        .padding(Spacing.sm)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .background(Color.phoneCanvas)
                    }
                }
                .padding(Spacing.md)
            }
            .navigationTitle("Communication")
            .inlineNavigationTitle()
            .toolbar {
                ToolbarItem(placement: .primaryAction) {
                    Button("Done") { dismiss() }
                }
            }
        }
    }

    private func atLabel(_ date: Date) -> String {
        let f = DateFormatter()
        f.dateStyle = .medium
        f.timeStyle = .short
        return f.string(from: date)
    }
}

// MARK: - Detail field

struct DetailField: View {
    let label: String
    let value: String
    init(label: String, value: String) {
        self.label = label
        self.value = value
    }
    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(label)
                .font(.caption)
                .foregroundStyle(.secondary)
            Text(value)
                .font(.body)
                .foregroundStyle(.primary)
        }
    }
}

// MARK: - Channel filter menu

struct ChannelFilterMenu: View {
    @ObservedObject var store: PersonalDataStore
    @EnvironmentObject var env: PhoneEnvironment

    private let channels = [nil as String?, "email", "imessage", "sms", "rcs", "voicemail"]

    var body: some View {
        Menu {
            ForEach(channels, id: \.self) { channel in
                Button(channel ?? "All channels") {
                    store.inboxChannelFilter = channel
                    if let service = PersonalSurfaceRefresh.makeService(server: env.server, store: store) {
                        Task {
                            await service.fetchInbox(personID: store.inboxPersonFilter, channel: channel)
                        }
                    }
                }
            }
        } label: {
            Label("Channel", systemImage: "line.3.horizontal.decrease.circle")
                .font(.caption)
        }
    }
}
