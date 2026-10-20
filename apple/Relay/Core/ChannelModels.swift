import Foundation

// =============================================================================
// Phase 4 D5' — additional Personal Operations capture planes (T003-10/11,
// G-2.4).  Each plane shares the same *normalized record → per-channel
// outbox → flush to NerveCenter* shape as the existing Messages relay, so a
// transport failure never loses local history (§70 local-first) and replay is
// idempotent end-to-end (§48).
//
//   email      → POST /api/email/ingest         (mail-connector plane, §75)
//   voicemail  → POST /api/voicemail/ingest     (§77)
//   calendar   → POST /api/calendar/ingest      (§42/§43)
//   contacts   → POST /api/contacts/ingest      (§79)
//
// Credentials (bearer token for NerveCenter, and the optional mailbox token)
// come from the environment only — they are NEVER written into the config
// template file (§47 "no remote config, secrets via env/keychain").
// =============================================================================

/// Env var that carries the *mailbox* credential (OAuth access token / app
/// password) used by the mail-connector capture plane. Distinct from
/// `NERVE_RELAY_TOKEN` (the NerveCenter access token). Never persisted.
public let NerveRelayMailTokenEnvVar = "NERVE_RELAY_MAIL_TOKEN"

/// A capture plane owned by the relay. `outboxSlug` is the per-channel
/// SQLite outbox filename; `endpoint` is the backend ingest path.
public enum RelayChannel: String, CaseIterable, Sendable, Hashable {
    case email
    case voicemail
    case calendar
    case contacts

    /// Per-channel outbox filename (one durable SQLite file per plane so a
    /// backlog on one channel can never starve or corrupt another).
    public var outboxSlug: String {
        switch self {
        case .email: return "email.db"
        case .voicemail: return "voicemail.db"
        case .calendar: return "calendar.db"
        case .contacts: return "contacts.db"
        }
    }

    /// Backend ingest endpoint (relative, appended to the base URL).
    public var endpoint: [String] {
        switch self {
        case .email: return ["api", "email", "ingest"]
        case .voicemail: return ["api", "voicemail", "ingest"]
        case .calendar: return ["api", "calendar", "ingest"]
        case .contacts: return ["api", "contacts", "ingest"]
        }
    }
}

/// Default retention bounds (§70 local-first: bounded *local* history, but
/// **never** delete on send-failure — only prune already-acknowledged, aged
/// archive rows; bounds come from config).
public enum RelayRetention {
    /// Default cap on retained *delivered* entries per channel (0 = no cap).
    public static let defaultMaxEntries = 5_000
    /// Default max age (days) of a retained *delivered* entry (0 = no cap).
    public static let defaultDays = 90
}

/// Per-channel configuration. Channels are **disabled by default** so an
/// operator opts in explicitly; tokens are env-only (never in this struct's
/// persisted representation).
public struct ChannelConfig: Sendable, Equatable {
    public var enabled: Bool
    public var batchSize: Int
    public var interval: TimeInterval
    /// Cap on retained *delivered* entries per channel (0 = no cap).
    public var maxEntries: Int
    /// Cap on age (days) of retained *delivered* entries (0 = no cap).
    public var retentionDays: Int
    /// Local capture source directory (folder-style sources).
    public var sourceDir: URL

    public init(
        enabled: Bool = false,
        batchSize: Int = 200,
        interval: TimeInterval = 30,
        maxEntries: Int = RelayRetention.defaultMaxEntries,
        retentionDays: Int = RelayRetention.defaultDays,
        sourceDir: URL = ChannelConfig.defaultSourceDirURL
    ) {
        self.enabled = enabled
        self.batchSize = max(1, batchSize)
        self.interval = max(1, interval)
        self.maxEntries = max(0, maxEntries)
        self.retentionDays = max(0, retentionDays)
        self.sourceDir = sourceDir
    }

    public static let defaultSourceDir = "~/.nervcenter/relay/capture"
    /// Expanded (`~/` → home) so the default capture dir is absolute,
    /// like every other path the relay writes to (§47 user-account scope).
    public static let defaultSourceDirURL = RelayConfig.expand(defaultSourceDir)
}

/// The set of per-channel settings carried by `RelayConfig`.
public struct ChannelConfigs: Sendable, Equatable {
    public var email: ChannelConfig
    public var voicemail: ChannelConfig
    public var calendar: ChannelConfig
    public var contacts: ChannelConfig

    public init(
        email: ChannelConfig = ChannelConfig(),
        voicemail: ChannelConfig = ChannelConfig(),
        calendar: ChannelConfig = ChannelConfig(),
        contacts: ChannelConfig = ChannelConfig()
    ) {
        self.email = email
        self.voicemail = voicemail
        self.calendar = calendar
        self.contacts = contacts
    }

    /// Channels the operator enabled, in a stable order.
    public var enabledChannels: [RelayChannel] {
        var out: [RelayChannel] = []
        if email.enabled { out.append(.email) }
        if voicemail.enabled { out.append(.voicemail) }
        if calendar.enabled { out.append(.calendar) }
        if contacts.enabled { out.append(.contacts) }
        return out
    }

