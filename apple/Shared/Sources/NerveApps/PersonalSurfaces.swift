import Foundation

// MARK: - §66 Personal surfaces (prompt2 §66) — platform-free core
//
// The five iPhone personal views (Today / Inbox / People / Waiting /
// Briefing) render rows that come ONLY from real backend endpoints
// (prompt2 §61/§62 honesty rule; gaps4 D2'). This file holds the
// platform-free logic — date bucketing, inbox grouping, attention ranking
// for the Today rollup — so the view layer stays a thin SwiftUI shell and
// the interesting behaviour is unit-testable without a simulator.
//
// No fabricated rows: every type here is either an empty value type or
// populated from a fetched wire record. The `*State` machine mirrors the
// engineering tabs' `LoadState` so the UI can show honest loading /
// empty / error / loaded states.

// MARK: - Load state (mirrors the engineering tabs' LoadState)

/// Load / empty / error / loaded state for a single personal surface.
///
/// Deliberately separate from `PhoneDataStore.LoadState` (which lives in the
/// app target) so the personal plane's logic is testable in `NerveApps`
/// without importing the iOS app target.
public enum PersonalLoadState: Equatable, Sendable {
    case loading
    case empty
    case error(String)
    case loaded
}

// MARK: - Today (prompt2 §66 — attention-ranked rollup)
//
// Today is a rollup of "what is on me today": the attention feed, open
// follow-ups, and waiting-on requests. The three sources are fetched
// independently (each endpoint is truthful on its own), so the composer
// must handle any subset being present, and must never invent rows to
// fill in a missing source.

/// One renderable row in the Today rollup.
///
/// A row is either a direct projection of an attention item, a follow-up,
/// or a waiting-on request. `origin` records which source produced it
/// (provenance — §71 auditability) and `date` is the bucketing key.
public struct TodayRow: Identifiable, Hashable, Sendable {
    /// Stable row identity (the source's own id + origin, so a re-fetch
    /// with the same data produces the same rows).
    public let id: String
    /// Human label — the thing that needs attention.
    public let label: String
    /// Secondary line — provenance / detail (e.g. "waiting on Sam · 3 days").
    public let detail: String?
    /// Which source produced the row (prompt2 §71 auditability).
    public let origin: TodayOrigin
    /// The bucketing date (due date / created date), if known.
    public let date: Date?
    /// Priority 0...1 from the attention scorer (0 when the source has
    /// no priority, e.g. a waiting-on request).
    public let priority: Double

    public init(
        id: String,
        label: String,
        detail: String? = nil,
        origin: TodayOrigin,
        date: Date? = nil,
        priority: Double = 0
    ) {
        self.id = id
        self.label = label
        self.detail = detail
        self.origin = origin
        self.date = date
        self.priority = priority
    }
}

/// Which personal source a Today row came from (prompt2 §71 auditability).
public enum TodayOrigin: String, CaseIterable, Sendable {
    case attention = "attention"
    case followup = "followup"
    case waiting = "waiting"

    public var displayName: String {
        switch self {
        case .attention: return "Attention"
        case .followup: return "Follow-up"
        case .waiting: return "Waiting on"
        }
    }
}

// MARK: - Today composer (platform-free, deterministic)

