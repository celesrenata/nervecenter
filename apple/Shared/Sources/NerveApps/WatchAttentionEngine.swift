import Foundation

// MARK: - §27/§60/§67 platform-free attention engine (ranking, count, wrist state machine)

/// A feed snapshot of the published attention items, with the honest
/// `published` flag distinguishing "the phone published `attention: []`"
/// (genuinely nothing needs the user) from "the key is absent" (the phone
/// has not published the personal plane yet — §61: never pretend either).
public struct AttentionFeedState: Hashable, Sendable {
    /// True when the §73 snapshot file carries the `attention` key.
    public var published: Bool
    /// The items exactly as published (`[]` when published-and-empty).
    public var items: [AttentionWireItem]

    public init(published: Bool, items: [AttentionWireItem]) {
        self.published = published
        self.items = items
    }
}

/// The per-item local state the wrist overlay applies on top of the
/// published item (spec §60: wrist actions are *records*; the phone is the
/// one that executes them against the backend and re-publishes truth).
public enum AttentionLocalState: Hashable, Sendable {
    /// No pending wrist action; the item shows as published.
    case open
    /// The wearer snoozed it; hidden until `until` passes.
    case snoozed(until: Date)
    /// The wearer escalated it; the phone will surface it (still live).
    case escalated
    /// The wearer marked it done; hidden until the phone re-publishes
    /// the resolved backend state.
    case resolved
}

/// A published attention item with its resolved local (wrist-overlay) state.
public struct AttentionRankedItem: Identifiable, Hashable, Sendable {
    public var item: AttentionWireItem
    public var localState: AttentionLocalState
    /// The action the wearer last requested for this item (nil = none).
    public var pendingAction: AttentionActionKind?

    public var id: String { item.attentionID }

    public init(item: AttentionWireItem, localState: AttentionLocalState, pendingAction: AttentionActionKind? = nil) {
        self.item = item
        self.localState = localState
        self.pendingAction = pendingAction
    }
}

// MARK: - Escalation state machine (§60)

/// The watch-side state machine for per-item wrist actions.
///
/// Pure and deterministic (platform-free, fully unit-tested):
///
/// ```text
/// open      --snooze-->   snoozed(until)
/// open      --resolve-->  resolved
/// open      --escalate--> escalated
/// snoozed   --snooze-->   snoozed(new until)
/// snoozed   --resolve-->  resolved
/// snoozed   --escalate--> escalated
/// escalated --snooze-->   snoozed(until)
/// escalated --resolve-->  resolved
/// escalated --escalate--> escalated   (idempotent)
/// resolved  --anything--> resolved    (terminal)
/// ```
///
/// "Snooze wake" is not an action — it is a time predicate: a snoozed item
/// becomes visible again once `now >= until` (``AttentionLocalState.isAwake(at:)``).
public enum AttentionEscalation {
    /// Default snooze window when the wrist gives no explicit one
    /// (spec §61 example: "[Snooze 1h]").
    public static let defaultSnoozeMinutes = 60

    /// Apply a wrist action to the current local state at `date`.
    public static func apply(
        _ action: AttentionActionKind,
        to state: AttentionLocalState,
        at date: Date,
        snoozeMinutes: Int? = nil
    ) -> AttentionLocalState {
        switch state {
        case .resolved:
            // Terminal: a done item stays done even if a stale record
            // arrives later (idempotent, honest).
            return .resolved
        case .open, .escalated, .snoozed:
            switch action {
            case .snooze:
                let minutes = (snoozeMinutes ?? defaultSnoozeMinutes).clamped(to: 5...1440)
                return .snoozed(until: date.addingTimeInterval(TimeInterval(minutes * 60)))
            case .resolve:
                return .resolved
            case .escalate:
                return .escalated
            }
        }
    }

    /// True when the item should be *visible* to the wearer at `date`.
    ///
    /// - `.open` / `.escalated` → visible (an escalation still needs the user).
    /// - `.snoozed` → visible only once the window has expired.
    /// - `.resolved` → hidden until the phone re-publishes the snapshot
    ///   without the resolved item (the wrist does not pretend the backend
    ///   state changed — §60/§61 honesty).
    public static func isAwake(_ state: AttentionLocalState, at date: Date) -> Bool {
        switch state {
        case .open, .escalated:
            return true
        case .snoozed(let until):
            return date >= until
        case .resolved:
            return false
        }
    }

    /// A snoozed item whose window has passed behaves as plain `open`
    /// again (the backend still holds it in its published state).
    public static func settled(_ state: AttentionLocalState, at date: Date) -> AttentionLocalState {
        switch state {
        case .snoozed(let until):
            return date >= until ? .open : .snoozed(until: until)
        case let other:
            return other
        }
    }
}

// MARK: - Ranking + count (§27, §67)

