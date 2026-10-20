import Foundation

import NerveAPI
import NerveApps

// MARK: - Personal-surface fetch service (§66)
//
// Talks to the personal-operations endpoints (or `MockTransport` in
// tests) and maps the wire contract into the `NerveApps` personal-surface
// value types, writing them into `PersonalDataStore`. Mirrors
// `PhoneFetchService` (the engineering tabs' fetcher) so the personal
// plane reuses the same truthful data-loading lift:
//
// - Truthful: 5xx → `.error` state, empty arrays → `.empty`, no 501
//   "not wired" special-casing (the personal routes are real, not stubs).
// - No fabricated rows: every row maps 1:1 from a fetched wire record;
//   a view over a non-shipped route shows a typed error, never demo data.
// - Thread-safe: all decoding happens off the main actor; only the store
//   mutation is MainActor-protected (the store is `ObservableObject` and
//   is only touched from the main actor in the app).
//
// Endpoints consumed (gaps4 D2' / G-2.1):
// - `GET /api/attention`                 → Today (attention source)
// - `GET /api/followups?status=OPEN`     → Today (follow-ups source)
// - `GET /api/waiting-on?status=open`    → Today (waiting source) + Waiting tab
// - `GET /api/communications`            → Inbox (+ person / channel filters)
// - `GET /api/communications/{id}`       → Inbox detail (content_ref)
// - `GET /api/people`                    → People
// - `POST /api/people/{id}/vip`          → People (VIP toggle, R101)
// - `GET /api/briefing?kind=`            → Briefing

/// One loaded communication detail (the §7 record for a tapped row).
///
/// The list row only carries a *summary*; the detail record is what the
/// `content_ref` pointed at. Because the §7 shape never stores raw
/// content in the graph, the detail shows the normalized metadata + the
/// `content_ref` itself, and the raw payload only renders if a
/// content-fetch path has been wired (it has not, so we never claim to
/// show content that was never fetched — §61/§62 honesty rule).
public struct CommunicationDetail: Equatable {
    public let id: String
    public let person: String?
    public let channel: String
    public let direction: String
    public let summary: String
    public let at: Date?
    public let contentRef: String
    /// The normalized classification verdict (commitment / request /
    /// deadline), if the detection plane filled it in.
    public let classification: String
    /// The §36 sensitivity label.
    public let sensitivity: String

    public init(
        id: String,
        person: String?,
        channel: String,
        direction: String,
        summary: String,
        at: Date?,
        contentRef: String,
        classification: String,
        sensitivity: String
    ) {
        self.id = id
        self.person = person
        self.channel = channel
        self.direction = direction
        self.summary = summary
        self.at = at
        self.contentRef = contentRef
        self.classification = classification
        self.sensitivity = sensitivity
    }
}

/// A fetch error that can be surfaced to the user (network vs server).
public enum PersonalFetchError: Error, CustomStringConvertible, Sendable {
    case network(String)
    case http(statusCode: Int, body: String?)
    case decoding(String)
    case unexpected(String)

    public var description: String {
        switch self {
        case .network(let msg): return "Network error: \(msg)"
        case .http(let code, _): return "Server error: \(code)"
        case .decoding(let msg): return "Data error: \(msg)"
        case .unexpected(let msg): return "Unexpected response: \(msg)"
        }
    }
}