/// Composes the Today rollup from the three fetched sources.
///
/// Deterministic + pure: same inputs → same rows. The composer is the
/// single place where ranking + dedup happens, so the UI never has to
/// reason about "which source wins" and the behaviour is unit-testable.
public enum TodayComposer {
    /// Compose the ranked Today rollup.
    ///
    /// Ranking (prompt2 §21/§22 — deterministic, explainable, no opaque ML):
    /// 1. Attention items first — they are already scored by the backend's
    ///    AttentionService (priority + urgency), so we preserve their
    ///    backend-returned order (highest priority first).
    /// 2. Follow-ups next, most-due first (the backend already sorts by
    ///    `due_at` ascending; we keep that order).
    /// 3. Waiting-on requests last, most-waiting first (the backend sorts
    ///    by days-elapsed descending).
    ///
    /// Each source is optional; a missing source contributes nothing (no
    /// fabricated rows). The result is the concatenation in the order
    /// above, so the UI can render section headers ("Needs you /
    /// Due / Waiting on") without re-ranking.
    ///
    /// - Parameters:
    ///   - attention: attention items in backend rank order (may be empty /
    ///     `nil` when the source is unavailable).
    ///   - followups: open follow-ups, most-due first.
    ///   - waiting: waiting-on requests, most-waiting first.
    /// - Returns: the composed, ordered rollup. Empty when all sources are
    ///   empty (the UI shows the honest empty state).
    public static func compose(
        attention: [AttentionInput]?,
        followups: [FollowUpInput]?,
        waiting: [WaitingInput]?
    ) -> [TodayRow] {
        var rows: [TodayRow] = []

        if let attention, !attention.isEmpty {
            for item in attention {
                rows.append(TodayRow(
                    id: "attention.\(item.id)",
                    label: item.reason,
                    detail: detailLine(for: item.why, sources: item.sources),
                    origin: .attention,
                    date: item.due,
                    priority: item.priority
                ))
            }
        }

        if let followups, !followups.isEmpty {
            for item in followups {
                rows.append(TodayRow(
                    id: "followup.\(item.id)",
                    label: item.reason,
                    detail: detailLine(for: [duePhrase(item.dueAt)], sources: nil),
                    origin: .followup,
                    date: item.dueAt,
                    priority: 0
                ))
            }
        }

        if let waiting, !waiting.isEmpty {
            for item in waiting {
                rows.append(TodayRow(
                    id: "waiting.\(item.requestId)",
                    label: item.description,
                    detail: "waiting on \(item.personDisplayName) · \(daysPhrase(item.daysElapsed))",
                    origin: .waiting,
                    date: item.requestedAt,
                    priority: 0
                ))
            }
        }

        return rows
    }

    /// A short, human detail line from explainability fragments.
    ///
    /// Returns `nil` when there is nothing to say (honest — no
    /// fabricated "no details" filler).
    private static func detailLine(for why: [String]?, sources: [String]?) -> String? {
        let parts: [String]
        if let why, !why.isEmpty {
            parts = why
        } else if let sources, !sources.isEmpty {
            parts = sources
        } else {
            return nil
        }
        return parts.joined(separator: " · ")
    }

    /// "due 2026-10-21" style framing for a follow-up due date.
    private static func duePhrase(_ date: Date) -> String {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.timeZone = TimeZone(identifier: "UTC")
        f.dateStyle = .medium
        f.timeStyle = .none
        return "due \(f.string(from: date))"
    }

    /// "3 days" / "1 day" / "today" framing for waiting-on elapsed time.
    public static func daysPhrase(_ days: Int) -> String {
        switch days {
        case 0: return "today"
        case 1: return "1 day"
        default: return "\(days) days"
        }
    }
}

/// An attention item reduced to the fields the Today rollup needs
/// (decoupled from the wire type so the composer is pure + testable).
public struct AttentionInput: Sendable {
    public let id: String
    public let reason: String
    public let priority: Double
    public let due: Date?
    public let why: [String]?
    public let sources: [String]?

    public init(
        id: String,
        reason: String,
        priority: Double,
        due: Date? = nil,
        why: [String]? = nil,
        sources: [String]? = nil
    ) {
        self.id = id
        self.reason = reason
        self.priority = priority
        self.due = due
        self.why = why
        self.sources = sources
    }
}

/// A follow-up reduced to the fields the Today rollup needs.
public struct FollowUpInput: Sendable {
    public let id: String
    public let reason: String
    public let dueAt: Date

    public init(id: String, reason: String, dueAt: Date) {
        self.id = id
        self.reason = reason
        self.dueAt = dueAt
    }
}

/// A waiting-on request reduced to the fields the Today rollup needs.
public struct WaitingInput: Sendable {
    public let requestId: String
    public let personDisplayName: String
    public let description: String
    public let requestedAt: Date
    public let daysElapsed: Int

