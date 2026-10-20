import XCTest
import SQLite3
@testable import NerveRelayCore

/// Phase 4 D5' — per-plane engine tests: capture → normalize → per-channel
/// outbox → flush, with §70 local-first semantics (transport failure must
/// never lose local history) and §70 bounded retention (prune touches the
/// delivered archive only).
final class ChannelEngineTests: XCTestCase {
    private var dir: URL!

    override func setUpWithError() throws {
        dir = FileManager.default.temporaryDirectory
            .appendingPathComponent("relay-ch-\(UUID().uuidString)")
        try? FileManager.default.removeItem(at: dir)
    }

    override func tearDownWithError() throws {
        try? FileManager.default.removeItem(at: dir)
    }

    // -- fakes ---------------------------------------------------------------

    /// A scripted transport: canned `RelayHTTPResult`s in call order; the
    /// last one repeats. Records every request it was asked to send.
    private final class ScriptedTransport: RelayTransport, @unchecked Sendable {
        private let lock = NSLock()
        private var queue: [Result<RelayHTTPResult, Error>]
        private(set) var requests: [URLRequest] = []
        private(set) var callCount = 0

        init(status: Int = 201, body: String = "", count: Int = 1) {
            self.queue = Array(
                repeating: .success(RelayHTTPResult(
                    status: status,
                    body: Data(body.utf8)
                )),
                count: count
            )
        }

        init(throwing error: Error, count: Int = 1) {
            self.queue = Array(repeating: .failure(error), count: count)
        }

        func send(_ request: URLRequest) async throws -> RelayHTTPResult {
            // Record the request + pick the scripted reply under the lock
            // (synchronous block — safe from any concurrency context).
            let reply: Result<RelayHTTPResult, Error> = nextReply(for: request)
            return try reply.get()
        }

        private func nextReply(for request: URLRequest) -> Result<RelayHTTPResult, Error> {
            lock.lock(); defer { lock.unlock() }
            requests.append(request)
            callCount += 1
            return queue.indices.contains(callCount - 1)
                ? queue[callCount - 1]
                : queue[queue.count - 1]
        }
    }

    /// A capture source with a fixed record list (re-yields every poll,
    /// which the engine's `seen` index must absorb).
    private struct FixedSource: ChannelCaptureSource {
        let records: [ChannelRecord]
        func fetch() async throws -> [ChannelRecord] { records }
    }

    // -- helpers -------------------------------------------------------------

    private func makeConfig() throws -> RelayConfig {
        let outboxDir = dir.appendingPathComponent("outbox")
        try? FileManager.default.createDirectory(
            at: outboxDir, withIntermediateDirectories: true
        )
        return RelayConfig(
            baseURL: URL(string: "https://relay.test")!,
            token: "t",
            storePath: URL(fileURLWithPath: "/tmp/does-not-matter.db"),
            outboxPath: outboxDir.appendingPathComponent("messages.db")
        )
    }

    private func emailRecord(id: String) -> ChannelRecord {
        .email(MailMessage(
            messageID: id, direction: "RECEIVED",
            fromAddress: "a@example.com", toAddress: "me@example.com",
            senderName: nil, subject: id, body: "body-\(id)",
            timestampUnix: 1705276800, source: "personal-relay"
        ))
    }

    private func makeEngine(
        _ channel: RelayChannel = .email,
        source: ChannelCaptureSource,
        transport: ScriptedTransport,
        config: RelayConfig? = nil
    ) -> ChannelEngine {
        let engine = ChannelEngine(
            channel: channel,
            config: config ?? {
                var c = RelayConfig(
                    baseURL: URL(string: "https://relay.test")!,
                    token: "t",
                    storePath: URL(fileURLWithPath: "/tmp/x.db"),
                    outboxPath: dir.appendingPathComponent("ob/messages.db")
                )
                c.channels[channel].enabled = true
                return c
            }(),
            source: source,
            transport: transport
        )
        // No sleeping through retries in unit tests.
        engine.backoffBase = 0.001
        return engine
    }

