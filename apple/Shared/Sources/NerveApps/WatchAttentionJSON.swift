import Foundation

import NerveModels

// MARK: - §27/§67 watch `cached-state.json` attention wire contract (phone → Watch, App Group)

/// One ranked attention item on the App Group wire.
///
/// A compact *projection* of the backend `AttentionItem` (Python
/// `backend/api/models.py::AttentionItem`, `model_dump(mode="json")`)
/// carrying exactly what the watch screen needs (spec §27: state + urgency +
/// why). Wire keys are snake_case and match the Pydantic schema field names
/// exactly, so a row the phone reads from `GET /api/attention/now` can be
/// projected into this shape without translation — the same convention as
/// `ApprovalWireRow` / `WatchApprovalsJSON` (D6 contract).
///
/// Where the backend field is optional (`due`, `suggested_action`), the wire
/// field is optional too and decodes to `nil` when absent; the encoder omits
/// `null` members (NerveJSON `exclude_none` parity).
public struct AttentionWireItem: Codable, Hashable, Sendable {
    /// Stable id — the backend `attention_id`.
    public var attentionID: String
    /// The one-line "what is this" (backend `reason`).
    public var reason: String
    /// 0...1 urgency (§21 scorer).
    public var urgency: Double
    /// 0...1 importance (§21 scorer).
    public var importance: Double
    /// 0...1 combined priority (§21 scorer) — the ranking key.
    public var priority: Double
    /// When the thing is due (UTC). `nil` when the item has no deadline.
    public var due: Date?
    /// Lifecycle state literal (backend `AttentionState`): `NOW` / `TODAY` /
    /// `SOON` / `WAITING_ON` / `FYI` / `SUPPRESSED` / `RESOLVED`.
    public var status: String
    /// Explainability (§22): human-readable reasons behind the ranking.
    public var why: [String]
    /// Suggested wrist action (backend `suggested_action`).
    public var suggestedAction: String?
    /// Entities the item concerns (people, systems).
    public var entities: [String]

    public init(
        attentionID: String,
        reason: String,
        urgency: Double = 0,
        importance: Double = 0,
        priority: Double = 0,
        due: Date? = nil,
        status: String,
        why: [String] = [],
        suggestedAction: String? = nil,
        entities: [String] = []
    ) {
        self.attentionID = attentionID
        self.reason = reason
        self.urgency = urgency
        self.importance = importance
        self.priority = priority
        self.due = due
        self.status = status
        self.why = why
        self.suggestedAction = suggestedAction
        self.entities = entities
    }

    // Custom keys match the *post-conversion* camelCase names NerveJSON's
    // snake⇄camel strategies produce (`attention_id` ⇄ `attentionId`; the
    // default `attentionID` spelling would not match the converter's output).
    // Decoding is tolerant of missing optional fields so a wire evolution
    // never blanks the whole file.
    private enum CodingKeys: String, CodingKey {
        case attentionID = "attentionId"
        case reason
        case urgency
        case importance
        case priority
        case due
        case status
        case why
        case suggestedAction
        case entities
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        attentionID = try c.decode(String.self, forKey: .attentionID)
        reason = try c.decode(String.self, forKey: .reason)
        urgency = try c.decodeIfPresent(Double.self, forKey: .urgency) ?? 0
        importance = try c.decodeIfPresent(Double.self, forKey: .importance) ?? 0
        priority = try c.decodeIfPresent(Double.self, forKey: .priority) ?? 0
        due = try c.decodeIfPresent(Date.self, forKey: .due)
        status = try c.decodeIfPresent(String.self, forKey: .status) ?? ""
        why = try c.decodeIfPresent([String].self, forKey: .why) ?? []
        suggestedAction = try c.decodeIfPresent(String.self, forKey: .suggestedAction)
        entities = try c.decodeIfPresent([String].self, forKey: .entities) ?? []
    }

    public func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encode(attentionID, forKey: .attentionID)
        try c.encode(reason, forKey: .reason)
        try c.encode(urgency, forKey: .urgency)
        try c.encode(importance, forKey: .importance)
        try c.encode(priority, forKey: .priority)
        try c.encodeIfPresent(due, forKey: .due)
        try c.encode(status, forKey: .status)
        try c.encode(why, forKey: .why)
        try c.encodeIfPresent(suggestedAction, forKey: .suggestedAction)
        try c.encode(entities, forKey: .entities)
    }
}

/// The `attention` member of the `cached-state.json` snapshot object.
///
/// The phone publishes the array on the *same cadence* as the rest of the
/// snapshot (NervePhone `PhoneEnvironment.publishToAppGroup`); it is the
/// list of open attention items (NOW-first ordering) from the backend
/// attention queue. Three honest possibilities are kept distinct:
///
/// - key **absent** from the snapshot → attention not published yet (older
///   phone, or the personal plane not reached);
/// - key present as `[]` → published, and nothing needs the user;
/// - file missing/malformed → no state at all (the §61 honesty rule).
public struct AttentionFile: Codable, Hashable, Sendable {
    /// `nil` when the key is absent (attention not published).
    public var attention: [AttentionWireItem]?