    public subscript(_ channel: RelayChannel) -> ChannelConfig {
        get {
            switch channel {
            case .email: return email
            case .voicemail: return voicemail
            case .calendar: return calendar
            case .contacts: return contacts
            }
        }
        set {
            switch channel {
            case .email: email = newValue
            case .voicemail: voicemail = newValue
            case .calendar: calendar = newValue
            case .contacts: contacts = newValue
            }
        }
    }
}

// =============================================================================
// Per-plane normalized records (same normalized message contract: stable id,
// provenance `source`, real timestamps, human-readable body).
// =============================================================================

/// A normalized email as captured from the local mailbox (§75).
public struct MailMessage: Sendable, Equatable {
    /// Stable, unique id for idempotent ingest (Message-Id or mailbox UID).
    public let messageID: String
    public let direction: String      // "RECEIVED" | "SENT"
    public let fromAddress: String
    public let toAddress: String
    public let senderName: String?
    public let subject: String
    public let body: String
    /// Seconds since Unix epoch.
    public let timestampUnix: TimeInterval
    public let source: String
}

/// A captured voicemail (§77).
public struct VoicemailCapture: Sendable, Equatable {
    public let voicemailID: String
    public let callerPhone: String
    public let callerName: String?
    public let transcript: String
    public let durationSeconds: Int
    public let receivedAtUnix: TimeInterval
    public let source: String   // "share-sheet" | "carrier-email" | "imported"
}

/// A captured calendar event (§42/§43).
public struct CalendarEventCapture: Sendable, Equatable {
    public let eventID: String
    public let title: String
    public let description: String?
    public let startUnix: TimeInterval?
    public let endUnix: TimeInterval?
    public let allDay: Bool
    public let location: String?
    public let participants: [String]
    public let reminderAtUnix: TimeInterval?
    public let source: String
}

/// A captured, resolved person (§79).
public struct ContactCapture: Sendable, Equatable {
    public let personID: String
    public let displayName: String
    public let emails: [String]
    public let phones: [String]
    public let notes: String?
    public let isVIP: Bool
    public let source: String
    public let updatedAtUnix: TimeInterval?
}

/// The normalized unit every capture plane yields. The normalizer maps a
/// record to the exact backend ingest payload for its plane; the engine
/// enqueues it to the channel's durable outbox.
public enum ChannelRecord: Sendable, Equatable {
    case email(MailMessage)
    case voicemail(VoicemailCapture)
    case calendar(CalendarEventCapture)
    case contact(ContactCapture)

    public var channel: RelayChannel {
        switch self {
        case .email: return .email
        case .voicemail: return .voicemail
        case .calendar: return .calendar
        case .contact: return .contacts
        }
    }

    /// Stable, non-empty id used for local dedupe (`seen` index) and backend
    /// idempotency.
    public var stableID: String {
        switch self {
        case .email(let m): return m.messageID
        case .voicemail(let v): return v.voicemailID
        case .calendar(let e): return e.eventID
        case .contact(let c): return c.personID
        }
    }
}

/// One capture plane. Each implementation reads its *local* source
/// (mailbox, voice-mail box, calendar, address book) read-only and yields
/// normalized records. The engine never touches a source directly — tests
/// inject fakes.
public protocol ChannelCaptureSource: Sendable {
    /// Records not yet delivered (dedupe is the engine's job via the local
    /// `seen` index, so a source may re-yield a record safely).
    func fetch() async throws -> [ChannelRecord]
}

// =============================================================================
// The mail-connector plane (§46/§47): the mailbox the relay reads from.
// NerveCenter never holds the mailbox socket or the credential — the relay
// is the only long-lived holder. The registry record (host/port/account/
// auth method) is non-secret configuration; the credential itself comes
// from `NERVE_RELAY_MAIL_TOKEN` (env) or a keychain item at poll time.
// =============================================================================

public enum MailCaptureError: Error, Equatable {
    /// No mailbox credential available — capture fails closed rather than
    /// guessing (§11 Strategy A).
    case noMailCredential
}

/// The `MailConnectorPlane` — email capture over the mail-connector registry.
///
/// Production flow (poll → normalize → outbox → backend):
/// 1. Read the connector record from the local config (non-secret).
/// 2. Resolve the mailbox credential from the environment
///    (`NERVE_RELAY_MAIL_TOKEN`) or keychain — never from the config file.
/// 3. Poll the mailbox read-only (IMAP), newest first, and yield
///    normalized `MailMessage` records.
///
/// The default `fetch()` implements step (2)'s gate and fails closed; the
/// live IMAP poll is host-side and injected in tests as a fake source.
public struct MailConnectorPlane: ChannelCaptureSource {
    public struct Config: Sendable, Equatable {
        public var connectorID: String
        public var account: String
        public var mailbox: String
        public var host: String
        public var port: Int
        public var ssl: Bool
        public var authMethod: String
        public init(
            connectorID: String = "", account: String = "", mailbox: String = "",
            host: String = "", port: Int = 993, ssl: Bool = true,
            authMethod: String = "oauth2"
        ) {
            self.connectorID = connectorID
            self.account = account
            self.mailbox = mailbox
            self.host = host
            self.port = port
            self.ssl = ssl
            self.authMethod = authMethod
        }
    }

