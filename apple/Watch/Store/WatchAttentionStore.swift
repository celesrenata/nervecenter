import Foundation
import Combine

import NerveApps

// MARK: - §27/§60/§67 Watch attention state (App Group provider)

/// I/O over the App Group files the attention surfaces read and write.
///
/// The read path mirrors `WatchSnapshotSource` (same container, same
/// file names, same "missing/malformed → honest empty" rule); the write
/// path mirrors the §67 `stop-agents.json` marker pattern (a watch →
/// phone marker file, observed by the phone — no network from the wrist,
/// spec §60). Tests inject an in-memory implementation.
public protocol WatchAttentionIO: Sendable {
    /// The published attention feed, decoded from `cached-state.json`.
    ///
    /// `published == false` when the file is absent or the `attention` key
    /// is absent (older phone); malformed bytes also degrade to
    /// `published == false` (never invented rows, spec §61).
    func readFeed() -> AttentionFeedState

    /// The pending wrist-action records from `watch-actions.json`.
    /// `[]` when absent or undecodable.
    func readRecords() -> [AttentionActionRecord]

    /// Append a wrist-action record for the phone to execute. Throws on
    /// I/O failure so the UI renders an honest error rather than pretending
    /// the action was recorded.
    func appendRecord(_ record: AttentionActionRecord) throws
}

/// Production I/O: the shared App Group container (`group.nerv.center`),
/// resolved exactly like `AppGroupSnapshotSource` (entitlement container
/// first, Application Support fallback).
public struct AppGroupAttentionIO: WatchAttentionIO {
    public let containerURL: @Sendable () -> URL

    public init(containerURL: @escaping @Sendable () -> URL = {
        FileManager.default.containerURL(
            forSecurityApplicationGroupIdentifier: WatchAppGroup.identifier
        ) ?? FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first?
            .appendingPathComponent("NerveCenter", isDirectory: true)
            ?? FileManager.default.temporaryDirectory
    }) {
        self.containerURL = containerURL
    }

    public func readFeed() -> AttentionFeedState {
        let url = containerURL().appendingPathComponent(WatchAttentionJSON.snapshotFileName)
        guard let data = try? Data(contentsOf: url) else {
            return AttentionFeedState(published: false, items: [])
        }
        guard let file = WatchAttentionJSON.decodeAttentionFile(from: data) else {
            // Malformed file → honest "not published" (never invented rows).
            return AttentionFeedState(published: false, items: [])
        }
        return AttentionFeedState(
            published: file.attention != nil,
            items: file.attention ?? []
        )
    }

    public func readRecords() -> [AttentionActionRecord] {
        let url = containerURL().appendingPathComponent(WatchAttentionJSON.actionsFileName)
        guard let data = try? Data(contentsOf: url) else { return [] }
        return WatchAttentionJSON.decodeActions(data)
    }

    public func appendRecord(_ record: AttentionActionRecord) throws {
        let url = containerURL().appendingPathComponent(WatchAttentionJSON.actionsFileName)
        let records = readRecords() + [record]
        guard let data = WatchAttentionJSON.encodeActions(records) else {
            throw WatchAttentionError.encodeFailed
        }
        try data.write(to: url, options: [.atomic])
    }
}

/// In-memory I/O for previews, tests, and hosts without the entitlement.
public final class InMemoryAttentionIO: WatchAttentionIO, @unchecked Sendable {
    public var feed: AttentionFeedState
    public var records: [AttentionActionRecord]
    /// Every record the wrist appended, in order (for test assertions).
    public private(set) var appended: [AttentionActionRecord] = []

    public init(
        feed: AttentionFeedState = AttentionFeedState(published: false, items: []),
        records: [AttentionActionRecord] = []
    ) {
        self.feed = feed
        self.records = records
    }

    public func readFeed() -> AttentionFeedState { feed }
    public func readRecords() -> [AttentionActionRecord] { records }

    public func appendRecord(_ record: AttentionActionRecord) throws {
        appended.append(record)
        records.append(record)
    }
}

public enum WatchAttentionError: LocalizedError {
    case encodeFailed
    public var errorDescription: String? {
        switch self {
        case .encodeFailed: return "Could not encode the action record."
        }
    }
}

// MARK: - §27/§60 observable attention store (Watch target)