    public init(attention: [AttentionWireItem]?) {
        self.attention = attention
    }
}

// MARK: - Watch → phone action records (App Group write-back, §60)

/// The wrist actions the watch can perform on an attention item (spec §27 /
/// §60). These are *records*, not network calls: the wrist writes them into
/// the App Group and the phone — the relay — executes them against the
/// backend attention actions (`POST /api/attention/{id}/snooze|resolve`) on
/// its next sync, then removes the processed record. The same
/// marker-file pattern as `stop-agents.json`.
public enum AttentionActionKind: String, Codable, CaseIterable, Hashable, Sendable {
    /// Defer the item (backend `snooze`; default window 1 h, §61 "[Snooze 1h]").
    case snooze
    /// Mark handled (backend `resolve`).
    case resolve
    /// Escalate: request that the phone surface the item to the user
    /// (notification / open) — the phone observes the record.
    case escalate
}

/// One wrist action the Watch recorded for an attention item.
public struct AttentionActionRecord: Codable, Hashable, Sendable, Identifiable {
    /// The attention item the action applies to.
    public var attentionID: String
    /// The action performed at the wrist.
    public var action: AttentionActionKind
    /// When the wearer performed it (UTC).
    public var requestedAt: Date
    /// Optional snooze window in minutes (absent → the phone applies the
    /// backend default).
    public var snoozeMinutes: Int?
    /// Short note from the wrist context (kept for the audit trail, §71).
    public var note: String?

    public var id: String { attentionID + ":" + action.rawValue }

    public init(
        attentionID: String,
        action: AttentionActionKind,
        requestedAt: Date,
        snoozeMinutes: Int? = nil,
        note: String? = nil
    ) {
        self.attentionID = attentionID
        self.action = action
        self.requestedAt = requestedAt
        self.snoozeMinutes = snoozeMinutes
        self.note = note
    }

    private enum CodingKeys: String, CodingKey {
        case attentionID = "attentionId"
        case action
        case requestedAt
        case snoozeMinutes
        case note
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        attentionID = try c.decode(String.self, forKey: .attentionID)
        action = try c.decode(AttentionActionKind.self, forKey: .action)
        requestedAt = try c.decode(Date.self, forKey: .requestedAt)
        snoozeMinutes = try c.decodeIfPresent(Int.self, forKey: .snoozeMinutes)
        note = try c.decodeIfPresent(String.self, forKey: .note)
    }

    public func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encode(attentionID, forKey: .attentionID)
        try c.encode(action, forKey: .action)
        try c.encode(requestedAt, forKey: .requestedAt)
        try c.encodeIfPresent(snoozeMinutes, forKey: .snoozeMinutes)
        try c.encodeIfPresent(note, forKey: .note)
    }
}

// MARK: - File-level contract

/// The App Group file conventions for attention (sibling of
/// `WatchApprovalsJSON`).
public enum WatchAttentionJSON {
    /// The attention array lives *inside* the §73 snapshot file — one
    /// publish cadence, one file to keep honest (spec §73 naming).
    public static let snapshotFileName = "cached-state.json"
    /// The wire key inside the snapshot file that carries the items.
    public static let attentionKey = "attention"
    /// The watch → phone action-marker file (the §60 write-back; same
    /// pattern as `stop-agents.json`).
    public static let actionsFileName = "watch-actions.json"

    /// Decode the `attention` key out of `cached-state.json` bytes.
    ///
    /// Returns `nil` when the bytes do not decode as a JSON object at all
    /// (malformed / wrong shape) — the caller maps that to the "malformed"
    /// availability state. An absent `attention` key decodes to
    /// `AttentionFile(attention: nil)` (attention not published); a present
    /// `[]` decodes to `AttentionFile(attention: [])` (honestly empty).
    /// Decoding only the `attention` member keeps extra snapshot siblings
    /// (and future ones) from breaking attention decoding.
    public static func decodeAttentionFile(from data: Data) -> AttentionFile? {
        NerveJSON.decode(AttentionFile.self, from: data)
    }

    /// Encode action records to `watch-actions.json` bytes. Returns `nil` on
    /// (impossible) encoding failure — the caller then skips the write and
    /// the phone keeps seeing the previous records.
    public static func encodeActions(_ records: [AttentionActionRecord]) -> Data? {
        NerveJSON.encode(records)
    }

    /// Decode `watch-actions.json` bytes. `[]` when the data is absent, not
    /// an array, or fails to decode — the Watch then shows no pending
    /// actions, never invents them.
    public static func decodeActions(_ data: Data) -> [AttentionActionRecord] {
        NerveJSON.decode([AttentionActionRecord].self, from: data) ?? []
    }
}