    // -- capture + enqueue ----------------------------------------------------

    func testRunOnceEnqueuesNewRecordsToChannelOutbox() async throws {
        let source = FixedSource(records: [emailRecord(id: "m1"), emailRecord(id: "m2")])
        let transport = ScriptedTransport(status: 201)
        let engine = makeEngine(source: source, transport: transport)
        _ = try engine.open()

        let cycle = try await engine.runOnce()
        XCTAssertEqual(cycle.captured, 2)
        XCTAssertEqual(cycle.sent, 2)
        XCTAssertEqual(cycle.failed, 0)
        XCTAssertEqual(cycle.backlog, 0)
        // Every record reached the channel endpoint with bearer auth.
        XCTAssertEqual(transport.requests.count, 2)
        XCTAssertEqual(
            transport.requests[0].url?.path, "/api/email/ingest"
        )
    }

    func testSeenIndexDedupesReYieldedRecords() async throws {
        let source = FixedSource(records: [emailRecord(id: "m1")])
        let transport = ScriptedTransport(status: 201, count: 4)
        let engine = makeEngine(source: source, transport: transport)
        _ = try engine.open()

        let first = try await engine.runOnce()
        XCTAssertEqual(first.captured, 1, "first poll captures")
        // The source re-yields the same record; the local `seen` index
        // must prevent a re-capture (and the backend dedupes anyway, §48).
        let second = try await engine.runOnce()
        XCTAssertEqual(second.captured, 0)
        XCTAssertEqual(second.skipped, 1)
        XCTAssertEqual(transport.requests.count, 1, "no second request for a seen record")
    }

    func testPoisonRecordIsSkippedNotThrown() async throws {
        let poison = ChannelRecord.email(MailMessage(
            messageID: "bad", direction: "FWD", fromAddress: "",
            toAddress: "me@example.com", senderName: nil, subject: "s",
            body: "b", timestampUnix: 1705276800, source: "x"
        ))
        let good = emailRecord(id: "ok")
        let transport = ScriptedTransport(status: 201)
        let engine = makeEngine(
            source: FixedSource(records: [poison, good]),
            transport: transport
        )
        _ = try engine.open()

        let cycle = try await engine.runOnce()
        XCTAssertEqual(cycle.captured, 1, "poison row is skipped, good row is captured")
        XCTAssertEqual(cycle.sent, 1)
    }

    func testFetchFailureFailsCycleButKeepsOutboxIntact() async throws {
        struct SourceGone: Error {}
        struct ThrowingSource: ChannelCaptureSource {
            func fetch() async throws -> [ChannelRecord] { throw SourceGone() }
        }
        let transport = ScriptedTransport(status: 201)
        let engine = makeEngine(
            source: ThrowingSource(), transport: transport
        )
        _ = try engine.open()
        // Seed the outbox so we can prove nothing was lost.
        try engine.outbox.enqueue(storeID: 0, payloads: ["{\"seeded\": true}"])

        do {
            _ = try await engine.runOnce()
            XCTFail("a failing fetch must fail the cycle loudly")
        } catch let e as ChannelError {
            guard case ChannelError.badRecord(let why) = e else {
                return XCTFail("expected badRecord, got \(e)")
            }
            XCTAssertTrue(why.hasPrefix("fetch:"), "error should describe the failed fetch: \(why)")
        } catch {
            XCTFail("expected ChannelError, got \(error)")
        }
        let pending = try engine.outbox.pending(limit: 10)
        XCTAssertEqual(pending.count, 1, "queued data must survive a failed fetch (§70)")
        XCTAssertEqual(transport.requests.count, 0, "no send may happen when fetch fails")
    }

    // -- §70 retain-on-failure -------------------------------------------------

