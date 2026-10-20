import Foundation

// =============================================================================
// Phase 4 D5' — per-plane channel engine (T003-10/11, G-2.4).
//
// One `ChannelEngine` per capture plane (email, voicemail, calendar,
// contacts). Same normalized-record → durable outbox → NerveCenter shape as
// the Messages `RelayEngine`, with:
//
//   - its own outbox file (`config.channelOutboxPath(channel)`) so a backlog
//     on one plane cannot starve or corrupt another;
//   - a local `seen` idempotency index so a source that re-yields a record
//     between polls is not re-captured (the backend also dedupes by id);
//   - §70 local-first retention: on transport failure records stay queued
//     and replay in order; acked records move to the local `delivered`
//     archive (never dropped); `prune` trims only that archive, bounded by
//     the per-channel config (`max_entries` / `retention_days`).
//
// Backoff mirrors the Messages engine: up to 3 attempts per cycle with a
// linear sleep between attempts; the remainder stays queued for the next
// cycle (§48).
// =============================================================================

/// One channel-cycle result (surfaced to the CLI `run`/`status`).
public struct ChannelCycle: Sendable, Equatable {
    public var captured: Int = 0
    public var skipped: Int = 0
    public var sent: Int = 0
    public var replayed: Int = 0
    public var failed: Int = 0
    public var backlog: Int = 0
    public var delivered: Int = 0
    public var pruned: Int = 0
    public var note: String = ""
    public init() {}
}

public enum ChannelError: Error, Equatable {
    /// A record failed the fail-closed normalization rules.
    case badRecord(String)
}

/// Fail-closed normalization errors per plane.
public enum ChannelNormalizationError: Error, Equatable {
    case emptyID
    case emptyPeer
    case badDirection(String)
}

/// The per-plane engine: tail the local source → normalize (fail closed) →
/// enqueue to this channel's durable outbox → flush to NerveCenter in `seq`
/// order → prune the delivered archive to the configured bounds.
public final class ChannelEngine {
    public let channel: RelayChannel
    public let config: RelayConfig
    public let outbox: Outbox
    public let transport: RelayTransport
    public let source: ChannelCaptureSource

    private let encoder: JSONEncoder
    /// Base for the in-cycle linear retry backoff (seconds, attempt n
    /// sleeps n × this). Production: 1s. Tests inject ~0 to stay fast.
    public var backoffBase: TimeInterval = 1

    public init(
        channel: RelayChannel,
        config: RelayConfig,
        source: ChannelCaptureSource,
        transport: RelayTransport? = nil
    ) {
        self.channel = channel
        self.config = config
        self.outbox = Outbox(url: config.channelOutboxPath(channel))
        self.transport = transport ?? URLSessionRelayTransport()
        self.source = source
        self.encoder = JSONEncoder()
        self.encoder.outputFormatting = [.sortedKeys]
    }

    /// Open this channel's outbox (schema-guarded). Returns the pending count.
    public func open() throws -> Int {
        try outbox.open()
    }

    /// One full channel cycle: capture new records, then flush what we can,
    /// then enforce the local retention bounds (§70).
    public func runOnce() async throws -> ChannelCycle {
        var cycle = ChannelCycle()

        // 1) Capture: fetch from the local source (read-only).
        let records: [ChannelRecord]
        do {
            records = try await source.fetch()
        } catch {
            // The local source is unavailable (permission revoked, no
            // mailbox credential, …). Fail the cycle *loudly* but never
            // touch the outbox — queued records are safe (§70).
            throw ChannelError.badRecord("fetch: \(String(describing: error))")
        }

        // 2) Normalize (fail closed) + local dedupe, then enqueue.
        var payloads: [(id: String, json: String)] = []
        for record in records {
            do {
                let json = try Self.payloadJSON(for: record, encoder: encoder)
                if outbox.seen(record.stableID) {
                    cycle.skipped += 1  // already queued or archived
                    continue
                }
                payloads.append((record.stableID, json))
            } catch let e as ChannelNormalizationError {
                cycle.skipped += 1
                cycle.note = "skipped: \(String(describing: e))"
            }
        }
        if !payloads.isEmpty {
            try outbox.enqueue(
                storeID: 0,
                payloads: payloads.map(\.json)
            )
            for id in payloads.map(\.id) {
                try outbox.markSeen(id)
            }
        }
        cycle.captured = payloads.count

        // 3) Flush pending oldest-first; ack accepted ones. A send failure
        //    leaves the queue intact (no local data loss — §70).
        let backlog = try outbox.count()
        if backlog > 0 {
            await flush(&cycle)
        }
        cycle.backlog = try outbox.count()

        // Delivered before the retention trim below: this cycle's acked
        // rows, not the post-prune archive size (the live size is read
        // straight from the outbox by the CLI).
        cycle.delivered = try outbox.deliveredCount()

        // 4) §70 bounded local retention — delivered archive only.
        let cfg = config.channels[channel]
        cycle.pruned = try outbox.prune(
            maxEntries: cfg.maxEntries,
            retentionDays: cfg.retentionDays
        )
        return cycle
    }