    public init(
        requestId: String,
        personDisplayName: String,
        description: String,
        requestedAt: Date,
        daysElapsed: Int
    ) {
        self.requestId = requestId
        self.personDisplayName = personDisplayName
        self.description = description
        self.requestedAt = requestedAt
        self.daysElapsed = daysElapsed
    }
}

// MARK: - Date bucketing for Today (prompt2 §25 NOW / TODAY / SOON)
//
// The backend already buckets attention items into NOW / TODAY / SOON,
// but the phone also needs a *local* date bucket for the Today screen's
// own framing (the Today tab shows "today's commitments / deadlines /
// waiting"). This is pure date math, deterministic, and unit-testable.

/// A date bucket relative to "today" (the user's local calendar day).
public enum TodayBucket: String, CaseIterable, Comparable, Sendable {
    /// Due today (or overdue — the phone surfaces overdue items as
    /// "today" so they are not lost).
    case today
    /// Due tomorrow.
    case tomorrow
    /// Due later this week.
    case thisWeek
    /// Due further out.
    case later
    /// No known date (e.g. a waiting-on request with no due date).
    case undated

    /// Sort order: things due soonest first, undated last.
    public var sortRank: Int {
        switch self {
        case .today: return 0
        case .tomorrow: return 1
        case .thisWeek: return 2
        case .later: return 3
        case .undated: return 4
        }
    }

    public static func < (lhs: TodayBucket, rhs: TodayBucket) -> Bool {
        lhs.sortRank < rhs.sortRank
    }

    public var displayName: String {
        switch self {
        case .today: return "Today"
        case .tomorrow: return "Tomorrow"
        case .thisWeek: return "This week"
        case .later: return "Later"
        case .undated: return "No date"
        }
    }
}

/// Pure date-bucketing helper. Deterministic given `now` + `calendar`.
///
/// `calendar` is injected so tests can pin a locale / time zone; the
/// production call uses the user's current calendar.
public enum DateBuckets {
    /// Bucket a due date relative to `now`.
    ///
    /// - Returns `.undated` when `date` is `nil`.
    /// - Returns `.today` when the date is today *or earlier* (overdue
    ///   items roll into "today" rather than disappearing).
    /// - Returns `.tomorrow` when the date is the next day.
    /// - Returns `.thisWeek` when the date is within the next 7 days.
    /// - Returns `.later` when the date is further out.
    public static func bucket(
        _ date: Date?,
        now: Date,
        calendar: Calendar = Calendar(identifier: .gregorian)
    ) -> TodayBucket {
        guard let date else { return .undated }

        let todayStart = startOfDay(now, calendar: calendar)
        let dayStart = startOfDay(date, calendar: calendar)

        // Overdue or today.
        if dayStart <= todayStart {
            return .today
        }

        let daysAhead = calendar.dateComponents(
            [.day],
            from: todayStart,
            to: dayStart
        ).day ?? 0

        if daysAhead == 1 {
            return .tomorrow
        } else if daysAhead <= 7 {
            return .thisWeek
        } else {
            return .later
        }
    }

    /// Midnight (start of day) for a given date, in `calendar`.
    public static func startOfDay(_ date: Date, calendar: Calendar) -> Date {
        calendar.dateInterval(of: .day, for: date)?.start ?? date
    }

    /// Group rows by bucket, preserving input order within each bucket.
    ///
    /// Returns buckets in sort order (today → later → undated), each a
    /// sub-array of the input rows. Rows with the same bucket keep their
    /// relative order from `rows` (stable).
    public static func group<T: Sendable>(
        _ rows: [T],
        dateOf: (T) -> Date?,
        now: Date,
        calendar: Calendar = Calendar(identifier: .gregorian)
    ) -> [TodayBucket: [T]] {
        var groups: [TodayBucket: [T]] = [:]
        for row in rows {
            let bucket = bucket(dateOf(row), now: now, calendar: calendar)
            groups[bucket, default: []].append(row)
        }
        return groups
    }
}

