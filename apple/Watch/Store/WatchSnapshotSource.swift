import Foundation

import NerveApps
import NerveAuth
import NerveModels
import NervePersistence

// MARK: - §69/§73 Watch snapshot source (App Group container)

/// App Group identifier shared by the iPhone / Watch / Widget targets
/// (spec §69/§71). The task contract pins `group.nerv.center`; the phone
/// target currently resolves its own id independently
/// (`NerveAppGroup.identifier` = `group.com.celestium.nervecenter`), so the
/// parent must align both when the app bundles are wired — see integration
/// notes.
public enum WatchAppGroup {
    public static let identifier = "group.nerv.center"

    /// The §73 cache snapshot file the phone publishes into the container.
    public static let snapshotFileName = "cached-state.json"
    /// The pending-approvals file the phone publishes into the container.
    public static let approvalsFileName = "approvals.json"
    /// The §67 emergency-stop marker the Watch writes.
    public static let stopMarkerFileName = "stop-agents.json"
}

/// Minimal approval row the Watch renders (spec §66). This is a compact
/// *projection* kept in the Watch target (not NerveApps): the shared domain
/// owns the `PlannedChange` wire model; this row carries only what the
/// watch screen needs plus the documents required to reconstruct a
/// `PlannedChange` for the §66 policy evaluation.
public struct WatchApprovalRow: Identifiable, Hashable, Sendable {
    /// Stable id — the underlying planned-change id.
    public var id: String
    public var title: String
    public var summary: String
    public var riskLevel: Int
    /// Wire change-type literal (e.g. `goal_change`); empty when unknown.
    public var changeTypeRaw: String
    /// Wire status literal (e.g. `pending_approval`); empty when unknown.
    public var statusRaw: String
    /// The diff document — kept so a `PlannedChange` can be reconstructed.
    public var diff: [String: NerveJSONValue]
    /// The rollback document — empty means "not reversible", which the §66
    /// policy treats as deep-link-required.
    public var rollback: [String: NerveJSONValue]

    public init(
        id: String,
        title: String,
        summary: String,
        riskLevel: Int,
        changeTypeRaw: String,
        statusRaw: String,
        diff: [String: NerveJSONValue],
        rollback: [String: NerveJSONValue]
    ) {
        self.id = id
        self.title = title
        self.summary = summary
        self.riskLevel = riskLevel
        self.changeTypeRaw = changeTypeRaw
        self.statusRaw = statusRaw
        self.diff = diff
        self.rollback = rollback
    }

    /// Reconstruct the shared-domain `PlannedChange` from this row so
    /// ``WatchApprovalPolicy.evaluate(change:maxRisk:)`` can run. Returns
    /// `nil` when the row lacks a known change-type literal — the screen
    /// must then fall back to `.deepLink`, never guess.
    public func makePlannedChange() -> PlannedChange? {
        guard let changeType = ChangeType(rawValue: changeTypeRaw) else { return nil }
        let status = PlannedChangeStatus(rawValue: statusRaw) ?? .pendingApproval
        return PlannedChange(
            id: id,
            changeType: changeType,
            title: title,
            description: summary.isEmpty ? nil : summary,
            status: status,
            riskLevel: riskLevel,
            subjectIds: [],
            diff: diff,
            rollback: rollback
        )
    }
}

/// Injectable source of the App Group snapshot files. Production uses the
/// App Group `FileManager` container; tests and previews use the in-memory
/// implementation. Keeping the I/O behind this protocol is what lets the
/// Watch stay honest when no snapshot exists: an empty source yields an
/// `.unavailable`-style store state, never fabricated numbers.
public protocol WatchSnapshotSource: Sendable {
    /// Read the §73 cache snapshot (`cached-state.json`). `nil` when the
    /// file is absent or does not decode as a `CachedState`.
    func readSnapshot() -> CachedState?

    /// Read the pending-approvals file (`approvals.json`). `[]` when absent
    /// or undecodable — the Watch then shows no approvals, never invents them.
    func readApprovals() -> [WatchApprovalRow]

