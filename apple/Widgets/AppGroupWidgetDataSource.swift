import Foundation

import NerveApps
import NerveModels
import NervePersistence

// MARK: - App Group widget data source

/// App Group identifier shared by the iPhone app, Watch app, and the
/// WidgetKit extension (spec §62/§71/§72).
///
/// NOTE: The iPhone app currently declares
/// `group.com.celestium.nervecenter` in `apple/iOS/App/Environment/
/// PhoneEnvironment.swift`. The parent must align the entitlement with this
/// value (or swap one for the other) before the extension can read the
/// phone's published state on device.
public enum NerveWidgetAppGroup {
    public static let identifier = "group.nerv.center"
}

/// Directory layout of the shared App Group container the phone writes:
/// `published/nerve-published-state.json` is the primary source;
/// `cached-state.json` + `compute.json` are supported as a secondary
/// fallback if the phone writes them in future (spec §73 cache naming).
public enum NerveWidgetPaths {
    public static let publishedStateFile = "nerve-published-state.json"
    public static let publishedStateDirectory = "published"
    public static let cachedStateFile = "cached-state.json"
    public static let computeFile = "compute.json"
}

/// A wire file the phone may publish for compute rows: an array of
/// `nodeID` / `hardware` / `utilizationPercent` entries.
private struct ComputeRowWire: Codable {
    var nodeID: String
    var hardware: String
    var utilizationPercent: Int
}

/// Production ``WidgetDataSource``: reads the phone's published state from
/// the App Group container.
///
/// Honesty rules (spec §62):
/// - No file at all → `nil` tile (the phone has never published).
/// - File present but corrupt / unreadable → `nil` tile, never fabricated
///   numbers or mock rows.
/// - Compute rows are only surfaced from a *real* `compute.json` (or the
///   tile already carrying them). Absent → empty list → the views render
///   "No compute data" instead of invented rows.
public struct AppGroupWidgetDataSource: WidgetDataSource {
    /// Resolves the shared App Group container directory. Injected for
    /// tests; defaults to the real App Group (with an Application Support
    /// fallback when the entitlement is not provisioned, mirroring
    /// `AppGroupContainer` in the phone app).
    public let containerDirectory: @Sendable () -> URL

    public init(containerDirectory: @escaping @Sendable () -> URL = {
        FileManager.default.containerURL(
            forSecurityApplicationGroupIdentifier: NerveWidgetAppGroup.identifier
        )
        ?? FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first?
            .appendingPathComponent("NerveCenter", isDirectory: true)
            ?? FileManager.default.temporaryDirectory
    }) {
        self.containerDirectory = containerDirectory
    }

    /// Whether any backing state file exists in the container (distinguishes
    /// "not connected" from "corrupt/no data" for the views).
    public func hasStateFile() async -> Bool {
        let dir = containerDirectory()
        return FileManager.default.fileExists(atPath: dir.appendingPathComponent(NerveWidgetPaths.publishedStateFile).path)
            || FileManager.default.fileExists(atPath: dir.appendingPathComponent(NerveWidgetPaths.cachedStateFile).path)
    }

    public func makeTile() async throws -> WidgetSnapshot {
        // §67: the attention feed lives in `cached-state.json` regardless
        // of which state file produces the tile below; read it once.
        let attentionText = readAttentionText()
        // 1) Primary source: the phone's published state (tile included).
        if let published = readPublishedState() {
            return WidgetSnapshot(tile: published.tile, updatedAt: published.updatedAt, attentionText: attentionText)
        }
        // 2) Fallback: the §73 cache plus optional compute rows.
        if let cache = readCachedState() {
            let tile = makeTile(from: cache)
            return WidgetSnapshot(tile: tile, updatedAt: tile.updatedAt, attentionText: attentionText)
        }
        // Honest absence: no published state, no cache → no tile. The views
        // render "Not connected" / "No data yet" — never fabricated data.
        return WidgetSnapshot(tile: nil, updatedAt: nil, attentionText: attentionText)
    }

    /// The §67 complication text from `cached-state.json`'s `attention`
    /// key (`"3 NEED YOU"` / `"✓ CLEAR"`). `nil` when the file is absent or
    /// malformed, or when the key is absent (the phone has not published
    /// the personal plane) — the watch then renders the system tile
    /// instead of a fake "CLEAR" (spec §61).
    private func readAttentionText() -> String? {
        let url = containerDirectory().appendingPathComponent(NerveWidgetPaths.cachedStateFile)
        guard
            let data = try? Data(contentsOf: url),
            let file = WatchAttentionJSON.decodeAttentionFile(from: data),
            let items = file.attention
        else {
            return nil
        }
        return WatchAttentionEngine.complicationText(
            feed: AttentionFeedState(published: true, items: items)
        )
    }

    // MARK: Reading

    /// Read the published state file; `nil` when missing or corrupt.
    func readPublishedState() -> PublishedNerveState? {
        let url = containerDirectory().appendingPathComponent(NerveWidgetPaths.publishedStateFile)
        guard FileManager.default.fileExists(atPath: url.path),
              let data = try? Data(contentsOf: url) else { return nil }
        return NerveJSON.decode(PublishedNerveState.self, from: data)
    }

    /// Read the §73 `cached-state.json` (primary goal + summary fields are
    /// not part of the cache wire format, so they stay honestly `nil`).
    func readCachedState() -> CachedState? {
        let url = containerDirectory().appendingPathComponent(NerveWidgetPaths.cachedStateFile)
        guard FileManager.default.fileExists(atPath: url.path),
              let data = try? Data(contentsOf: url) else { return nil }
        return NerveJSON.decode(CachedState.self, from: data)
    }

    /// Read `compute.json` into widget rows; `[]` when absent or corrupt.
    func readComputeRows() -> [ComputeWidgetRow] {
        let url = containerDirectory().appendingPathComponent(NerveWidgetPaths.computeFile)
        guard FileManager.default.fileExists(atPath: url.path),
              let data = try? Data(contentsOf: url),
              let wires = NerveJSON.decode([ComputeRowWire].self, from: data) else { return [] }
        return wires.map { ComputeWidgetRow(nodeID: $0.nodeID, hardware: $0.hardware, utilizationPercent: $0.utilizationPercent) }
    }

    /// Derive a tile from a §73 cache. Compute rows come only from the real
    /// `compute.json`; nothing is invented.
    func makeTile(from cache: CachedState) -> WidgetTile {
        WidgetTile(
            systemHealth: cache.systemHealth,
            agents: cache.runningAgents,
            incidents: cache.activeIncidents,
            pendingApprovals: cache.pendingApprovals,
            primaryGoalLabel: nil,
            primaryGoalProgress: cache.primaryGoalProgress,
            computeRows: readComputeRows(),
            incidentsSummary: nil,
            agentActivitySummary: nil,
            updatedAt: cache.updatedAt
        )
    }
}