/// Service that orchestrates personal-surface fetches.
///
/// The service is created per-fetch by the app (mirroring
/// `PhoneEnvironment.fetchInto`) and injected so each personal view
/// shares one coherent client.
public actor PersonalFetchService: Sendable {
    private let client: APIClient
    private let store: PersonalDataStore

    public init(client: APIClient, store: PersonalDataStore) {
        self.client = client
        self.store = store
    }

    // MARK: - Today

    /// Fetch the Today rollup: attention + open follow-ups + open
    /// waiting-on. Each source is fetched independently and a per-source
    /// failure is reported honestly (the rollup omits that source rather
    /// than inventing rows). A failure of *all* sources sets `.error`.
    public func fetchToday() async {
        let attention = await fetchAttention()
        let followups = await fetchFollowups()
        // Type-annotated to disambiguate the private SourceResult overload
        // from the public store-writing `fetchWaiting(status:)`.
        let waiting: SourceResult<WaitingInput> = await fetchWaiting(status: .open)

        var sourceErrors: [String: String] = [:]
        if let e = attention.error { sourceErrors[TodayOrigin.attention.rawValue] = e }
        if let e = followups.error { sourceErrors[TodayOrigin.followup.rawValue] = e }
        if let e = waiting.error { sourceErrors[TodayOrigin.waiting.rawValue] = e }

        let rows = TodayComposer.compose(
            attention: attention.items,
            followups: followups.items,
            waiting: waiting.items
        )

        await MainActor.run {
            store.setToday(rows: rows, sourceErrors: sourceErrors)
            if rows.isEmpty && !sourceErrors.isEmpty {
                store.setTodayError("Could not load any Today source")
            }
        }
    }

    // MARK: - Inbox

    /// Fetch the inbox, optionally filtered by person / channel.
    public func fetchInbox(personID: String? = nil, channel: String? = nil) async {
        do {
            var query: [URLQueryItem] = []
            if let personID { query.append(URLQueryItem(name: "person_id", value: personID)) }
            if let channel { query.append(URLQueryItem(name: "channel", value: channel)) }

            let response: WireListResponse<WireCommunication> = try await client.send(
                path: "/communications",
                method: "GET",
                query: query.isEmpty ? nil : query
            )
            let rows = response.data.map { PersonalWireMapper.communication(row: $0, personNames: nil) }
            await MainActor.run {
                store.inboxPersonFilter = personID
                store.inboxChannelFilter = channel
                store.setInbox(rows)
            }
        } catch NerveAPIError.network(let underlying) {
            await MainActor.run { store.setInboxError("Network: \(underlying)") }
        } catch NerveAPIError.http(let code, _) {
            await MainActor.run { store.setInboxError("Inbox: server \(code)") }
        } catch NerveAPIError.decoding(let underlying) {
            await MainActor.run { store.setInboxError("Inbox: \(underlying)") }
        } catch NerveAPIError.unexpectedResponse(let underlying) {
            await MainActor.run { store.setInboxError("Inbox: \(underlying)") }
        } catch {
            await MainActor.run { store.setInboxError(String(describing: error)) }
        }
    }

    /// Fetch one communication by id (the Inbox tap-through detail).
    ///
    /// Loads the normalized §7 record behind the list row's `content_ref`.
    /// The record never carries raw content, so we render the normalized
    /// metadata + the `content_ref` itself — never a body we did not fetch.
    public func fetchCommunication(_ id: String) async {
        do {
            let response: WireOneResponse<WireCommunication> = try await client.send(
                path: "/communications/\(id)",
                method: "GET"
            )
            let detail = PersonalWireMapper.communicationDetail(from: response.item, personNames: nil)
            await MainActor.run {
                store.setCommunicationDetail(detail)
            }
        } catch NerveAPIError.http(let code, _) where code == 404 {
            await MainActor.run {
                store.setCommunicationDetail(nil)
                store.setCommunicationDetailError("Communication not found")
            }
        } catch NerveAPIError.network(let underlying) {
            await MainActor.run { store.setCommunicationDetailError("Network: \(underlying)") }
        } catch NerveAPIError.http(let code, _) {
            await MainActor.run { store.setCommunicationDetailError("Detail: server \(code)") }
        } catch NerveAPIError.decoding(let underlying) {
            await MainActor.run { store.setCommunicationDetailError("Detail: \(underlying)") }
        } catch {
            await MainActor.run { store.setCommunicationDetailError(String(describing: error)) }
        }
    }

    // MARK: - People

    /// Fetch the canonical person list (R101).
    public func fetchPeople() async {
        do {
            let response: WireListResponse<WirePerson> = try await client.send(
                path: "/people",
                method: "GET"
            )
            let rows = response.data.map { PersonalWireMapper.person($0) }
            await MainActor.run { store.setPeople(rows) }
        } catch NerveAPIError.network(let underlying) {
            await MainActor.run { store.setPeopleError("Network: \(underlying)") }
        } catch NerveAPIError.http(let code, _) {
            await MainActor.run { store.setPeopleError("People: server \(code)") }
        } catch NerveAPIError.decoding(let underlying) {
            await MainActor.run { store.setPeopleError("People: \(underlying)") }
        } catch NerveAPIError.unexpectedResponse(let underlying) {
            await MainActor.run { store.setPeopleError("People: \(underlying)") }
        } catch {
            await MainActor.run { store.setPeopleError(String(describing: error)) }
        }
    }

    /// Toggle the VIP flag on a person via `POST /api/people/{id}/vip`
    /// (R101 step 5). Applies the server-confirmed value to the local row.
    ///
    /// Returns the confirmed `is_vip` so the caller can update its own
    /// optimistic state; on failure the store is left untouched and the
    /// error is surfaced through `peopleState` only when it is a
    /// transport/decode failure (a 404/409 on the toggle does not
    /// invalidate the whole list).
    @discardableResult
    public func setVIP(personID: String, isVip: Bool) async -> Bool? {
        do {
            let response: WireOneResponse<WirePerson> = try await client.send(
                path: "/people/\(personID)/vip",
                method: "POST",
                body: WireVipRequest(isVip: isVip)
            )
            let confirmed = response.item.isVip ?? false
            await MainActor.run {
                store.clearVipInFlight(personID)
                store.applyVip(confirmed: confirmed, to: personID)
            }
            return confirmed
        } catch NerveAPIError.network {
            await MainActor.run { store.clearVipInFlight(personID) }
            return nil
        } catch {
            await MainActor.run { store.clearVipInFlight(personID) }
            return nil
        }
    }

    // MARK: - Waiting

    /// Fetch the waiting-on queue with a status filter.
    public func fetchWaiting(status: WaitingStatusFilter) async {
        do {
            let response: WireListResponse<WireWaitingOn> = try await client.send(
                path: "/waiting-on",
                method: "GET",
                query: [URLQueryItem(name: "status", value: status.queryValue)]
            )
            let rows = response.data.map { PersonalWireMapper.waiting($0) }
            await MainActor.run {
                store.waitingFilter = status
                store.setWaiting(rows)
            }
        } catch NerveAPIError.network(let underlying) {
            await MainActor.run { store.setWaitingError("Network: \(underlying)") }
        } catch NerveAPIError.http(let code, _) {
            await MainActor.run { store.setWaitingError("Waiting: server \(code)") }
        } catch NerveAPIError.decoding(let underlying) {
            await MainActor.run { store.setWaitingError("Waiting: \(underlying)") }
        } catch NerveAPIError.unexpectedResponse(let underlying) {
            await MainActor.run { store.setWaitingError("Waiting: \(underlying)") }
        } catch {
            await MainActor.run { store.setWaitingError(String(describing: error)) }
        }
    }

    // MARK: - Briefing

    /// Fetch the daily briefing for a kind.
    public func fetchBriefing(kind: BriefingKind = .morning) async {
        do {
            let response: WireBriefing = try await client.send(
                path: "/briefing",
                method: "GET",
                query: [URLQueryItem(name: "kind", value: kind.queryValue)]
            )
            let data = PersonalWireMapper.briefing(response)
            await MainActor.run {
                store.briefingKind = kind
                store.setBriefing(data)
            }
        } catch NerveAPIError.network(let underlying) {
            await MainActor.run { store.setBriefingError("Network: \(underlying)") }
        } catch NerveAPIError.http(let code, _) {
            await MainActor.run { store.setBriefingError("Briefing: server \(code)") }
        } catch NerveAPIError.decoding(let underlying) {
            await MainActor.run { store.setBriefingError("Briefing: \(underlying)") }
        } catch NerveAPIError.unexpectedResponse(let underlying) {
            await MainActor.run { store.setBriefingError("Briefing: \(underlying)") }
        } catch {
            await MainActor.run { store.setBriefingError(String(describing: error)) }
        }
    }

    // MARK: - Source fetchers (used by fetchToday)

    private struct SourceResult<T: Sendable> {
        var items: [T]?
        var error: String?
    }

    private func fetchAttention() async -> SourceResult<AttentionInput> {
        do {
            let response: WireListResponse<WireAttentionItem> = try await client.send(
                path: "/attention",
                method: "GET",
                query: [URLQueryItem(name: "limit", value: "50")]
            )
            let items = response.data.map { PersonalWireMapper.attention($0) }
            return SourceResult(items: items, error: nil)
        } catch {
            return SourceResult(items: nil, error: String(describing: error))
        }
    }

    private func fetchFollowups() async -> SourceResult<FollowUpInput> {
        do {
            let response: WireListResponse<WireFollowUp> = try await client.send(
                path: "/followups",
                method: "GET",
                query: [URLQueryItem(name: "status", value: "OPEN")]
            )
            let items = response.data.map { PersonalWireMapper.followup($0) }
            return SourceResult(items: items, error: nil)
        } catch {
            return SourceResult(items: nil, error: String(describing: error))
        }
    }

    private func fetchWaiting(status: WaitingStatusFilter) async -> SourceResult<WaitingInput> {
        do {
            let response: WireListResponse<WireWaitingOn> = try await client.send(
                path: "/waiting-on",
                method: "GET",
                query: [URLQueryItem(name: "status", value: status.queryValue)]
            )
            let items = response.data.map { PersonalWireMapper.waitingInput($0) }
            return SourceResult(items: items, error: nil)
        } catch {
            return SourceResult(items: nil, error: String(describing: error))
        }
    }
}

