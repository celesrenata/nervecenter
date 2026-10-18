import Foundation

import NerveApps
import NerveModels

// MARK: - §47/§49 Wire contract decode (tolerant)
//
// The backend endpoints return `ListResponse { data: [Any], meta }` for most
// list routes, or a flat object for `POST /api/context/ask`. The web client
// (web/src/api/client.ts) is the reference implementation; the phone mirrors
// it here.
//
// Because the backend is currently a set of 501 stubs (see
// backend/api/endpoints/*.py) the wire decoder is *tolerant*: it accepts both
// the canonical `ListResponse` envelope and bare arrays/objects, and maps the
// snake_case wire fields into the NerveApps contract types.
//
// All decoding is `Sendable`-safe (value types only) so the service can hop
// across the main actor boundary without races.

// MARK: Wire envelope

/// The generic `ListResponse` envelope used by most backend list endpoints.
///
/// Mirrors `backend/api/models.py` `ListResponse`.
struct WireListResponse<T: Decodable>: Decodable {
    var data: [T]
    var meta: WireResponseMeta?
}

/// `meta` block: version / request_id / timestamp / pagination.
struct WireResponseMeta: Decodable {
    var version: String?
    var requestId: String?
    var timestamp: String?
    var pagination: [String: Int]?

    // `request_id` decodes to `requestId` (convertFromSnakeCase)
}

// MARK: Wire item shapes (snake_case on wire, camelCase Swift)

/// A goal as returned by `GET /api/goals` (or `GET /goals`).
///
/// The backend currently returns mock data with `name` / `progress: Int (0–100)`.
/// The canonical contract uses `title` / `progress: Double (0–1)`. The decoder
/// accepts both.
struct WireGoal: Decodable {
    var id: String
    var title: String?
    var name: String?
    var description: String?
    var status: String?
    var progress: Double?
    var priority: Int?
    var deadline: String? // ISO-8601
    var createdAt: String? // ISO-8601

    var displayTitle: String { title ?? name ?? id }

    /// Normalise progress into the 0…1 contract range.
    var normalizedProgress: Double? {
        guard let p = progress else { return nil }
        return p > 1.0 ? p / 100.0 : p
    }
}

/// An incident as returned by `GET /api/incidents`.
///
/// The canonical contract (NerveApps `IncidentDetail`) has eleven sections.
/// The wire stub returns a minimal object; the mapper fills the §59 sections
/// with honest empty values rather than fabricating content.
struct WireIncident: Decodable {
    var id: String
    var title: String?
    var summary: String?
    var severity: String?
    var status: String?
    var createdAt: String? // ISO-8601
    var updatedAt: String? // ISO-8601
    var affectedEntities: [String]?
    var facts: [WireStatement]?
    var hypotheses: [WireStatement]?
    var confidence: Double?
    var supportingEvidence: [String]?
    var investigatingAgents: [String]?
    var recommendedActions: [String]?
    var plannedChanges: [String]?
    var verification: String?
    var timeline: [WireTimelineEntry]?

    var displayTitle: String { title ?? summary ?? id }
}

/// A single fact / hypothesis line on the wire.
struct WireStatement: Decodable {
    var id: String?
    var text: String?
    var statement: String?
    var confidence: Double?
    var evidence: [String]?

    var displayText: String { text ?? statement ?? "" }
}

/// A timeline entry on the wire.
struct WireTimelineEntry: Decodable {
    var id: String?
    var at: String? // ISO-8601
    var text: String?
    var severity: String?
}

/// An agent as returned by `GET /api/agents`.
struct WireAgent: Decodable {
    var id: String
    var name: String?
    var status: String?
    var currentTask: String?
    var task: String?

    var displayName: String { name ?? id }
    var displayStatus: String { status ?? "unknown" }
    var displayTask: String? { currentTask ?? task }
}

/// A system / compute node as returned by `GET /api/systems` or
/// `GET /api/compute`.
struct WireSystem: Decodable {
    /// Live `/api/systems` rows may omit `id` (they carry `name`/`status`
    /// only), so `id` is optional and a stable row id is derived.
    var id: String?
    var name: String?
    var health: String?
    var status: String?
    var utilization: Int?
    var utilizationPercent: Int?
    var hardware: String?

    /// A stable, non-fabricated row identifier: real `id` when present,
    /// otherwise the display name, otherwise a positional placeholder.
    var rowID: String { id ?? name ?? "system" }

    var displayName: String { name ?? id ?? "system" }
    var displayHealth: String { health ?? status ?? "unknown" }
    var displayUtilization: Int? { utilizationPercent ?? utilization }
    var displayHardware: String? { hardware }
}

/// The `POST /api/context/ask` request body.
struct AskRequest: Encodable {
    var question: String
}

/// The `POST /api/context/ask` response.
///
/// Mirrors the web `mockAskResponse` shape: `{ answer, confidence,
/// facts, hypotheses }`. Evidence is carried inline per fact/hypothesis.
struct WireAskResponse: Decodable {
    var answer: String?
    var confidence: Double?
    var facts: [WireStatement]?
    var hypotheses: [WireStatement]?
    var evidence: [WireEvidence]?
    var traceId: String?

    // `trace_id` decodes to `traceId` (convertFromSnakeCase)
}

