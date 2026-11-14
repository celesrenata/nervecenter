import Foundation

// -- transport (Core-local so the relay is unit-testable without NerveAPI) --

/// One HTTP result: status + raw body.
public struct RelayHTTPResult: Sendable, Equatable {
    public let status: Int
    public let body: Data
    public init(status: Int, body: Data) {
        self.status = status
        self.body = body
    }
}

public enum RelayTransportError: Error, Equatable {
    /// The network layer failed (offline, DNS, TLS) — transient, retry.
    case network(String)
    /// The server answered but not with success (4xx/5xx) — the body is
    /// carried in the error so the CLI can show the server's reason.
    case http(status: Int, body: String)
}

/// Transport seam for the relay. The production implementation is
/// `URLSessionRelayTransport`; tests inject a scripted mock.
public protocol RelayTransport: Sendable {
    func send(_ request: URLRequest) async throws -> RelayHTTPResult
}

/// TLS to the configured base URL (§47 "encrypt transport"). The
/// Authorization header is a bearer token (§47 "authenticate to
/// NerveCenter") — no credentials in URLs, no query strings.
public struct URLSessionRelayTransport: RelayTransport {
    public init() {}

    public func send(_ request: URLRequest) async throws -> RelayHTTPResult {
        do {
            let (data, response) = try await URLSession.shared.data(for: request)
            let status = (response as? HTTPURLResponse)?.statusCode ?? 0
            if status == 0 {
                throw RelayTransportError.network("no HTTP status in response")
            }
            return RelayHTTPResult(status: status, body: data)
        } catch let e as RelayTransportError {
            throw e
        } catch {
            throw RelayTransportError.network(error.localizedDescription)
        }
    }
}

// -- cycle result ---------------------------------------------------------------

/// One iteration result, surfaced to the CLI for `run --once` and
/// `status` (and for the LaunchAgent's `StandardOutPath` log).
public struct RelayCycle: Sendable, Equatable {
    public var captured: Int = 0
    public var skipped: Int = 0
    public var sent: Int = 0
    public var replayed: Int = 0
    public var failed: Int = 0
    public var backlog: Int = 0
    public var maxStoreID: Int64 = 0
    public var note: String = ""

    public init() {}
}

public enum RelayError: Error {
    case config(RelayConfig.ConfigError)
    case store(StoreError)
    case outbox(OutboxError)
    /// The relay is not allowed to parse this schema — fail closed (§11).
    case schemaRefused
}

// -- engine ----------------------------------------------------------------------

/// The relay loop: tail the Messages store (read-only) → normalize
/// (fail closed on anything unparsable) → enqueue to the durable
/// outbox → flush the outbox to NerveCenter in `seq` order.
///
/// prompt2 §46/§47/§48 and §11 Strategy A. Each `runOnce()` is one
/// idempotent cycle; `run` just repeats it on an interval.
public final class RelayEngine {
    public let config: RelayConfig
    public let store: MessageStore
    public let outbox: Outbox
    public let transport: RelayTransport

    private let encoder: JSONEncoder

    public init(config: RelayConfig, transport: RelayTransport? = nil) {
        self.config = config
        self.store = MessageStore(url: config.storePath)
        self.outbox = Outbox(url: config.outboxPath)
        self.transport = transport ?? URLSessionRelayTransport()
        self.encoder = JSONEncoder()
        self.encoder.outputFormatting = [.sortedKeys]
    }

    /// Open store (schema-guarded) + outbox. Throws on any refusal —
    /// in particular a missing/renamed column stops the relay cold.
    public func open() throws -> (schemaVersion: String, backlog: Int) {
        let opened = try store.open()
        switch opened {
        case .ok(let schemaVersion):
            let backlog = try outbox.open()
            return (schemaVersion, backlog)
        case .notFound:
            // open() already threw for a missing file; this case is
            // defensive only.
            throw StoreError.openFailed("store missing")
        }
    }