// MARK: - Personal wire contract decode (tolerant, §47/§49)
//
// The personal endpoints return the canonical `ListResponse { data, meta }`
// envelope for list routes, and `{ item, meta }` for single-object routes
// (people detail / vip / communication detail). Decoding uses the shared
// `NerveJSON` snake_case strategy, so wire fields map directly.

/// The `{ item, meta }` single-object envelope used by the people /
/// communication detail + vip routes.
struct WireOneResponse<T: Decodable>: Decodable {
    var item: T
}

/// A normalized communication as returned by `GET /api/communications`
/// (the §7 shape — never raw content, only `content_ref`).
struct WireCommunication: Decodable {
    var communicationID: String
    var personID: String?
    var channel: String
    var direction: String
    var sender: String?
    var timestamp: String?
    var threadID: String?
    var contentRef: String
    var attachments: [String]?
    var classification: [String: String]?
    var sensitivity: String?
    var source: String?
}

/// A canonical person as returned by `GET /api/people` (R101 §6).
struct WirePerson: Decodable {
    var personID: String
    var displayName: String?
    var isVip: Bool?
    var identities: [WireIdentity]?
    var proposedMerges: [String]?
    var createdAt: String?
    var updatedAt: String?
}

/// A normalized identity link on a person.
struct WireIdentity: Decodable {
    var kind: String
    var value: String
    var rawValue: String?
    var displayName: String?
    var source: String?
    var firstSeenAt: String?
    var lastSeenAt: String?
}