/// The platform-free derivation of everything the watch renders about
/// attention: the ranked feed, the "n NEED YOU" complication count, and the
/// honest empty/clear texts. All inputs are wire data from
/// `cached-state.json` + `watch-actions.json` (App Group) — nothing here
/// touches the network (§60: the wrist never calls the backend; the phone
/// is the relay).
public enum WatchAttentionEngine {
    /// The backend lifecycle literals that are *not* live (spec §25).
    public static let closedStatuses: Set<String> = ["SUPPRESSED", "RESOLVED"]

    /// True when the item is in the NOW bucket (the "needs you now" set).
    public static func isNow(_ status: String) -> Bool {
        status.uppercased() == "NOW"
    }

    /// True when the published item is still live (not suppressed/resolved
    /// on the backend side).
    public static func isOpen(_ status: String) -> Bool {
        !closedStatuses.contains(status.uppercased())
    }

    /// Rank items the way the backend orders its NOW feed
    /// (`backend/attention/service.py::_sort_key`): higher priority first;
    /// ties broken by earliest due (no due last); then stable id.
    public static func rank(_ items: [AttentionWireItem]) -> [AttentionWireItem] {
        items.sorted { a, b in
            if a.priority != b.priority { return a.priority > b.priority }
            switch (a.due, b.due) {
            case let (x?, y?):
                if x != y { return x < y }
            case (nil, _):
                return false
            case (_, nil):
                return true
            }
            return a.attentionID < b.attentionID
        }
    }

    /// The local state of one item given its pending wrist records.
    ///
    /// Records are replayed oldest → newest through the
    /// ``AttentionEscalation`` machine starting from `.open`, then settled
    /// against `asOf` so expired snoozes wake the item.
    public static func localState(
        for attentionID: String,
        in records: [AttentionActionRecord],
        asOf date: Date
    ) -> AttentionLocalState {
        let mine = records
            .filter { $0.attentionID == attentionID }
            .sorted { a, b in
                if a.requestedAt != b.requestedAt { return a.requestedAt < b.requestedAt }
                return a.action.rawValue < b.action.rawValue
            }
        var state: AttentionLocalState = .open
        for record in mine {
            state = AttentionEscalation.apply(record.action, to: state, at: record.requestedAt, snoozeMinutes: record.snoozeMinutes)
        }
        return AttentionEscalation.settled(state, at: date)
    }

    /// The live feed the home renders: published, open, awake items ranked
    /// with NOW items first, then by backend order.
    public static func liveItems(
        feed: AttentionFeedState,
        records: [AttentionActionRecord],
        asOf date: Date
    ) -> [AttentionRankedItem] {
        guard feed.published else { return [] }
        var ranked: [AttentionRankedItem] = []
        for item in rank(feed.items) where isOpen(item.status) {
            let local = localState(for: item.attentionID, in: records, asOf: date)
            guard AttentionEscalation.isAwake(local, at: date) else { continue }
            let pending = latestRecord(for: item.attentionID, in: records)
            ranked.append(AttentionRankedItem(item: item, localState: local, pendingAction: pending))
        }
        return ranked
    }

    /// The complication count (spec §67): open NOW-state attention items,
    /// including the wrist overlay (snoozed-but-not-expired items are out;
    /// resolved are out; escalated stay in — they still need the user).
    public static func needYouCount(
        feed: AttentionFeedState,
        records: [AttentionActionRecord] = [],
        asOf date: Date = Date()
    ) -> Int {
        liveItems(feed: feed, records: records, asOf: date)
            .filter { isNow($0.item.status) }
            .count
    }

    /// The complication text (spec §67):
    ///
    /// - published with `n > 0` NOW items → `"\(n) NEED YOU"`
    /// - published with zero NOW items → `"✓ CLEAR"` (honest zero)
    /// - attention not published at all → `"–"` (no data — *not* CLEAR,
    ///   we simply do not know; §61 honesty)
    public static func complicationText(
        feed: AttentionFeedState,
        records: [AttentionActionRecord] = [],
        asOf date: Date = Date()
    ) -> String {
        guard feed.published else { return "–" }
        let count = needYouCount(feed: feed, records: records, asOf: date)
        return count > 0 ? "\(count) NEED YOU" : "✓ CLEAR"
    }

    /// The home header text (spec §27: "2 things need you").
    public static func headerText(
        feed: AttentionFeedState,
        records: [AttentionActionRecord] = [],
        asOf date: Date = Date()
    ) -> String {
        guard feed.published else { return "" }
        let count = needYouCount(feed: feed, records: records, asOf: date)
        switch count {
        case 0: return "All clear"
        case 1: return "1 thing needs you"
        default: return "\(count) things need you"
        }
    }

    // MARK: Internal

    private static func latestRecord(
        for attentionID: String,
        in records: [AttentionActionRecord]
    ) -> AttentionActionKind? {
        let mine = records.filter { $0.attentionID == attentionID }
        return mine.max { $0.requestedAt < $1.requestedAt }?.action
    }
}

private extension Comparable {
    func clamped(to range: ClosedRange<Self>) -> Self {
        min(max(self, range.lowerBound), range.upperBound)
    }
}
