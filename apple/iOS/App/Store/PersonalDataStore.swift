import Foundation

import Combine
import NerveApps

// MARK: - §66 personal-surface data store
//
// Observable store for the five §66 personal views (Today / Inbox /
// People / Waiting / Briefing). Mirrors the engineering tabs'
// `PhoneDataStore` pattern: value-type rows + one `PersonalLoadState`
// per surface, all mutation on the main actor. Every row comes from a
// real backend endpoint (prompt2 §61/§62 honesty rule — no fabricated
// rows); a surface over a non-shipped / unreachable route lands in its
// honest `.error` state, never in demo data.
//
// The platform-free composition (Today rollup, inbox grouping, VIP
// toggle, waiting-status filter, briefing kinds) lives in
// `NerveApps.PersonalSurfaces` so it is unit-testable without the app
// target; this store only holds the observable render state.
public final class PersonalDataStore: ObservableObject {

    // MARK: - Today

    /// The composed Today rollup (attention + follow-ups + waiting-on),
    /// in backend rank order. Empty until the first successful fetch.
    @Published public private(set) var todayRows: [TodayRow] = []
    @Published public private(set) var todayState: PersonalLoadState = .empty

    /// Per-source availability for the Today screen's honest section
    /// labels. A source that failed to fetch is reported as such — the
    /// rollup simply omits it (no fabricated rows).
    @Published public private(set) var todaySourceErrors: [String: String] = [:]

    // MARK: - Inbox

    @Published public private(set) var inboxRows: [CommunicationRow] = []
    @Published public private(set) var inboxState: PersonalLoadState = .empty

    /// The person the inbox is filtered by (nil = all people).
    @Published public var inboxPersonFilter: String?
    /// The channel the inbox is filtered by (nil = all channels).
    @Published public var inboxChannelFilter: String?

    /// Detail state for the tapped communication (loaded on demand via
    /// `content_ref` — the list row never carries raw content, §7).
    @Published public private(set) var communicationDetail: CommunicationDetail?
    @Published public private(set) var communicationDetailState: PersonalLoadState = .empty

    // MARK: - People

    @Published public private(set) var people: [PersonRow] = []
    @Published public private(set) var peopleState: PersonalLoadState = .empty

    /// Optimistic VIP toggles in flight (person_id -> requested value).
    /// The view rolls the row back if the request fails.
    @Published public private(set) var vipInFlight: Set<String> = []

    // MARK: - Waiting

    @Published public private(set) var waitingRows: [WaitingRow] = []
    @Published public private(set) var waitingState: PersonalLoadState = .empty
    @Published public var waitingFilter: WaitingStatusFilter = .open

    // MARK: - Briefing

    @Published public private(set) var briefing: BriefingData?
    @Published public private(set) var briefingState: PersonalLoadState = .empty
    @Published public var briefingKind: BriefingKind = .morning

    public init() {}

    // MARK: - Public mutations (called by the fetch service / views)

    /// Replace the Today rollup. The fetch service composes rows via
    /// `TodayComposer.compose` before calling this, so the store stays
    /// dumb and the composition is unit-tested in `NerveApps`.
    public func setToday(
        rows: [TodayRow],
        sourceErrors: [String: String] = [:]
    ) {
        todayRows = rows
        todaySourceErrors = sourceErrors
        todayState = rows.isEmpty && sourceErrors.isEmpty ? .empty : .loaded
    }

    public func setInbox(_ rows: [CommunicationRow]) {
        inboxRows = rows
        inboxState = rows.isEmpty ? .empty : .loaded
    }

    public func setPeople(_ rows: [PersonRow]) {
        people = rows
        peopleState = rows.isEmpty ? .empty : .loaded
    }

    public func setWaiting(_ rows: [WaitingRow]) {
        waitingRows = rows
        waitingState = rows.isEmpty ? .empty : .loaded
    }

    public func setBriefing(_ data: BriefingData) {
        briefing = data
        // An `empty: true` brief is a *valid loaded* brief — the screen
        // renders the honest closing line, not an error.
        briefingState = .loaded
    }

    public func setCommunicationDetail(_ detail: CommunicationDetail?) {
        communicationDetail = detail
        communicationDetailState = detail == nil ? .empty : .loaded
    }

    /// Apply a server-confirmed VIP flag to a local row (called after a
    /// successful `POST /api/people/{id}/vip`).
    public func applyVip(confirmed: Bool, to personID: String) {
        guard let idx = people.firstIndex(where: { $0.id == personID }) else { return }
        people[idx] = VipToggle.apply(confirmed: confirmed, to: people[idx])
    }

    // MARK: - Error-state mutators

    /// Record a fetch failure for the Today rollup (all sources failed).
    public func setTodayError(_ message: String) {
        todayState = .error(message)
    }

    /// Record a fetch failure for the Inbox.
    public func setInboxError(_ message: String) {
        inboxState = .error(message)
    }

    /// Record a fetch failure for a communication detail load.
    public func setCommunicationDetailError(_ message: String) {
        communicationDetailState = .error(message)
    }

    /// Record a fetch failure for the People list.
    public func setPeopleError(_ message: String) {
        peopleState = .error(message)
    }

    /// Record a fetch failure for the Waiting queue.
    public func setWaitingError(_ message: String) {
        waitingState = .error(message)
    }

    /// Record a fetch failure for the Briefing.
    public func setBriefingError(_ message: String) {
        briefingState = .error(message)
    }

    // MARK: - VIP in-flight mutators

    /// Mark a person's VIP toggle as in flight (optimistic start).
    public func markVipInFlight(_ personID: String) {
        vipInFlight.insert(personID)
    }

    /// Clear a person's VIP in-flight marker (success or failure).
    public func clearVipInFlight(_ personID: String) {
        vipInFlight.remove(personID)
    }
}
