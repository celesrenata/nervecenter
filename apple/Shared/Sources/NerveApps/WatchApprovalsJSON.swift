import Foundation

import NerveModels

// MARK: - §66 watch `approvals.json` wire contract (phone → Watch, App Group)

/// The pending-approval rows the phone publishes into the shared App Group
/// container so the Watch's §66 Approvals screen can render them.
///
/// This is the *shared wire contract* for the `approvals.json` file: the
/// phone (publisher) and the Watch (consumer) both use `ApprovalWireRow` /
/// `WatchApprovalsJSON` instead of each defining the shape privately. The
/// wire keys are snake_case — the exact literals the shared
/// `PlannedChange` contract (NerveAuth `ChangeType` /
/// `PlannedChangeStatus`) and the Python `schemas/planned_changes.py`
/// literal sets use — so a row the phone reads from a backend can be
/// projected into this shape without translation.
///
/// Honesty contract: the phone always writes this file on the same cadence
/// as `cached-state.json` (NervePhone `PhoneEnvironment.publishToAppGroup`).
/// When the phone holds no rows the file contains `[]` — a *valid* empty
/// array, which the Watch decodes as "no approvals pending". The Watch
/// never invents rows, and a missing/malformed file degrades to `[]`.
public enum WatchApprovalsJSON {
    /// The file name inside the App Group container (`group.nerv.center`).
    public static let fileName = "approvals.json"

    /// Encode an approvals array to `approvals.json` bytes. Returns `nil` on
    /// (impossible) encoding failure — the caller then skips the write and
    /// the file keeps its previous content, which the Watch still decodes
    /// honestly.
    public static func encode(_ rows: [ApprovalWireRow]) -> Data? {
        NerveJSON.encode(rows)
    }

    /// Decode `approvals.json` bytes. `[]` when the data is absent, not a
    /// JSON array, or fails to decode — the caller (the Watch) then shows
    /// no approvals, never invents them.
    public static func decode(_ data: Data) -> [ApprovalWireRow] {
        NerveJSON.decode([ApprovalWireRow].self, from: data) ?? []
    }
}

/// One pending-approval row on the `approvals.json` wire.
///
/// A compact projection of the shared `PlannedChange` domain model (NerveAuth)
/// carrying exactly what the Watch screen needs plus the documents required
/// to reconstruct a `PlannedChange` for the §66 policy evaluation. Unknown
/// optional fields degrade to honest `nil`/empty defaults instead of failing
/// the whole file.
public struct ApprovalWireRow: Codable, Hashable, Sendable {
    /// Stable id — the underlying planned-change id.
    public var id: String
    /// Short human-readable title of the change.
    public var title: String
    /// Full description; `nil` when the phone has none.
    public var description: String?
    /// Risk severity on the 0–4 scale.
    public var riskLevel: Int
    /// Wire change-type literal (e.g. `goal_change`).
    public var changeType: String
    /// Wire status literal (e.g. `pending_approval`).
    public var status: String
    /// The diff document — kept so a `PlannedChange` can be reconstructed.
    public var diff: [String: NerveJSONValue]?
    /// The rollback document — empty/`nil` means "not reversible", which the
    /// §66 policy treats as deep-link-required.
    public var rollback: [String: NerveJSONValue]?

    public init(
        id: String,
        title: String,
        description: String? = nil,
        riskLevel: Int,
        changeType: String,
        status: String,
        diff: [String: NerveJSONValue]? = nil,
        rollback: [String: NerveJSONValue]? = nil
    ) {
        self.id = id
        self.title = title
        self.description = description
        self.riskLevel = riskLevel
        self.changeType = changeType
        self.status = status
        self.diff = diff
        self.rollback = rollback
    }
}
