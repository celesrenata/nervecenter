import Foundation

import NerveModels

// MARK: - §68/§69 Personal widget content (gaps4 D4' / T003-09)

/// Tolerant wire subset of the *personal* fields the phone may publish into
/// the shared App Group `cached-state.json` (spec §68/§69 compact state).
///
/// Every field is optional so a fresh install — where the phone has not yet
/// published any personal keys — decodes cleanly instead of failing the whole
/// file (a `cached-state.json` that only carries the engineering fields still
/// decodes, with every personal field `nil`). The wire keys are snake_case to
/// match §69 naming (see `CachedState`, which states "Wire fields are
/// snake_case to match §69 compact state naming"); `NerveJSON` decodes them to
/// these camelCase properties via `convertFromSnakeCase`.
///
/// Decoding is *additive*: it ignores the engineering fields that already live
/// in the same file, so this struct can coexist with the existing `CachedState`
/// without either type knowing about the other.
public struct PersonalStateWire: Codable, Hashable, Sendable {
    /// Number of items currently in the user's attention queue.
    public var attentionCount: Int?
    /// The single most important attention item's title.
    public var topAttentionTitle: String?
    /// The top attention item's urgency score (0...1).
    public var topAttentionUrgency: Double?
    /// The single most important open commitment's title.
    public var topCommitmentTitle: String?
    /// When the top commitment is due.
    public var topCommitmentDueAt: Date?
    /// Number of things the user is waiting on (others to act).
    public var waitingOnCount: Int?

    public init(
        attentionCount: Int? = nil,
        topAttentionTitle: String? = nil,
        topAttentionUrgency: Double? = nil,
        topCommitmentTitle: String? = nil,
        topCommitmentDueAt: Date? = nil,
        waitingOnCount: Int? = nil
    ) {
        self.attentionCount = attentionCount
        self.topAttentionTitle = topAttentionTitle
        self.topAttentionUrgency = topAttentionUrgency
        self.topCommitmentTitle = topCommitmentTitle
        self.topCommitmentDueAt = topCommitmentDueAt
        self.waitingOnCount = waitingOnCount
    }

    /// `true` when the wire carried at least one personal field — i.e. the
    /// phone has *published* its personal state, as opposed to a fresh install
    /// where the file simply lacks the keys. This is what lets a genuinely
    /// empty queue read as "Clear" without a fresh install faking one.
    public var hasAnyField: Bool {
        attentionCount != nil
            || topAttentionTitle != nil
            || topAttentionUrgency != nil
            || topCommitmentTitle != nil
            || topCommitmentDueAt != nil
            || waitingOnCount != nil
    }

    /// Decode a personal wire snapshot from raw `cached-state.json` bytes.
    ///
    /// Returns `nil` only when the bytes are not a JSON object (corrupt file);
    /// a valid object with no personal keys returns a wire whose fields are all
    /// `nil` (and `hasAnyField == false`).
    public static func decode(from data: Data) -> PersonalStateWire? {
        NerveJSON.decode(PersonalStateWire.self, from: data)
    }
}

/// The honest, renderable personal section of a widget tile (spec §68/§69).
///
/// Count aggregation, urgency bucketing, and presence tracking happen here,
/// platform-free and testable. Views render a given block only when its
/// presence flag is set, so a fresh install renders an honest empty state
/// (nothing fabricated), while a genuinely empty queue can honestly read
/// "Clear".
public struct PersonalWidgetContent: Codable, Hashable, Sendable {
    // Attention (spec §67/§68 "N need you").
    public var attentionCount: Int
    public var topAttentionTitle: String?
    public var topAttentionUrgency: Double?
    /// `true` when the phone published at least one attention field — lets a
    /// known-empty queue render "Clear" without a fresh install faking it.
    public var hasAttention: Bool

    // Top commitment (§69 "Next deadline").
    public var topCommitmentTitle: String?
    public var topCommitmentDueAt: Date?
    /// `true` when the phone published at least one commitment field.
    public var hasCommitment: Bool

    // Waiting-on (§69 "N waiting").
    public var waitingOnCount: Int
    /// `true` when the phone published the waiting-on count.
    public var hasWaiting: Bool

    public init(
        attentionCount: Int = 0,
        topAttentionTitle: String? = nil,
        topAttentionUrgency: Double? = nil,
        hasAttention: Bool = false,
        topCommitmentTitle: String? = nil,
        topCommitmentDueAt: Date? = nil,
        hasCommitment: Bool = false,
        waitingOnCount: Int = 0,
        hasWaiting: Bool = false
    ) {
        self.attentionCount = max(0, attentionCount)
        self.topAttentionTitle = PersonalWidgetContent.trimmed(topAttentionTitle)
        self.topAttentionUrgency = topAttentionUrgency.map { min(1.0, max(0.0, $0)) }
        self.hasAttention = hasAttention
        self.topCommitmentTitle = PersonalWidgetContent.trimmed(topCommitmentTitle)
        self.topCommitmentDueAt = topCommitmentDueAt
        self.hasCommitment = hasCommitment
        self.waitingOnCount = max(0, waitingOnCount)
        self.hasWaiting = hasWaiting
    }