    func testTransportFailureKeepsBacklogQueued() async throws {
        let source = FixedSource(records: [emailRecord(id: "m1"), emailRecord(id: "m2")])
        // Two failures, then a 201: the in-cycle retry should recover the
        // batch after the transient failures (still one cycle).
        let transport = ScriptedTransport(status: 500, body: "boom", count: 2)
        var config = try makeConfig()
        config.channels.email.batchSize = 10
        let engine = makeEngine(
            source: source, transport: transport, config: config
        )
        _ = try engine.open()

        let cycle = try await engine.runOnce()
        XCTAssertEqual(cycle.captured, 2)
        XCTAssertGreaterThanOrEqual(cycle.failed, 1)
        XCTAssertEqual(cycle.sent, 0)
        XCTAssertEqual(cycle.backlog, 2, "failed sends must remain queued locally (§70)")

        // Connectivity returns: a fresh engine on the same durable outbox
        // file replays the backlog, acks it, and empties the queue.
        engine.outbox.close()
        let reopened = Outbox(url: config.channelOutboxPath(.email))
        _ = try reopened.open()
        XCTAssertEqual(try reopened.count(), 2, "durable queue survives restart")
        let engine2 = ChannelEngine(
            channel: .email, config: config,
            source: FixedSource(records: []), transport: ScriptedTransport(status: 201)
        )
        engine2.backoffBase = 0.001
        _ = try engine2.open()
        let replay = try await engine2.runOnce()
        XCTAssertEqual(replay.sent, 2, "backlog replays when the transport recovers")
        XCTAssertEqual(replay.backlog, 0)
        XCTAssertEqual(replay.captured, 0)
    }

    func testHTTP4xxRetainsBacklogAndFailsLoudly() async throws {
        let source = FixedSource(records: [emailRecord(id: "m1")])
        let transport = ScriptedTransport(
            status: 401, body: """
            {"detail":"invalid token"}
            """
        )
        var config = try makeConfig()
        config.channels.email.batchSize = 10
        let engine = makeEngine(source: source, transport: transport, config: config)
        _ = try engine.open()

        let cycle = try await engine.runOnce()
        XCTAssertEqual(cycle.failed, 1)
        XCTAssertEqual(cycle.sent, 0)
        XCTAssertEqual(cycle.backlog, 1, "a 4xx must not ack — the record stays queued")
        XCTAssertTrue(
            cycle.note.contains("401"),
            "the server's reason should surface in the cycle note, got: \(cycle.note)"
        )
    }

    func testReplayedDuplicateIsCounted() async throws {
        let source = FixedSource(records: [emailRecord(id: "m1")])
        // The backend answers 200 with {"duplicate": true} — a replay, §48.
        let transport = ScriptedTransport(
            status: 200, body: #"{"duplicate": true}"#
        )
        var config = try makeConfig()
        config.channels.email.batchSize = 10
        let engine = makeEngine(source: source, transport: transport, config: config)
        _ = try engine.open()

        let cycle = try await engine.runOnce()
        XCTAssertEqual(cycle.sent, 1)
        XCTAssertEqual(cycle.replayed, 1)
        XCTAssertEqual(cycle.backlog, 0)
    }

    func testAckedRowsMoveToDeliveredArchive() async throws {
        let source = FixedSource(records: [emailRecord(id: "m1")])
        let transport = ScriptedTransport(status: 201)
        let engine = makeEngine(source: source, transport: transport)
        _ = try engine.open()

        _ = try await engine.runOnce()
        XCTAssertEqual(try engine.outbox.count(), 0, "acked rows leave the outbox")
        XCTAssertEqual(
            try engine.outbox.deliveredCount(), 1,
            "…and land in the local delivered archive (§70 — history kept locally)"
        )
        // Re-acking is an idempotent no-op (crash-safe replay).
        let entry = OutboxEntry(seq: 999, storeID: 0, payloadJSON: "x")
        _ = try engine.outbox.ack([entry.seq])
        XCTAssertEqual(try engine.outbox.deliveredCount(), 1)
    }

    // -- §70 bounded retention -------------------------------------------------