    public let config: Config
    public init(config: Config) { self.config = config }

    /// The env-only mailbox credential (never persisted). A blank value
    /// counts as absent — fail closed, don't poll an empty token.
    public static func mailToken() -> String? {
        let v = ProcessInfo.processInfo.environment[NerveRelayMailTokenEnvVar]
            .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
        guard let v, !v.isEmpty else { return nil }
        return v
    }

    public func fetch() async throws -> [ChannelRecord] {
        // Fail closed: without the env-only mailbox token the plane cannot
        // poll the mailbox; returning empty here would silently lose mail.
        if Self.mailToken() == nil {
            throw MailCaptureError.noMailCredential
        }
        // The live IMAP poll is host-side (credential-bound); tests replace
        // this source with a fake. No mailbox access happens from this
        // default without a real transport implementation.
        return []
    }
}

// =============================================================================
// Mail-connector registry client (§46): the relay discovers the connectors
// NerveCenter has registered (where its mailboxes live, whether they answer)
// over the same transport seam as ingest. Read-only from the relay's point of
// view — registration/verify are operator actions.
// =============================================================================

/// A registered mail connector as listed by `GET /api/mail-connectors`.
/// Non-secret registry fields only — `token_ref` is an opaque pointer to
/// where the credential is kept, never the credential itself.
public struct MailConnectorRecord: Sendable, Equatable {
    public let connectorID: String
    public let account: String
    public let mailbox: String
    public let host: String
    public let port: Int
    public let ssl: Bool
    public let authMethod: String
    public let status: String
    public let tokenRef: String?
    public let lastCheckedAt: String?

    public init(
        connectorID: String, account: String, mailbox: String, host: String,
        port: Int = 993, ssl: Bool = true, authMethod: String = "oauth2",
        status: String = "pending", tokenRef: String? = nil, lastCheckedAt: String? = nil
    ) {
        self.connectorID = connectorID
        self.account = account
        self.mailbox = mailbox
        self.host = host
        self.port = port
        self.ssl = ssl
        self.authMethod = authMethod
        self.status = status
        self.tokenRef = tokenRef
        self.lastCheckedAt = lastCheckedAt
    }
}

public enum MailConnectorRegistryError: Error, Equatable {
    case http(status: Int, body: String)
    case network(String)
}

/// Read client for the mail-connector registry plane.
public struct MailConnectorRegistryClient: Sendable {
    public let transport: RelayTransport
    public let baseURL: URL
    public let token: String

    public init(transport: RelayTransport, baseURL: URL, token: String) {
        self.transport = transport
        self.baseURL = baseURL
        self.token = token
    }

    private func request() -> URLRequest {
        var url = baseURL
        for part in ["api", "mail-connectors"] { url.appendPathComponent(part) }
        var req = URLRequest(url: url)
        req.httpMethod = "GET"
        req.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        req.timeoutInterval = 15
        return req
    }

    /// `GET /api/mail-connectors` → registered connectors. An unconfigured
    /// system returns an empty list, never a fabricated connector. Transport
    /// failures throw `network`; a non-2xx answer throws `http`.
    public func list() async throws -> [MailConnectorRecord] {
        let result: RelayHTTPResult
        do {
            result = try await transport.send(request())
        } catch {
            throw MailConnectorRegistryError.network(String(describing: error))
        }
        guard (200...299).contains(result.status) else {
            throw MailConnectorRegistryError.http(
                status: result.status,
                body: String(data: result.body, encoding: .utf8) ?? ""
            )
        }
        // ListResponse: {"data": [...], "meta": {...}} — unconfigured is [].
        guard let obj = (try? JSONSerialization.jsonObject(with: result.body)) as? [String: Any],
              let items = obj["data"] as? [[String: Any]] else {
            return []
        }
        var out: [MailConnectorRecord] = []
        for item in items {
            guard let id = item["connector_id"] as? String, !id.isEmpty else { continue }
            out.append(MailConnectorRecord(
                connectorID: id,
                account: (item["account"] as? String) ?? "",
                mailbox: (item["mailbox"] as? String) ?? "",
                host: (item["host"] as? String) ?? "",
                port: (item["port"] as? Int) ?? 993,
                ssl: (item["ssl"] as? Bool) ?? true,
                authMethod: (item["auth_method"] as? String) ?? "oauth2",
                status: (item["status"] as? String) ?? "pending",
                tokenRef: item["token_ref"] as? String,
                lastCheckedAt: item["last_checked_at"] as? String
            ))
        }
        return out
    }
}