    /// Send the pending batch; retry briefly on transient failures, then
    /// stop for this cycle (remainder stays queued — §48).
    private func flush(_ cycle: inout ChannelCycle) async {
        for attempt in 0..<3 {
            let batch = (try? outbox.pending(limit: config.channels[channel].batchSize)) ?? []
            if batch.isEmpty { return }

            var ackedSeqs: [Int64] = []
            var transient = false
            for entry in batch {
                let request = Self.makeRequest(channel: channel, config: config, entry: entry)
                do {
                    let result = try await transport.send(request)
                    if (200...299).contains(result.status) {
                        ackedSeqs.append(entry.seq)
                        cycle.sent += 1
                        if RelayEngine.isDuplicate(result.body) { cycle.replayed += 1 }
                    } else {
                        // 4xx/5xx: do NOT ack. A bad token keeps failing
                        // loudly without spinning: one retry per cycle.
                        cycle.failed += 1
                        let detail = (String(data: result.body, encoding: .utf8) ?? "")
                            .trimmingCharacters(in: .whitespacesAndNewlines)
                        cycle.note = "HTTP \(result.status): \(detail)"
                        return
                    }
                } catch {
                    // Network/TLS failure — transient; the whole batch stays
                    // queued for the next cycle.
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
            try? await Task.sleep(
                nanoseconds: UInt64(max(0, backoffBase) * Double(attempt + 1) * 1_000_000_000)
            )
        }
        // Out of attempts for this cycle — remainder stays queued.
    }

    // -- wire helpers ---------------------------------------------------------

    /// `POST {base}/api/<plane>/ingest` with bearer auth.
    static func makeRequest(
        channel: RelayChannel, config: RelayConfig, entry: OutboxEntry
    ) -> URLRequest {
        var url = config.baseURL
        for part in channel.endpoint { url.appendPathComponent(part) }
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue("Bearer \(config.token)", forHTTPHeaderField: "Authorization")
        request.httpBody = entry.payloadJSON.data(using: .utf8)
        request.timeoutInterval = 15
        return request
    }

    /// Encode a channel record into its plane's backend ingest request
    /// (snake_case fields matching the Pydantic models exactly). Fail closed
    /// on anything that the backend would reject as a 422 (empty ids/peers),
    /// so a poison row can never wedge the outbox.
    static func payloadJSON(for record: ChannelRecord, encoder: JSONEncoder) throws -> String {
        switch record {
        case .email(let m):
            let direction = m.direction.trimmingCharacters(in: .whitespacesAndNewlines)
                .uppercased()
            guard direction == "RECEIVED" || direction == "SENT" else {
                throw ChannelNormalizationError.badDirection(m.direction)
            }
            guard !m.messageID.isEmpty else { throw ChannelNormalizationError.emptyID }
            guard !m.fromAddress.isEmpty else { throw ChannelNormalizationError.emptyPeer }
            struct Payload: Encodable {
                let message_id: String
                let direction: String
                let from_address: String
                let to_address: String
                let sender_name: String?
                let subject: String
                let body: String
                let timestamp: String
                let source: String
            }
            let p = Payload(
                message_id: m.messageID,
                direction: direction,
                from_address: m.fromAddress,
                to_address: m.toAddress,
                sender_name: m.senderName,
                subject: m.subject,
                body: m.body,
                timestamp: ISO8601DateFormatter.iso8601Second
                    .string(from: Date(timeIntervalSince1970: m.timestampUnix)),
                source: m.source
            )
            return String(data: try encoder.encode(p), encoding: .utf8) ?? "{}"

        case .voicemail(let v):
            guard !v.voicemailID.isEmpty else { throw ChannelNormalizationError.emptyID }
            guard !v.callerPhone.isEmpty else { throw ChannelNormalizationError.emptyPeer }
            struct Payload: Encodable {
                let voicemail_id: String
                let caller_phone: String
                let caller_name: String?
                let transcript: String
                let duration_seconds: Int
                let received_at: String
                let source: String
            }
            let p = Payload(
                voicemail_id: v.voicemailID,
                caller_phone: v.callerPhone,
                caller_name: v.callerName,
                transcript: v.transcript,
                duration_seconds: v.durationSeconds,
                received_at: ISO8601DateFormatter.iso8601Second
                    .string(from: Date(timeIntervalSince1970: v.receivedAtUnix)),
                source: v.source
            )
            return String(data: try encoder.encode(p), encoding: .utf8) ?? "{}"

        case .calendar(let e):
            guard !e.eventID.isEmpty else { throw ChannelNormalizationError.emptyID }
            guard !e.title.isEmpty else { throw ChannelNormalizationError.emptyID }
            struct Payload: Encodable {
                let event_id: String
                let title: String
                let description: String?
                let start: String?
                let end: String?
                let all_day: Bool
                let location: String?
                let participants: [String]
                let reminder_at: String?
                let source: String
            }
            func iso(_ t: TimeInterval?) -> String? {
                guard let t else { return nil }
                return ISO8601DateFormatter.iso8601Second
                    .string(from: Date(timeIntervalSince1970: t))
            }
            let p = Payload(
                event_id: e.eventID,
                title: e.title,
                description: e.description,
                start: iso(e.startUnix),
                end: iso(e.endUnix),
                all_day: e.allDay,
                location: e.location,
                participants: e.participants,
                reminder_at: iso(e.reminderAtUnix),
                source: e.source
            )
            return String(data: try encoder.encode(p), encoding: .utf8) ?? "{}"

        case .contact(let c):
            guard !c.personID.isEmpty else { throw ChannelNormalizationError.emptyID }
            guard !c.displayName.isEmpty else { throw ChannelNormalizationError.emptyID }
            struct Payload: Encodable {
                let person_id: String
                let display_name: String
                let emails: [String]
                let phones: [String]
                let notes: String?
                let is_vip: Bool
                let source: String
                let timestamp: String?
            }
            let p = Payload(
                person_id: c.personID,
                display_name: c.displayName,
                emails: c.emails,
                phones: c.phones,
                notes: c.notes,
                is_vip: c.isVIP,
                source: c.source,
                timestamp: c.updatedAtUnix.map {
                    ISO8601DateFormatter.iso8601Second
                        .string(from: Date(timeIntervalSince1970: $0))
                }
            )
            return String(data: try encoder.encode(p), encoding: .utf8) ?? "{}"
        }
    }
}