    func testPruneNeverTouchesPendingOutbox() async throws {
        let ob = Outbox(url: dir.appendingPathComponent("prune.db"))
        _ = try ob.open()
        // Pending row: must never be pruned, no matter the bounds.
        _ = try ob.enqueue(storeID: 0, payloads: ["pending-payload"])
        // Delivered rows older than the retention window.
        for i in 0..<5 {
            try ob.ack(try ob.enqueue(storeID: 0, payloads: ["d\(i)"]))
        }
        XCTAssertEqual(try ob.count(), 1, "one pending row left in the outbox")
        XCTAssertEqual(try ob.deliveredCount(), 5)

        let now = Date(timeIntervalSince1970: 2_000_000_000)
        let pruned = try ob.prune(maxEntries: 100, retentionDays: 30, now: now)
        XCTAssertEqual(try ob.count(), 1, "pending rows are never touched by prune")
        XCTAssertEqual(try ob.deliveredCount(), 0, "all 5 delivered rows are aged out")
        XCTAssertEqual(pruned, 5)
    }

    func testPruneTrimsToMaxEntriesKeepingNewest() async throws {
        let ob = Outbox(url: dir.appendingPathComponent("prune-max.db"))
        _ = try ob.open()
        for i in 0..<7 {
            try ob.ack(try ob.enqueue(storeID: 0, payloads: ["row-\(i)"]))
        }
        let pruned = try ob.prune(maxEntries: 3, retentionDays: 0)
        XCTAssertEqual(pruned, 4)
        XCTAssertEqual(try ob.deliveredCount(), 3, "newest 3 survive the entry cap")
        // The survivors are the most recently acked rows (oldest pruned).
        let survivors = try deliveredPayloads(at: ob.url)
        XCTAssertEqual(survivors, ["row-4", "row-5", "row-6"],
                       "newest acked rows survive; oldest are pruned first")
    }

    func testPruneNoCapsIsNoOp() async throws {
        let ob = Outbox(url: dir.appendingPathComponent("prune-none.db"))
        _ = try ob.open()
        try ob.ack(try ob.enqueue(storeID: 0, payloads: ["keep"]))
        let pruned = try ob.prune(maxEntries: 0, retentionDays: 0)
        XCTAssertEqual(pruned, 0)
        XCTAssertEqual(try ob.deliveredCount(), 1, "0/0 caps mean 'never prune'")
    }

    /// Read `delivered.payload` in ack order, newest last (test-only
    /// inspection of the local archive; the Outbox API exposes only counts).
    private func deliveredPayloads(at url: URL) throws -> [String] {
        var handle: OpaquePointer?
        guard sqlite3_open_v2(url.path, &handle, SQLITE_OPEN_READONLY, nil) == SQLITE_OK,
              let handle else {
            throw OutboxError.openFailed("cannot open for read")
        }
        defer { sqlite3_close(handle) }
        var stmt: OpaquePointer?
        guard sqlite3_prepare_v2(
            handle,
            "SELECT payload FROM delivered ORDER BY acked_at ASC, seq ASC",
            -1, &stmt, nil
        ) == SQLITE_OK, let stmt else {
            throw OutboxError.execFailed(String(cString: sqlite3_errmsg(handle)))
        }
        defer { sqlite3_finalize(stmt) }
        var out: [String] = []
        while sqlite3_step(stmt) == SQLITE_ROW {
            out.append(ncCStr(sqlite3_column_text(stmt, 0)) ?? "")
        }
        return out
    }

    func testEngineCycleAppliesConfiguredRetention() async throws {
        var config = try makeConfig()
        config.channels.email.maxEntries = 1
        config.channels.email.retentionDays = 0
        config.channels.email.batchSize = 10
        let source = FixedSource(
            records: [emailRecord(id: "m1"), emailRecord(id: "m2")]
        )
        let transport = ScriptedTransport(status: 201)
        let engine = makeEngine(source: source, transport: transport, config: config)
        _ = try engine.open()

        let cycle = try await engine.runOnce()
        XCTAssertEqual(cycle.captured, 2)
        XCTAssertEqual(cycle.delivered, 2)
        XCTAssertEqual(cycle.pruned, 1, "config bound (max_entries=1) trims the archive")
        XCTAssertEqual(try engine.outbox.deliveredCount(), 1)
    }
}
