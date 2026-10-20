import Foundation

import NerveApps

// MARK: - §62 Widget data source (platform-free, testable)

/// A snapshot of the state a widget tile can render.
///
/// `tile == nil` means the backing state is *honestly* absent (never paired,
/// no data written yet, or a corrupt/missing file) — in which case the tile
/// renders an honest "Not connected" / "No data yet" state rather than
/// invented numbers.
public struct WidgetSnapshot: Sendable, Hashable {
    /// The renderable tile (from ``NerveApps/WidgetTile``). `nil` = no data.
    public var tile: NerveApps.WidgetTile?
    /// When the backing cache was last written. `nil` when there is no tile.
    public var updatedAt: Date?
    /// §67 complication text ("3 NEED YOU" / "✓ CLEAR") read from the
    /// snapshot's `attention` key. `nil` = the phone has not published the
    /// personal plane — the watch renders the system tile (never a fake
    /// "CLEAR"; spec §61).
    public var attentionText: String?

    public init(tile: NerveApps.WidgetTile?, updatedAt: Date? = nil, attentionText: String? = nil) {
        self.tile = tile
        self.updatedAt = updatedAt
        self.attentionText = attentionText
    }

    /// The most specific available timestamp: the snapshot-level
    /// `updatedAt` (e.g. when the phone last *wrote* the file) falling back
    /// to the tile's own cache `updatedAt`. Always non-nil when `tile != nil`
    /// (`WidgetTile.updatedAt` is a non-optional `Date`).
    public var effectiveUpdatedAt: Date? {
        updatedAt ?? tile?.updatedAt
    }
}

/// Where a widget's data came from. Drives the honest placeholder/empty copy.
public enum WidgetDataState: Sendable, Hashable {
    /// A tile was read. Staleness is derived from the tile's `updatedAt`
    /// against ``StaleState/StalenessPolicy/default`` (90 s).
    case connected(snapshot: WidgetSnapshot)
    /// The phone has never published state (no file in the App Group).
    case notConnected
    /// A state file exists but is corrupt / unreadable / empty.
    case noData
}

extension WidgetDataState {
    /// `true` when the state carries a real tile (regardless of staleness).
    public var hasTile: Bool {
        switch self {
        case .connected: return true
        case .notConnected, .noData: return false
        }
    }

    /// `true` when there is nothing real to render — used to choose a more
    /// aggressive refresh interval.
    public var needsAttention: Bool { !hasTile }
}

/// Platform-free, testable source for widget data.
///
/// The production implementation (``AppGroupWidgetDataSource``) reads the
/// phone's published state from the shared App Group container. An
/// in-memory implementation (``InMemoryWidgetDataSource``) is used by tests.
///
/// Implementations must never fabricate a tile: a missing or corrupt backing
/// state must surface as `tile == nil`, never as a tile with invented
/// numbers or mock rows.
public protocol WidgetDataSource: Sendable {
    /// Build the current tile snapshot.
    ///
    /// Returns a snapshot whose `tile` is `nil` when no data is present.
    /// The caller uses ``hasStateFile()`` to decide between the "Not
    /// connected" and "No data yet" renderings.
    func makeTile() async throws -> WidgetSnapshot

    /// Whether the source's backing storage contains any state file at all
    /// (even an unreadable one). `false` → the phone never published
    /// ("Not connected"); `true` with a `nil` tile → "No data yet".
    func hasStateFile() async -> Bool
}

extension WidgetDataSource {
    /// Map a source result to the display state the provider and views use.
    public func state(snapshot: WidgetSnapshot, hasFile: Bool) -> WidgetDataState {
        guard let tile = snapshot.tile else {
            return hasFile ? .noData : .notConnected
        }
        let updatedAt = snapshot.updatedAt ?? tile.updatedAt
        return .connected(
            snapshot: WidgetSnapshot(tile: tile, updatedAt: updatedAt, attentionText: snapshot.attentionText)
        )
    }
}