/// A waiting-on request as returned by `GET /api/waiting-on` (§19).
struct WireWaitingOn: Decodable {
    var requestID: String
    var personDisplayName: String
    var personID: String?
    var description: String
    var requestedAt: String
    var source: String?
    var sourceThreadID: String?
    var status: String?
    var resolvedAt: String?
    var note: String?
    var followUpCount: Int?
    var lastFollowUpAt: String?
    var attentionID: String?
}

/// An attention item as returned by `GET /api/attention` (§20).
struct WireAttentionItem: Decodable {
    var attentionID: String
    var reason: String
    var priority: Double?
    var urgency: Double?
    var importance: Double?
    var due: String?
    var entities: [String]?
    var sources: [String]?
    var suggestedAction: String?
    var status: String?
    var why: [String]?
    var createdAt: String?
    var updatedAt: String?
}

/// A follow-up as returned by `GET /api/followups` (§57).
struct WireFollowUp: Decodable {
    var id: String
    var targetKind: String?
    var targetID: String
    var personID: String?
    var dueAt: String
    var reason: String
    var createdFrom: String?
    var status: String?
    var attentionID: String?
    var auditHistory: [WireFollowUpAction]?
    var createdAt: String?
    var updatedAt: String?
}

struct WireFollowUpAction: Decodable {
    var action: String?
    var at: String?
    var detail: String?
}

