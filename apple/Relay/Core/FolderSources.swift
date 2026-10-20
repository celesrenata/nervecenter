import Foundation

// =============================================================================
// FolderChannelSource — production capture path for the D5' planes.
//
// Each plane reads a local capture file `<source_dir>/<channel>.json`
// (an array of records with the snake_case wire keys the backend expects).
// The file is read-only: capture never modifies or deletes it. Dedupe is the
// engine's local `seen` index (§70: re-yielding a record is safe; no local
// data loss), so an operator (or a future capture helper) can re-run capture
// against the same file without double-sending.
//
// For email, this is the mailbox-capture half of the mail-connector plane:
// the mailbox itself stays on the user's machine; the connector registry
// (`GET /api/mail-connectors`, see `MailConnectorRegistryClient`) records
// which mailbox answers.
// =============================================================================

/// A local folder capture source for one plane.
public struct FolderChannelSource: ChannelCaptureSource {
    public let channel: RelayChannel
    public let sourceDir: URL

    public init(channel: RelayChannel, sourceDir: URL) {
        self.channel = channel
        self.sourceDir = sourceDir
    }

    /// The capture file for this channel (e.g. `email.json`).
    public var fileURL: URL {
        sourceDir.appendingPathComponent("\(channel.rawValue).json")
    }

    public func fetch() async throws -> [ChannelRecord] {
        guard FileManager.default.fileExists(atPath: fileURL.path) else {
            return []  // nothing captured this interval
        }
        let data = try Data(contentsOf: fileURL)
        // Decoding is exact: the row structs carry explicit CodingKeys that
        // match the snake_case wire keys. (Foundation's `convertFromSnakeCase`
        // is a lossy heuristic — it maps `message_id` to `messageId`, not
        // `messageID`, so ID-suffixed properties would silently fail to
        // decode.)
        let decoder = JSONDecoder()
        switch channel {
        case .email:
            let rows = try decoder.decode([EmailCaptureRow].self, from: data)
            return rows.map { r in
                .email(MailMessage(
                    messageID: r.messageID,
                    direction: r.direction,
                    fromAddress: r.fromAddress,
                    toAddress: r.toAddress ?? "",
                    senderName: r.senderName,
                    subject: r.subject,
                    body: r.body ?? "",
                    timestampUnix: r.timestampUnix,
                    source: r.source ?? "personal-relay"
                ))
            }
        case .voicemail:
            let rows = try decoder.decode([VoicemailCaptureRow].self, from: data)
            return rows.map { r in
                .voicemail(VoicemailCapture(
                    voicemailID: r.voicemailID,
                    callerPhone: r.callerPhone,
                    callerName: r.callerName,
                    transcript: r.transcript ?? "",
                    durationSeconds: r.durationSeconds ?? 0,
                    receivedAtUnix: r.receivedAtUnix,
                    source: r.source ?? "imported"
                ))
            }
        case .calendar:
            let rows = try decoder.decode([CalendarEventCaptureRow].self, from: data)
            return rows.map { r in
                .calendar(CalendarEventCapture(
                    eventID: r.eventID,
                    title: r.title,
                    description: r.description,
                    startUnix: r.startUnix,
                    endUnix: r.endUnix,
                    allDay: r.allDay ?? false,
                    location: r.location,
                    participants: r.participants ?? [],
                    reminderAtUnix: r.reminderAtUnix,
                    source: r.source ?? "calendar-sync"
                ))
            }
        case .contacts:
            let rows = try decoder.decode([ContactCaptureRow].self, from: data)
            return rows.map { r in
                .contact(ContactCapture(
                    personID: r.personID,
                    displayName: r.displayName,
                    emails: r.emails ?? [],
                    phones: r.phones ?? [],
                    notes: r.notes,
                    isVIP: r.isVIP ?? false,
                    source: r.source ?? "personal-relay",
                    updatedAtUnix: r.updatedAtUnix
                ))
            }
        }
    }
}

// -- wire rows (snake_case JSON → convertFromSnakeCase) -----------------------

private struct EmailCaptureRow: Decodable {
    let messageID: String
    let direction: String
    let fromAddress: String
    let toAddress: String?
    let senderName: String?
    let subject: String
    let body: String?
    let timestampUnix: TimeInterval
    let source: String?
    enum CodingKeys: String, CodingKey {
        case messageID = "message_id"
        case direction
        case fromAddress = "from_address"
        case toAddress = "to_address"
        case senderName = "sender_name"
        case subject
        case body
        case timestampUnix = "timestamp_unix"
        case source
    }
}

private struct VoicemailCaptureRow: Decodable {
    let voicemailID: String
    let callerPhone: String
    let callerName: String?
    let transcript: String?
    let durationSeconds: Int?
    let receivedAtUnix: TimeInterval
    let source: String?
    enum CodingKeys: String, CodingKey {
        case voicemailID = "voicemail_id"
        case callerPhone = "caller_phone"
        case callerName = "caller_name"
        case transcript
        case durationSeconds = "duration_seconds"
        case receivedAtUnix = "received_at_unix"
        case source
    }
}

private struct CalendarEventCaptureRow: Decodable {
    let eventID: String
    let title: String
    let description: String?
    let startUnix: TimeInterval?
    let endUnix: TimeInterval?
    let allDay: Bool?
    let location: String?
    let participants: [String]?
    let reminderAtUnix: TimeInterval?
    let source: String?
    enum CodingKeys: String, CodingKey {
        case eventID = "event_id"
        case title
        case description
        case startUnix = "start_unix"
        case endUnix = "end_unix"
        case allDay = "all_day"
        case location
        case participants
        case reminderAtUnix = "reminder_at_unix"
        case source
    }
}

private struct ContactCaptureRow: Decodable {
    let personID: String
    let displayName: String
    let emails: [String]?
    let phones: [String]?
    let notes: String?
    let isVIP: Bool?
    let source: String?
    enum CodingKeys: String, CodingKey {
        case personID = "person_id"
        case displayName = "display_name"
        case emails
        case phones
        case notes
        case isVIP = "is_vip"
        case source
        case updatedAtUnix = "updated_at_unix"
    }
    let updatedAtUnix: TimeInterval?
}