    /// One full cycle: capture new rows, then flush what we can.
    ///
    /// A send failure leaves the outbox intact — nothing is dropped;
    /// the next cycle (or reconnect) replays it (§48).
    public func runOnce() async throws -> RelayCycle {
        var cycle = RelayCycle()

        // 1) Capture: tail rows past the capture cursor (see highWaterMark).
        let cursor = try highWaterMark()
        let scan = try store.scan(afterCursor: cursor, limit: config.batchSize)
        let rows = scan.rows
        cycle.skipped += scan.unreadable
        var payloads: [String] = []
        for raw in rows {
            do {
                let norm = try MessageNormalizer().normalize(raw)
                payloads.append(Self.ingestJSON(norm, encoder: encoder))
            } catch let e as NormalizationError {
                cycle.skipped += 1
                cycle.note = "skipped \(String(raw.guid.prefix(12)))…: \(String(describing: e))"
            }
        }
        if !payloads.isEmpty {
            // Attribute the batch to the highest row id so the cursor
            // never re-captures a partially-sent batch.
            try outbox.enqueue(storeID: max(rows.map(\.id).max() ?? 0, scan.scannedMaxID), payloads: payloads)
        } else if scan.scannedMaxID > cursor {
            // Everything scanned was unreadable or skipped: still move past
            // it, or the same all-skipped batch would be re-read forever and
            // newer messages would never be reached.
            try outbox.setCursor(scan.scannedMaxID)
        }
        cycle.captured = payloads.count
        cycle.maxStoreID = try store.maxID()

        // 2) Flush: pending entries oldest-first; ack accepted ones.
        let backlog = try outbox.count()
        if backlog > 0 {
            await flush(&cycle)
        }
        cycle.backlog = try outbox.count()
        return cycle
    }

    /// Send the pending batch; retry briefly on transient failures,
    /// then stop for this cycle (the remainder stays queued and
    /// replays next cycle / on reconnect — §48).
    private func flush(_ cycle: inout RelayCycle) async {
        for attempt in 0..<3 {
            let batch = (try? outbox.pending(limit: config.batchSize)) ?? []
            if batch.isEmpty { return }

            var ackedSeqs: [Int64] = []
            var transient = false
            for entry in batch {
                let request = Self.makeRequest(entry: entry, config: config)
                do {
                    let result = try await transport.send(request)
                    if (200...299).contains(result.status) {
                        ackedSeqs.append(entry.seq)
                        cycle.sent += 1
                        if Self.isDuplicate(result.body) { cycle.replayed += 1 }
                    } else {
                        // 4xx/5xx: do NOT ack. A 4xx usually means the
                        // token is bad — keep failing loudly, but do not
                        // spin: one retry per cycle is enough.
                        cycle.failed += 1
                        let detail = (String(data: result.body, encoding: .utf8) ?? "")
                            .trimmingCharacters(in: .whitespacesAndNewlines)
                        cycle.note = "HTTP \(result.status): \(detail)"
                        return
                    }
                } catch {
                    // Network/TLS failure — transient; the whole batch
                    // stays queued for the next cycle.
                    cycle.failed += 1
                    cycle.note = "transport: \(String(describing: error))"
                    transient = true
                    break
                }
            }

            if !ackedSeqs.isEmpty {
                _ = (try? outbox.ack(ackedSeqs))
            }
            if !transient { return }
            try? await Task.sleep(nanoseconds: 1_000_000_000 * UInt64(attempt + 1))
        }
        // Out of attempts for this cycle — remainder stays queued.
    }

    // -- wire helpers ------------------------------------------------------------

    /// The request for one outbox entry.
    ///
    /// Relay-device mode (`config.relayDeviceID` set): `POST {base}/api/relay/messages`
    /// with the device token and a one-message batch. Otherwise the legacy
    /// `POST {base}/api/messages/ingest`.
    static func makeRequest(entry: OutboxEntry, config: RelayConfig) -> URLRequest {
        var url = config.baseURL
        url.appendPathComponent("api")
        var body = entry.payloadJSON.data(using: .utf8)
        if let device = config.relayDeviceID {
            url.appendPathComponent("relay")
            url.appendPathComponent("messages")
            body = relayBatchBody(payloadJSON: entry.payloadJSON, deviceID: device) ?? body
        } else {
            url.appendPathComponent("messages")
            url.appendPathComponent("ingest")
        }
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue("Bearer \(config.token)", forHTTPHeaderField: "Authorization")
        request.httpBody = body
        request.timeoutInterval = 15
        return request
    }