// MARK: - Inbox grouping (prompt2 §66 — cross-channel communication list)
//
// The Inbox lists normalized communications across channels (email,
// iMessage, SMS, RCS, voicemail). The §7 shape never carries raw content
// (only a `content_ref`), so the list is a *summary* row and the detail
// screen loads content by `content_ref`. Grouping is by (person, channel)
// so a conversation reads as one thread, not a flat interleaved list.

/// One normalized communication, reduced to the fields the Inbox needs.
///
/// `content` is the *display summary* (a subject / preview string the
/// backend provides for list rendering). It is NOT the raw payload — the
/// raw payload is behind `contentRef` and only the detail screen fetches
/// it. This mirrors the §7 contract: the list view never renders raw
/// content that was never fetched.
public struct CommunicationRow: Identifiable, Hashable, Sendable {
    public let id: String
    /// The person (display name) — `nil` when unattributed (the backend
    /// leaves `person_id` null for unknown counterparts, §6).
    public let person: String?
    /// The channel (email / imessage / sms / rcs / voicemail).
    public let channel: String
    /// Direction (incoming / outgoing).
    public let direction: String
    /// The list-level display summary (subject / preview). NOT raw content.
    public let summary: String
    /// When it happened.
    public let at: Date?
    /// The `content_ref` pointer to the (encrypted) raw payload. The
    /// detail screen uses this to load the full content on demand.
    public let contentRef: String

    public init(
        id: String,
        person: String?,
        channel: String,
        direction: String,
        summary: String,
        at: Date? = nil,
        contentRef: String
    ) {
        self.id = id
        self.person = person
        self.channel = channel
        self.direction = direction
        self.summary = summary
        self.at = at
        self.contentRef = contentRef
    }
}

/// A grouped thread: all communications for one (person, channel) pair,
/// most-recent-first.
public struct CommunicationThread: Identifiable, Hashable, Sendable {
    /// Stable thread identity (person + channel). `nil` person becomes a
    /// synthetic "Unattributed" key so the row still groups.
    public let id: String
    public let person: String?
    public let channel: String
    /// The thread's communications, most-recent first.
    public let communications: [CommunicationRow]

    public var newest: Date? { communications.first?.at }

    public init(person: String?, channel: String, communications: [CommunicationRow]) {
        self.id = Self.makeID(person: person, channel: channel)
        self.person = person
        self.channel = channel
        self.communications = communications
    }

    static func makeID(person: String?, channel: String) -> String {
        let p = person ?? "unattributed"
        return "\(p)|\(channel)"
    }
}

/// Pure inbox-grouping helper.
///
/// Deterministic: groups `rows` by (person, channel), orders each
/// thread most-recent-first, and orders threads most-recent-first (the
/// thread with the newest communication first). Rows with no date sort
/// after dated rows within a thread.
public enum InboxGrouping {
    /// Group flat communications into threads.
    public static func group(_ rows: [CommunicationRow]) -> [CommunicationThread] {
        // Collect by (person, channel) key, preserving order of first
        // appearance per key so the stable thread list is deterministic.
        var keyToRows: [String: [CommunicationRow]] = [:]
        var keyOrder: [String] = []
        for row in rows {
            let key = CommunicationThread.makeID(person: row.person, channel: row.channel)
            if keyToRows[key] == nil {
                keyOrder.append(key)
            }
            keyToRows[key, default: []].append(row)
        }

        // Most-recent-first within each thread (dated rows before undated,
        // descending date; undated rows keep input order after them).
        var threads: [CommunicationThread] = []
        for key in keyOrder {
            let rowsForKey = keyToRows[key] ?? []
            let sorted = sortMostRecentFirst(rowsForKey)
            threads.append(CommunicationThread(
                person: rowsForKey.first?.person,
                channel: rowsForKey.first?.channel ?? "",
                communications: sorted
            ))
        }

        // Order threads by newest communication (most-recent thread first).
        return threads.sorted { a, b in
            compareNewest(a.newest, b.newest)
        }
    }