/// The Watch's attention state provider (spec §27 home, §60 wrist actions,
/// §67 complication count).
///
/// Plain (non-isolated) observable store, only touched from the main actor
/// by the SwiftUI layer — the same convention as `WatchDataStore`.
public final class WatchAttentionStore: ObservableObject {
    // MARK: Published state

    /// The published feed (re-read on `refresh()`).
    @Published public private(set) var feed: AttentionFeedState
    /// The pending wrist-action records (re-read on `refresh()`).
    @Published public private(set) var records: [AttentionActionRecord]
    /// Honest result of the last wrist action (nil = none yet / success).
    @Published public private(set) var lastActionError: String?

    // MARK: Dependencies

    public let io: WatchAttentionIO
    let nowProvider: () -> Date

    // MARK: Init

    public init(
        io: WatchAttentionIO,
        nowProvider: @escaping () -> Date = { Date() }
    ) {
        self.io = io
        self.nowProvider = nowProvider
        self.feed = io.readFeed()
        self.records = io.readRecords()
    }

    /// Production wiring: App Group container, App Group action marker.
    public convenience init() {
        self.init(io: AppGroupAttentionIO())
    }

    /// Re-read both App Group files. Called on refresh and on app active —
    /// the phone publishes while the Watch is awake.
    public func refresh() {
        feed = io.readFeed()
        records = io.readRecords()
    }

    // MARK: Derived state

    /// The ranked, wrist-overlay-applied live feed (spec §27 list order:
    /// NOW items first, then backend priority order).
    public var live: [AttentionRankedItem] {
        WatchAttentionEngine.liveItems(feed: feed, records: records, asOf: nowProvider())
    }

    /// §67 complication count: open NOW-state items (honest zero when
    /// published-and-empty; `0` with `attentionPublished == false` when
    /// the phone has not published — the complication renders "–", not
    /// "✓ CLEAR").
    public var needYouCount: Int {
        WatchAttentionEngine.needYouCount(feed: feed, records: records, asOf: nowProvider())
    }

    /// True when the phone has published the `attention` key at all.
    public var attentionPublished: Bool {
        feed.published
    }

    /// §67 complication text (`"3 NEED YOU"` / `"✓ CLEAR"` / `"–"`).
    public var complicationText: String {
        WatchAttentionEngine.complicationText(feed: feed, records: records, asOf: nowProvider())
    }

    /// §27 home header ("2 things need you" / "All clear" / `""` when not
    /// published).
    public var headerText: String {
        WatchAttentionEngine.headerText(feed: feed, records: records, asOf: nowProvider())
    }

    // MARK: §60 wrist actions

    /// Snooze the item (default 1 h window; the phone executes the backend
    /// `snooze` action on its next sync).
    public func snooze(_ item: AttentionRankedItem, minutes: Int? = nil) {
        apply(.snooze, to: item, snoozeMinutes: minutes)
    }

    /// Mark the item done (the phone executes the backend `resolve`).
    public func resolve(_ item: AttentionRankedItem) {
        apply(.resolve, to: item, snoozeMinutes: nil)
    }

    /// Escalate: record the request so the phone surfaces the item
    /// (notification / open) — spec §60.
    public func escalate(_ item: AttentionRankedItem) {
        apply(.escalate, to: item, snoozeMinutes: nil)
    }

    /// One action: append the record to `watch-actions.json`, then overlay
    /// it locally so the wrist sees the result immediately (the phone is
    /// still the one that executes it against the backend — no network
    /// call from the wrist).
    private func apply(_ action: AttentionActionKind, to item: AttentionRankedItem, snoozeMinutes: Int?) {
        let now = nowProvider()
        let record = AttentionActionRecord(
            attentionID: item.item.attentionID,
            action: action,
            requestedAt: now,
            snoozeMinutes: action == .snooze ? (snoozeMinutes ?? AttentionEscalation.defaultSnoozeMinutes) : nil,
            note: "watch \(action.rawValue)"
        )
        do {
            try io.appendRecord(record)
            lastActionError = nil
        } catch {
            lastActionError = error.localizedDescription
        }
        // Overlay the record locally so the UI reflects the wrist decision
        // even before the next phone publish (the record is durable — the
        // phone reads it from the App Group). The `records` append alone
        // fires `objectWillChange`; `live` re-derives from it.
        records.append(record)
    }
}