    /// Convert one legacy ingest payload into the relay endpoint's batch shape
    /// (`timestamp` becomes `received_at`). `nil` when the payload is not the
    /// expected JSON object, in which case the caller sends it unchanged and
    /// the server rejects it loudly.
    static func relayBatchBody(payloadJSON: String, deviceID: String) -> Data? {
        guard let raw = payloadJSON.data(using: .utf8),
              let obj = (try? JSONSerialization.jsonObject(with: raw)) as? [String: Any],
              let messageID = obj["message_id"] as? String
        else { return nil }
        let message: [String: Any] = [
            "message_id": messageID,
            "channel": obj["channel"] as? String ?? "imessage",
            // The relay endpoint stores the value as given and imports use lowercase.
            "direction": (obj["direction"] as? String ?? "inbound").lowercased(),
            "peer_identifier": obj["peer_identifier"] as? String ?? "",
            "peer_name": (obj["peer_name"] as? String).map { $0 as Any } ?? NSNull(),
            "body": obj["body"] as? String ?? "",
            "received_at": obj["timestamp"] as? String ?? "",
            "source": obj["source"] as? String ?? "personal-relay",
        ]
        return try? JSONSerialization.data(withJSONObject: ["device_id": deviceID, "messages": [message]])
    }

    /// Encode a normalized message into the backend `IngestRequest`
    /// JSON (snake_case fields, matching the Pydantic model exactly).
    static func ingestJSON(_ m: NormalizedMessage, encoder: JSONEncoder) -> String {
        struct Payload: Encodable {
            let message_id: String
            let direction: String
            let peer_identifier: String
            let peer_name: String?
            let body: String
            let channel: String
            let timestamp: String
            let source: String
        }
        let p = Payload(
            message_id: m.messageID,
            direction: m.direction,
            peer_identifier: m.peerIdentifier,
            peer_name: m.peerName,
            body: m.body,
            channel: m.channel,
            timestamp: m.timestampISO8601,
            source: m.source
        )
        return String(data: try! encoder.encode(p), encoding: .utf8) ?? "{}"
    }

    /// Whether the server already had this message (a replay, §48).
    /// Legacy ingest answers `{"duplicate": true}`; the relay endpoint answers
    /// `{"ok": true, "stored": 0, "skipped": 1}`.
    static func isDuplicate(_ body: Data) -> Bool {
        guard let obj = (try? JSONSerialization.jsonObject(with: body)) as? [String: Any] else {
            return false
        }
        if obj["duplicate"] as? Bool == true { return true }
        if let stored = obj["stored"] as? Int, let skipped = obj["skipped"] as? Int {
            return stored == 0 && skipped > 0
        }
        return false
    }

    /// Continue exactly where an import ended: adopt `cursor` as the capture
    /// cursor when none is set yet. Without this the first `run` seeds at the
    /// newest message and silently skips whatever arrived between the import
    /// and the first run. Never moves a cursor backwards.
    @discardableResult
    public func adoptCursorIfUnset(_ cursor: Int64) throws -> Bool {
        guard cursor > 0, try outbox.cursor() == 0 else { return false }
        try outbox.setCursor(cursor)
        return true
    }

    /// Capture cursor: the persisted outbox cursor — the highest chat.db
    /// `id` already captured. On the very first run it is seeded at the
    /// store's current max so we do not replay the entire message history
    /// (MVP: only *new* messages, §13/§48). Persisting it (rather than
    /// deriving it from `MAX(outbox.store_id)`) means rows that arrive
    /// while the outbox is momentarily empty are captured next cycle,
    /// not skipped.
    private func highWaterMark() throws -> Int64 {
        let cursor = try outbox.cursor()
        if cursor > 0 { return cursor }
        let seed = try store.maxID()
        try outbox.setCursor(seed)
        return seed
    }
}