    /// Sort a thread's rows most-recent-first. Dated rows descending by
    /// `at`; undated rows after them, stable.
    public static func sortMostRecentFirst(_ rows: [CommunicationRow]) -> [CommunicationRow] {
        let dated = rows.filter { $0.at != nil }
            .sorted { a, b in (a.at ?? .distantPast) > (b.at ?? .distantPast) }
        let undated = rows.filter { $0.at == nil }
        return dated + undated
    }

    /// Compare two "newest" dates for thread ordering. `nil` (no dated
    /// communication in the thread) sorts last.
    static func compareNewest(_ a: Date?, _ b: Date?) -> Bool {
        switch (a, b) {
        case (nil, nil): return false
        case (nil, _): return false   // a sorts after b
        case (_, nil): return true    // a sorts before b
        case let (x?, y?): return x > y
        }
    }
}

// MARK: - People (prompt2 §66 — person list + VIP)
//
// People is the list of canonical persons (R101). Each person has
// normalized identities and can be toggled VIP via `POST
// /api/people/{id}/vip`. The VIP toggle is a small, deterministic state
// machine (the optimistic-UI contract), so it lives here as a testable
/// enum + helper rather than in the view.

/// One canonical person, reduced to the fields the People view needs.
public struct PersonRow: Identifiable, Hashable, Sendable {
    public let id: String
    public let displayName: String?
    public let isVip: Bool
    /// Normalized identity values (e.g. emails / phones) for display.
    public let identities: [IdentityRow]

    public init(
        id: String,
        displayName: String?,
        isVip: Bool,
        identities: [IdentityRow] = []
    ) {
        self.id = id
        self.displayName = displayName
        self.isVip = isVip
        self.identities = identities
    }

    /// The display name, or a stable fallback when the backend has none
    /// (honest — falls back to a truncated id, never a fabricated name).
    public var displayLabel: String {
        if let displayName, !displayName.isEmpty { return displayName }
        return id
    }
}

/// One normalized identity value for a person.
public struct IdentityRow: Identifiable, Hashable, Sendable {
    /// The kind (email / phone / imessage / voicemail / contact).
    public let kind: String
    /// The normalized value (case-folded email, E.164 phone, ...).
    public let value: String
    public var id: String { "\(kind):\(value)" }

    public init(kind: String, value: String) {
        self.kind = kind
        self.value = value
    }
}

/// The VIP toggle state machine (prompt2 §12 — VIP contacts weigh
/// heavier in the attention engine).
///
/// A small, deterministic contract: `toggle(from:)` returns the target
/// value for the R101 `POST /api/people/{id}/vip` call. The view applies
/// it optimistically and rolls back on a transport error.
public enum VipToggle {
    /// The next VIP value when the user taps the toggle for a person
    /// currently at `current`.
    public static func next(current: Bool) -> Bool { !current }

    /// The `is_vip` value to POST to `/api/people/{id}/vip`.
    public static func payloadValue(for person: PersonRow) -> Bool {
        next(current: person.isVip)
    }

    /// Apply the server-confirmed result to the local row.
    ///
    /// - Returns a new row with `isVip` set to `confirmed`.
    public static func apply(
        confirmed: Bool,
        to person: PersonRow
    ) -> PersonRow {
        PersonRow(
            id: person.id,
            displayName: person.displayName,
            isVip: confirmed,
            identities: person.identities
        )
    }
}

// MARK: - Waiting (prompt2 §66 — waiting-on queue with status filter)
//
// Waiting is the `GET /api/waiting-on` queue. The status filter is a
/// pure, testable value type: it maps the UI's filter choice to the
/// backend's `?status=` query parameter.

/// One waiting-on request, reduced to the fields the Waiting view needs.
public struct WaitingRow: Identifiable, Hashable, Sendable {
    public let id: String
    public let personDisplayName: String
    public let personId: String?
    public let description: String
    public let requestedAt: Date
    public let source: String
    public let status: String
    public let daysElapsed: Int
    /// How many follow-ups have been drafted for this request (§19
    /// `[Follow up]` bookkeeping).
    public let followUpCount: Int