    /// An honest, empty personal section (fresh install / nothing published).
    /// Every presence flag is `false`, so no block renders at all.
    public static let empty = PersonalWidgetContent()

    /// Derive honest content from a (possibly empty) wire snapshot.
    ///
    /// Absent wire fields map to honest zero/nil — never to invented data.
    /// Negative counts clamp to zero, urgency clamps to 0...1, blank titles
    /// become `nil`, and each presence flag mirrors whether the wire carried
    /// that block's fields.
    public static func make(from wire: PersonalStateWire) -> PersonalWidgetContent {
        PersonalWidgetContent(
            attentionCount: wire.attentionCount ?? 0,
            topAttentionTitle: wire.topAttentionTitle,
            topAttentionUrgency: wire.topAttentionUrgency,
            hasAttention: wire.attentionCount != nil
                || wire.topAttentionTitle != nil
                || wire.topAttentionUrgency != nil,
            topCommitmentTitle: wire.topCommitmentTitle,
            topCommitmentDueAt: wire.topCommitmentDueAt,
            hasCommitment: wire.topCommitmentTitle != nil || wire.topCommitmentDueAt != nil,
            waitingOnCount: wire.waitingOnCount ?? 0,
            hasWaiting: wire.waitingOnCount != nil
        )
    }

    /// Convenience derivation straight from raw `cached-state.json` bytes.
    /// Returns ``empty`` when the bytes are not a valid JSON object.
    public static func make(from data: Data) -> PersonalWidgetContent {
        guard let wire = PersonalStateWire.decode(from: data) else { return .empty }
        return make(from: wire)
    }

    /// `true` when the phone published *any* personal field at all.
    public var hasData: Bool { hasAttention || hasCommitment || hasWaiting }

    /// `true` when at least one item needs the user's attention.
    public var needsAttention: Bool { attentionCount > 0 }

    /// The §67/§68 "needs you" line for compact tiles, or `nil` when the phone
    /// never published attention state (fresh install — no misleading
    /// "0 need you" or a "Clear" we did not observe).
    ///
    /// - no attention data published → `nil`
    /// - items in the queue           → `"N need you"`
    /// - queue empty, data known      → `"Clear"`
    public var attentionLine: String? {
        guard hasAttention else { return nil }
        return attentionCount > 0 ? "\(attentionCount) need you" : "Clear"
    }

    /// The §69 waiting-on line, or `nil` when never published.
    public var waitingLine: String? {
        guard hasWaiting else { return nil }
        return "\(waitingOnCount) waiting"
    }

    /// The combined personal action count (attention + waiting-on), for the
    /// compact §69 "N actions" style line.
    public var actionCount: Int { attentionCount + waitingOnCount }

    /// Urgency bucket for the top attention item, or `nil` when no urgency is
    /// known. Matches the backend attention policy's `URGENT_THRESHOLD`
    /// (`backend/attention/policy.py`) so the widget does not over- or
    /// under-flag items relative to the app.
    public var urgencyLabel: String? {
        guard let urgency = topAttentionUrgency else { return nil }
        return urgency >= Self.urgentThreshold ? "urgent" : "normal"
    }

    /// Urgency threshold mirrored from the backend attention policy
    /// (`URGENT_THRESHOLD = 0.7` in `backend/attention/policy.py`).
    public static let urgentThreshold: Double = 0.7

    /// A human-readable due label for the top commitment, or `nil` when there
    /// is no due time.
    ///
    /// `reference` is injectable so tests can assert deterministic output; it
    /// defaults to `Date()` in production rendering.
    public func dueLabel(reference: Date = Date()) -> String? {
        guard let due = topCommitmentDueAt else { return nil }
        let calendar = Calendar(identifier: .gregorian)
        let startToday = calendar.startOfDay(for: reference)
        let startDue = calendar.startOfDay(for: due)
        let days = calendar.dateComponents([.day], from: startToday, to: startDue).day ?? 0
        switch days {
        case let dayCount where dayCount < 0:
            return "overdue"
        case 0:
            return "today"
        case 1:
            return "tomorrow"
        default:
            return Self.dueFormatter.string(from: due)
        }
    }

    private static func trimmed(_ value: String?) -> String? {
        guard let value else { return nil }
        let trimmed = value.trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmed.isEmpty ? nil : trimmed
    }

    private static let dueFormatter: DateFormatter = {
        let formatter = DateFormatter()
        // Fixed locale/time zone so the label is deterministic regardless of
        // the widget process's environment.
        formatter.locale = Locale(identifier: "en_US")
        formatter.timeZone = TimeZone(identifier: "UTC")
        formatter.dateStyle = .short
        formatter.timeStyle = .short
        return formatter
    }()
}