/// An evidence record as carried inline on the wire (subset of
/// `NerveModels.Evidence`).
struct WireEvidence: Decodable {
    var id: String?
    var sourceType: String?
    var sourceRef: String?
    var content: String?
    var trust: Double?
    var capturedAt: String? // ISO-8601

    // `source_type`, `source_ref`, `captured_at` decode via convertFromSnakeCase
}

// MARK: - Mapping into NerveApps contract types

enum WireMapper {
    /// Map a wire goal into the NerveModels `Goal` contract.
    static func goal(_ w: WireGoal) -> Goal {
        Goal(
            id: w.id,
            title: w.displayTitle,
            description: w.description,
            status: mapGoalStatus(w.status),
            progress: w.normalizedProgress ?? 0.0,
            criteria: [],
            subgoalIds: [],
            priority: w.priority ?? 0,
            deadline: parseDate(w.deadline),
            relatedEntityIds: []
        )
    }

    /// Map a wire incident into the §59 `IncidentDetail` contract.
    ///
    /// All eleven §59 sections are populated; sections with no wire data get
    /// honest empty values (empty arrays / nil) rather than fabricated content.
    static func incident(_ w: WireIncident) -> IncidentDetail {
        let facts: [IncidentStatement] = (w.facts ?? []).map { s in
            IncidentStatement(
                id: s.id ?? UUID().uuidString,
                text: s.displayText,
                confidence: s.confidence,
                evidenceIDs: s.evidence ?? []
            )
        }
        let hypotheses: [IncidentStatement] = (w.hypotheses ?? []).map { s in
            IncidentStatement(
                id: s.id ?? UUID().uuidString,
                text: s.displayText,
                confidence: s.confidence,
                evidenceIDs: s.evidence ?? []
            )
        }
        let timeline: [IncidentTimelineEntry] = (w.timeline ?? []).map { t in
            IncidentTimelineEntry(
                id: t.id ?? UUID().uuidString,
                at: parseDate(t.at) ?? Date(),
                text: t.text ?? "",
                severity: mapSeverity(t.severity)
            )
        }
        return IncidentDetail(
            id: w.id,
            summary: w.displayTitle,
            timeline: timeline,
            affectedEntityIDs: w.affectedEntities ?? [],
            facts: facts,
            hypotheses: hypotheses,
            confidence: w.confidence,
            supportingEvidenceIDs: w.supportingEvidence ?? [],
            investigatingAgentIDs: w.investigatingAgents ?? [],
            recommendedActions: w.recommendedActions ?? [],
            plannedChangeIDs: w.plannedChanges ?? [],
            verification: w.verification
        )
    }

    /// Map a wire ask response into the §58 `AskAnswer` contract.
    static func askAnswer(_ w: WireAskResponse) -> AskAnswer {
        let evidence: [Evidence] = (w.evidence ?? []).compactMap { e in
            guard let ref = e.sourceRef, !ref.isEmpty else { return nil }
            return Evidence(
                id: e.id ?? UUID().uuidString,
                sourceType: mapEvidenceSource(e.sourceType),
                sourceRef: ref,
                content: e.content,
                capturedAt: parseDate(e.capturedAt) ?? Date(),
                trust: e.trust ?? 0.0
            )
        }
        return AskAnswer(
            answer: w.answer ?? "",
            evidence: evidence,
            traceID: w.traceId
        )
    }

    // MARK: - Status / severity / source mapping

    static func mapGoalStatus(_ s: String?) -> GoalStatus {
        switch s?.lowercased() {
        case "active": return .active
        case "paused": return .paused
        case "achieved", "complete": return .achieved
        case "abandoned": return .abandoned
        default: return .active
        }
    }

    static func mapSeverity(_ s: String?) -> EventSeverity {
        switch s?.lowercased() {
        case "debug": return .debug
        case "info": return .info
        case "warning": return .warning
        case "error": return .error
        case "critical": return .critical
        default: return .info
        }
    }

    static func mapEvidenceSource(_ s: String?) -> EvidenceSourceType {
        switch s?.lowercased() {
        case "web_page": return .webPage
        case "document": return .document
        case "transcript": return .transcript
        case "message": return .message
        case "tool_result": return .toolResult
        case "screenshot": return .screenshot
        case "database_row": return .databaseRow
        case "file": return .file
        case "manual_note": return .manualNote
        case "model_output": return .modelOutput
        default: return .other
        }
    }

    /// Parse an ISO-8601 date string. Returns `nil` when the string is nil or
    /// unparseable (honest: the UI shows "Not recorded" for nil dates).
    static func parseDate(_ s: String?) -> Date? {
        guard let s, !s.isEmpty else { return nil }
        // Try ISO-8601 with fractional seconds first, then plain ISO-8601.
        if let d = ISO8601DateFormatter().date(from: s) { return d }
        // Python pydantic sometimes omits fractional seconds
        if let d = iso8601NoFractional.date(from: s) { return d }
        return nil
    }

    static let iso8601NoFractional: DateFormatter = {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.timeZone = TimeZone(identifier: "UTC")
        f.dateFormat = "yyyy-MM-dd'T'HH:mm:ss'Z'"
        return f
    }()
}
