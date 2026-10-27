import Foundation

/// Normalizes a raw `chat.db` row into the wire shape accepted by
/// `POST /api/messages/ingest` (backend IngestRequest, prompt2 §46).
///
/// Fail-closed rules (prompt2 §11 Strategy A):
/// - `date` is nanoseconds since the Darwin epoch (2001-01-01). A value
///   that does not land inside a sane window (2000..2100) is treated as
///   unparsable → the row is **skipped with a reason**, never forwarded
///   with a guessed timestamp.
/// - `guid` must be non-empty (it is the idempotency key on the backend).
/// - The channel is derived from the `service` column; anything we do not
///   recognize is skipped rather than mislabeled.
public enum NormalizationError: Error, Equatable {
    case emptyGUID
    case emptyPeer
    case implausibleDate(nanos: Int64)
    case unknownService(String)
}

public struct NormalizedMessage: Sendable, Equatable {
    public let messageID: String
    public let direction: String
    public let peerIdentifier: String
    public let peerName: String?
    public let body: String
    public let channel: String
    /// ISO-8601 UTC, e.g. `2026-10-13T14:15:00Z`.
    public let timestampISO8601: String
    public let source: String
}

public struct MessageNormalizer: Sendable {
    public static let sourceName = "personal-relay"

    /// Rows whose `date` falls outside this Unix-seconds window are
    /// implausible (clock skew, corrupted row, or a schema change).
    public static let minUnixSeconds: TimeInterval = 946_684_800      // 2000-03-01
    public static let maxUnixSeconds: TimeInterval = 4_102_444_800    // 2100-03-01

    public init() {}

    public func normalize(_ raw: RawMessage) throws -> NormalizedMessage {
        guard !raw.guid.isEmpty else { throw NormalizationError.emptyGUID }
        guard !raw.peerIdentifier.isEmpty else { throw NormalizationError.emptyPeer }

        // Darwin nanoseconds → Unix seconds (fractional kept as .0;
        // Messages timestamps are second-granularity in practice).
        let seconds = Double(raw.dateNanos) / 1_000_000_000.0 + MessageStore.darwinEpochUnix
        let window = MessageNormalizer.minUnixSeconds...MessageNormalizer.maxUnixSeconds
        guard window.contains(seconds) else {
            throw NormalizationError.implausibleDate(nanos: raw.dateNanos)
        }

        guard let channel = Self.channel(for: raw.service) else {
            throw NormalizationError.unknownService(raw.service)
        }

        let direction: String = raw.isFromMe == 1 ? "OUTBOUND" : "INBOUND"
        let date = Date(timeIntervalSince1970: seconds)
        let iso = ISO8601DateFormatter.iso8601Second.string(from: date)

        return NormalizedMessage(
            messageID: raw.guid,
            direction: direction,
            peerIdentifier: raw.peerIdentifier,
            peerName: nil, // local contact resolution arrives with the contacts MVP
            body: raw.body,
            channel: channel,
            timestampISO8601: iso,
            source: Self.sourceName
        )
    }

    /// Maps the Messages `service` column to the backend `channel` field.
    /// `iMessage` → `imessage`; SMS/MMS services → `sms`. Unknown values
    /// return nil so the caller can skip (fail closed) instead of guess.
    public static func channel(for service: String) -> String? {
        switch service.trimmingCharacters(in: .whitespacesAndNewlines).lowercased() {
        case "imessage":
            return "imessage"
        case "sms", "mms", "messaging":
            return "sms"
        default:
            return nil
        }
    }
}