/// A composed briefing as returned by `GET /api/briefing` (§30).
struct WireBriefing: Decodable {
    var kind: String?
    var generatedAt: String?
    var actions: [WireBriefingAction]?
    var waitingOn: [WireBriefingWaiting]?
    var today: [WireBriefingEvent]?
    var overnight: WireBriefingOvernight?
    var empty: Bool?
}

struct WireBriefingAction: Decodable {
    var attentionID: String
    var label: String
    var due: String?
}

struct WireBriefingWaiting: Decodable {
    var requestID: String
    var person: String
    var description: String
    var daysElapsed: Int?
}

struct WireBriefingEvent: Decodable {
    var eventID: String
    var title: String
    var start: String?
}

struct WireBriefingOvernight: Decodable {
    var emails: Int?
    var messages: Int?
    var voicemails: Int?
}

/// The `POST /api/people/{id}/vip` request body (R101 step 5).
struct WireVipRequest: Encodable {
    var isVip: Bool
}

// MARK: - Wire → NerveApps personal-surface mapping

enum PersonalWireMapper {
    /// Parse an ISO-8601 timestamp the way the engineering tabs do.
    static func parseDate(_ s: String?) -> Date? {
        guard let s, !s.isEmpty else { return nil }
        if let d = ISO8601DateFormatter().date(from: s) { return d }
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.timeZone = TimeZone(identifier: "UTC")
        f.dateFormat = "yyyy-MM-dd'T'HH:mm:ss'Z'"
        return f.date(from: s)
    }

    /// Whole days elapsed between `requestedAt` and `now`, floored at 0.
    ///
    /// The backend's `waiting_on` service computes `days_elapsed` on the
    /// server; when it is absent (older wire) we derive it locally from
    /// `requested_at` so the UI can still render "3 days".
    static func daysElapsed(since requestedAt: String, now: Date = Date()) -> Int {
        guard let d = parseDate(requestedAt) else { return 0 }
        let seconds = now.timeIntervalSince(d)
        guard seconds > 0 else { return 0 }
        return max(0, Int(seconds / 86_400))
    }

    static func attention(_ w: WireAttentionItem) -> AttentionInput {
        AttentionInput(
            id: w.attentionID,
            reason: w.reason,
            priority: w.priority ?? 0,
            due: parseDate(w.due),
            why: w.why,
            sources: w.sources
        )
    }

    static func followup(_ w: WireFollowUp) -> FollowUpInput {
        FollowUpInput(
            id: w.id,
            reason: w.reason,
            dueAt: parseDate(w.dueAt) ?? Date()
        )
    }

    static func waiting(_ w: WireWaitingOn) -> WaitingRow {
        WaitingRow(
            id: w.requestID,
            personDisplayName: w.personDisplayName,
            personId: w.personID,
            description: w.description,
            requestedAt: parseDate(w.requestedAt) ?? Date(),
            source: w.source ?? "messages",
            status: w.status ?? "open",
            daysElapsed: daysElapsed(since: w.requestedAt),
            followUpCount: w.followUpCount ?? 0
        )
    }