    public init(
        id: String,
        personDisplayName: String,
        personId: String?,
        description: String,
        requestedAt: Date,
        source: String,
        status: String,
        daysElapsed: Int,
        followUpCount: Int = 0
    ) {
        self.id = id
        self.personDisplayName = personDisplayName
        self.personId = personId
        self.description = description
        self.requestedAt = requestedAt
        self.source = source
        self.status = status
        self.daysElapsed = daysElapsed
        self.followUpCount = followUpCount
    }
}

/// The status filter for the Waiting queue, mapped to the backend's
/// `?status=` query value.
public enum WaitingStatusFilter: String, CaseIterable, Sendable {
    case open
    case resolved
    case cancelled
    case all

    public var displayName: String {
        switch self {
        case .open: return "Open"
        case .resolved: return "Resolved"
        case .cancelled: return "Cancelled"
        case .all: return "All"
        }
    }

    /// The `?status=` value sent to `GET /api/waiting-on`.
    public var queryValue: String { rawValue }
}

// MARK: - Briefing (prompt2 §30 — daily briefing)
//
// Briefing is a read-only, deterministic aggregation from
// `GET /api/briefing`. The `empty` flag drives the honest closing line
// ("Nothing else requires attention."). The kind (morning / midday /
// evening) is a pure value type mapped to the `?kind=` query.

/// Which briefing of the day (prompt2 §30).
public enum BriefingKind: String, CaseIterable, Sendable {
    case morning
    case midday
    case evening

    public var displayName: String {
        switch self {
        case .morning: return "Morning"
        case .midday: return "Midday"
        case .evening: return "Evening"
        }
    }

    /// The `?kind=` value sent to `GET /api/briefing`.
    public var queryValue: String { rawValue }
}

/// A briefing action item ("things need action").
public struct BriefingActionRow: Identifiable, Hashable, Sendable {
    public let attentionId: String
    public let label: String
    public let due: Date?

    public init(attentionId: String, label: String, due: Date? = nil) {
        self.attentionId = attentionId
        self.label = label
        self.due = due
    }

    public var id: String { attentionId }
}

/// An outstanding request in the briefing ("you're waiting on").
public struct BriefingWaitingRow: Identifiable, Hashable, Sendable {
    public let requestId: String
    public let person: String
    public let description: String
    public let daysElapsed: Int

    public init(requestId: String, person: String, description: String, daysElapsed: Int) {
        self.requestId = requestId
        self.person = person
        self.description = description
        self.daysElapsed = daysElapsed
    }

    public var id: String { requestId }
}

/// A calendar event in the briefing ("today").
public struct BriefingEventRow: Identifiable, Hashable, Sendable {
    public let eventId: String
    public let title: String
    public let start: Date?

    public init(eventId: String, title: String, start: Date? = nil) {
        self.eventId = eventId
        self.title = title
        self.start = start
    }

    public var id: String { eventId }
}

/// The composed Briefing, reduced to the fields the Briefing view needs.
public struct BriefingData: Sendable {
    public let kind: BriefingKind
    public let generatedAt: Date
    public let actions: [BriefingActionRow]
    public let waitingOn: [BriefingWaitingRow]
    public let today: [BriefingEventRow]
    public let overnightEmails: Int
    public let overnightMessages: Int
    public let overnightVoicemails: Int
    public let empty: Bool

    public init(
        kind: BriefingKind,
        generatedAt: Date,
        actions: [BriefingActionRow] = [],
        waitingOn: [BriefingWaitingRow] = [],
        today: [BriefingEventRow] = [],
        overnightEmails: Int = 0,
        overnightMessages: Int = 0,
        overnightVoicemails: Int = 0,
        empty: Bool = false
    ) {
        self.kind = kind
        self.generatedAt = generatedAt
        self.actions = actions
        self.waitingOn = waitingOn
        self.today = today
        self.overnightEmails = overnightEmails
        self.overnightMessages = overnightMessages
        self.overnightVoicemails = overnightVoicemails
        self.empty = empty
    }

    /// The honest closing line when the brief is empty (prompt2 §30).
    public var closingLine: String? {
        empty ? "Nothing else requires attention." : nil
    }
}