    /// Write the §67 emergency-stop marker (`stop-agents.json`) into the
    /// container so the phone can observe the request. Throws on I/O failure.
    func writeStopMarker(at date: Date) throws

    /// Remove the stop marker (tests/previews; the phone owns removal).
    func clearStopMarker() throws
}

/// Production source: the App Group container on `FileManager.default`.
public struct AppGroupSnapshotSource: WatchSnapshotSource {
    public let containerURL: URL

    public init(appGroupIdentifier: String = WatchAppGroup.identifier) {
        let fm = FileManager.default
        if let group = fm.containerURL(
            forSecurityApplicationGroupIdentifier: appGroupIdentifier
        ) {
            self.containerURL = group
        } else {
            // Pre-entitlement / host fallback: Application Support. The
            // Watch still reads honest data — just from a non-shared path —
            // and `readSnapshot()` returns `nil` when nothing was written.
            let base = fm.urls(for: .applicationSupportDirectory, in: .userDomainMask).first
                ?? fm.temporaryDirectory
            self.containerURL = base.appendingPathComponent("NerveCenter", isDirectory: true)
        }
    }

    private var snapshotURL: URL {
        containerURL.appendingPathComponent(WatchAppGroup.snapshotFileName)
    }

    private var approvalsURL: URL {
        containerURL.appendingPathComponent(WatchAppGroup.approvalsFileName)
    }

    private var stopMarkerURL: URL {
        containerURL.appendingPathComponent(WatchAppGroup.stopMarkerFileName)
    }

    public func readSnapshot() -> CachedState? {
        guard let data = try? Data(contentsOf: snapshotURL) else { return nil }
        return NerveJSON.decode(CachedState.self, from: data)
    }

    public func readApprovals() -> [WatchApprovalRow] {
        guard let data = try? Data(contentsOf: approvalsURL) else { return [] }
        return decodeApprovals(data)
    }

    public func writeStopMarker(at date: Date) throws {
        try FileManager.default.createDirectory(
            at: containerURL, withIntermediateDirectories: true
        )
        let marker: [String: Any] = [
            "requested_at": date.ISO8601Format(),
            "control": EmergencyControl.stopAgents.rawValue,
        ]
        let data = try JSONSerialization.data(withJSONObject: marker)
        try data.write(to: stopMarkerURL, options: .atomic)
    }

    public func clearStopMarker() throws {
        try? FileManager.default.removeItem(at: stopMarkerURL)
    }
}

/// Map the shared `approvals.json` wire rows (NerveApps
/// `WatchApprovalsJSON`) onto the watch's compact rows. The wire contract is
/// shared with the phone's publisher — same file name, same snake_case keys;
/// unknown optional fields degrade to honest defaults and a malformed file
/// yields `[]` at the call site.
private func decodeApprovals(_ data: Data) -> [WatchApprovalRow] {
    WatchApprovalsJSON.decode(data).map { wire in
        WatchApprovalRow(
            id: wire.id,
            title: wire.title,
            summary: wire.description ?? "",
            riskLevel: wire.riskLevel,
            changeTypeRaw: wire.changeType,
            statusRaw: wire.status,
            diff: wire.diff ?? [:],
            rollback: wire.rollback ?? [:]
        )
    }
}

/// In-memory source for previews, tests, and hosts without the entitlement.
public final class InMemorySnapshotSource: WatchSnapshotSource, @unchecked Sendable {
    public var snapshot: CachedState?
    public var approvals: [WatchApprovalRow]
    public private(set) var stopMarkers: [Date] = []

    public init(
        snapshot: CachedState? = nil,
        approvals: [WatchApprovalRow] = []
    ) {
        self.snapshot = snapshot
        self.approvals = approvals
    }

    public func readSnapshot() -> CachedState? { snapshot }
    public func readApprovals() -> [WatchApprovalRow] { approvals }

    public func writeStopMarker(at date: Date) throws {
        stopMarkers.append(date)
    }

    public func clearStopMarker() throws {
        stopMarkers.removeAll()
    }
}