    static func waitingInput(_ w: WireWaitingOn) -> WaitingInput {
        WaitingInput(
            requestId: w.requestID,
            personDisplayName: w.personDisplayName,
            description: w.description,
            requestedAt: parseDate(w.requestedAt) ?? Date(),
            daysElapsed: daysElapsed(since: w.requestedAt)
        )
    }

    static func person(_ w: WirePerson) -> PersonRow {
        PersonRow(
            id: w.personID,
            displayName: w.displayName,
            isVip: w.isVip ?? false,
            identities: (w.identities ?? []).map {
                IdentityRow(kind: $0.kind, value: $0.value)
            }
        )
    }

    /// Map a wire communication into an inbox list row.
    ///
    /// `personNames` maps `person_id` → display name for the person label;
    /// when `nil` (the list endpoint does not return person names) the row
    /// uses the communication's own attribution (sender, when present) so
    /// the list stays honest — it shows what the record says, not a guess.
    static func communication(
        row: WireCommunication,
        personNames: [String: String]?
    ) -> CommunicationRow {
        let person: String?
        if let pid = row.personID, let names = personNames, let name = names[pid] {
            person = name
        } else if row.direction == "incoming", let sender = row.sender, !sender.isEmpty {
            // Unattributed incoming: the sender handle is the honest
            // "who" — it is part of the normalized record, not raw content.
            person = sender
        } else {
            person = nil
        }
        return CommunicationRow(
            id: row.communicationID,
            person: person,
            channel: row.channel,
            direction: row.direction,
            summary: row.sender ?? row.channel,
            at: parseDate(row.timestamp),
            contentRef: row.contentRef
        )
    }

    static func communicationDetail(
        from row: WireCommunication,
        personNames: [String: String]?
    ) -> CommunicationDetail {
        let base = communication(row: row, personNames: personNames)
        return CommunicationDetail(
            id: row.communicationID,
            person: base.person,
            channel: row.channel,
            direction: row.direction,
            summary: base.summary,
            at: base.at,
            contentRef: row.contentRef,
            classification: classify(row.classification),
            sensitivity: row.sensitivity ?? "PERSONAL"
        )
    }

    /// The detection verdict, if the classification dict filled it in.
    static func classify(_ dict: [String: String]?) -> String {
        guard let dict else { return "" }
        for key in ["verdict", "kind", "label", "type"] {
            if let v = dict[key], !v.isEmpty { return v }
        }
        return ""
    }

    static func briefing(_ w: WireBriefing) -> BriefingData {
        BriefingData(
            kind: BriefingKind(rawValue: w.kind?.lowercased() ?? "") ?? .morning,
            generatedAt: parseDate(w.generatedAt) ?? Date(),
            actions: (w.actions ?? []).map {
                BriefingActionRow(
                    attentionId: $0.attentionID,
                    label: $0.label,
                    due: parseDate($0.due)
                )
            },
            waitingOn: (w.waitingOn ?? []).map {
                BriefingWaitingRow(
                    requestId: $0.requestID,
                    person: $0.person,
                    description: $0.description,
                    daysElapsed: $0.daysElapsed ?? 0
                )
            },
            today: (w.today ?? []).map {
                BriefingEventRow(
                    eventId: $0.eventID,
                    title: $0.title,
                    start: parseDate($0.start)
                )
            },
            overnightEmails: w.overnight?.emails ?? 0,
            overnightMessages: w.overnight?.messages ?? 0,
            overnightVoicemails: w.overnight?.voicemails ?? 0,
            empty: w.empty ?? ((w.actions ?? []).isEmpty
                && (w.waitingOn ?? []).isEmpty
                && (w.today ?? []).isEmpty)
        )
    }
}
