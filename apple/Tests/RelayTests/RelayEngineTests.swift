import XCTest
import SQLite3
@testable import NerveRelayCore

/// Records requests and replies with a canned status/body.
final class TestTransport: RelayTransport, @unchecked Sendable {
    let status: Int
    let responseBody: String
    private(set) var callCount = 0
    private(set) var authHeaders: [String] = []
    private(set) var lastURL: URL?

    init(status: Int, responseBody: String) {
        self.status = status
        self.responseBody = responseBody
    }

    func send(_ request: URLRequest) async throws -> RelayHTTPResult {
        callCount += 1
        lastURL = request.url
        if let auth = request.value(forHTTPHeaderField: "Authorization") {
            authHeaders.append(auth)
        }
        return RelayHTTPResult(
            status: status,
            body: Data(responseBody.utf8)
        )
    }
}

/// 2024-01-15T10:30:00Z in Darwin nanoseconds.
private func darwin(_ unixSeconds: TimeInterval) -> Int64 {
    Int64((unixSeconds - MessageStore.darwinEpochUnix) * 1_000_000_000)
}

/// Minimal chat.db-shaped fixture.
private func seedStore(at url: URL) throws {
    var db: OpaquePointer?
    guard sqlite3_open(url.path, &db) == SQLITE_OK, let db else {
        throw NSError(domain: "test", code: 1)
    }
    defer { sqlite3_close(db) }
    let ddl = """
        PRAGMA user_version = 11;
        CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT NOT NULL UNIQUE);
        CREATE TABLE chat_message (chat_id INTEGER, message_id INTEGER);
        CREATE TABLE message (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guid TEXT,
            handle_id INTEGER,
            item_type INTEGER DEFAULT 0,
            is_from_me INTEGER DEFAULT 0,
            service TEXT DEFAULT 'iMessage',
            date INTEGER,
            text TEXT
        );
    """
    var err: UnsafeMutablePointer<CChar>?
    guard sqlite3_exec(db, ddl, nil, nil, &err) == SQLITE_OK else {
        let msg = err.map { String(cString: $0) } ?? "ddl failed"
        if let err { sqlite3_free(err) }
        throw NSError(domain: "test", code: 2, userInfo: [NSLocalizedDescriptionKey: msg])
    }
}

/// Append one message row (creating its handle if needed).
private func addMessage(
    at url: URL,
    guid: String,
    peer: String,
    fromMe: Int32 = 0,
    service: String = "iMessage",
    text: String
) throws {
    var db: OpaquePointer?
    guard sqlite3_open(url.path, &db) == SQLITE_OK, let db else {
        throw NSError(domain: "test", code: 3)
    }
    defer { sqlite3_close(db) }

    var s1: OpaquePointer?
    let sql = "INSERT OR IGNORE INTO handle (id) VALUES (?)"
    guard sqlite3_prepare_v2(db, sql, -1, &s1, nil) == SQLITE_OK else {
        throw NSError(domain: "test", code: 4)
    }
    sqlite3_bind_text(s1, 1, peer, -1, unsafeBitCast(-1, to: sqlite3_destructor_type.self))
    _ = sqlite3_step(s1)
    sqlite3_finalize(s1)

    var s2: OpaquePointer?
    guard sqlite3_prepare_v2(db, "SELECT ROWID FROM handle WHERE id = ?", -1, &s2, nil) == SQLITE_OK else {
        throw NSError(domain: "test", code: 6)
    }
    sqlite3_bind_text(s2, 1, peer, -1, unsafeBitCast(-1, to: sqlite3_destructor_type.self))
    guard sqlite3_step(s2) == SQLITE_ROW else {
        sqlite3_finalize(s2)
        throw NSError(domain: "test", code: 7)
    }
    let handleID = sqlite3_column_int64(s2, 0)
    sqlite3_finalize(s2)

    let msgSQL = """
        INSERT INTO message (guid, handle_id, item_type, is_from_me, service, date, text)
        VALUES (?, ?, 0, ?, ?, ?, ?)
    """
    var s3: OpaquePointer?
    guard sqlite3_prepare_v2(db, msgSQL, -1, &s3, nil) == SQLITE_OK else {
        throw NSError(domain: "test", code: 8)
    }
    sqlite3_bind_text(s3, 1, guid, -1, unsafeBitCast(-1, to: sqlite3_destructor_type.self))
    sqlite3_bind_int64(s3, 2, handleID)
    sqlite3_bind_int(s3, 3, Int32(fromMe))
    sqlite3_bind_text(s3, 4, service, -1, unsafeBitCast(-1, to: sqlite3_destructor_type.self))
    sqlite3_bind_int64(s3, 5, darwin(1_705_314_600))
    sqlite3_bind_text(s3, 6, text, -1, unsafeBitCast(-1, to: sqlite3_destructor_type.self))
    guard sqlite3_step(s3) == SQLITE_DONE else {
        sqlite3_finalize(s3)
        throw NSError(domain: "test", code: 9)
    }
    sqlite3_finalize(s3)
}

final class RelayEngineTests: XCTestCase {
    private let peer = "+15550000001"
    private var dir: URL!
    private var storeURL: URL!
    private var outboxURL: URL!

    override func setUpWithError() throws {
        dir = FileManager.default.temporaryDirectory
            .appendingPathComponent("relay-engine-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        storeURL = dir.appendingPathComponent("chat.db")
        outboxURL = dir.appendingPathComponent("outbox.db")
        try seedStore(at: storeURL)
    }

    override func tearDownWithError() throws {
        try? FileManager.default.removeItem(at: dir)
    }

    private func makeEngine(transport: TestTransport) throws -> RelayEngine {
        let config = RelayConfig(
            baseURL: URL(string: "http://127.0.0.1:9")!,
            token: "tok-123",
            storePath: storeURL,
            outboxPath: outboxURL,
            batchSize: 200
        )
        return RelayEngine(config: config, transport: transport)
    }

    /// First run seeds the cursor at the store max id — it must NOT
    /// replay the whole message history.
    func testFirstRunSeedsCursorAndSkipsHistory() async throws {
        try addMessage(at: storeURL, guid: "G1", peer: peer, text: "old")
        try addMessage(at: storeURL, guid: "G2", peer: peer, text: "old2")

        let t = TestTransport(status: 201, responseBody: #"{"duplicate":false}"#)
        let engine = try makeEngine(transport: t)
        _ = try engine.open()

        let cycle = try await engine.runOnce()
        XCTAssertEqual(cycle.captured, 0, "history must not be replayed on first run")
        XCTAssertEqual(cycle.sent, 0)
        XCTAssertEqual(cycle.backlog, 0)
        XCTAssertEqual(t.callCount, 0)
        XCTAssertEqual(try engine.outbox.cursor(), 2)
    }

    /// New rows after the seed are captured and sent.
    func testCapturesNewRowsAfterSeed() async throws {
        try addMessage(at: storeURL, guid: "G1", peer: peer, text: "old")
        let t = TestTransport(status: 201, responseBody: #"{"duplicate":false}"#)
        let engine = try makeEngine(transport: t)
        _ = try engine.open()
        _ = try await engine.runOnce() // seeds cursor

        try addMessage(at: storeURL, guid: "G2", peer: peer, text: "new")
        let cycle = try await engine.runOnce()
        XCTAssertEqual(cycle.captured, 1)
        XCTAssertEqual(cycle.sent, 1)
        XCTAssertEqual(cycle.backlog, 0)
        XCTAssertEqual(t.callCount, 1)
        if let url = t.lastURL {
            XCTAssertEqual(url.path, "/api/messages/ingest")
        }
        XCTAssertEqual(t.authHeaders.first, "Bearer tok-123")
    }

    /// Regression: after a batch is fully acked the outbox is EMPTY, so the
    /// capture cursor must come from the persisted meta value — not
    /// MAX(outbox.store_id) (0 on an empty table, which would re-seed to
    /// the store max and silently skip the next row).
    func testSecondCaptureAfterAckUsesPersistedCursor() async throws {
        try addMessage(at: storeURL, guid: "G1", peer: peer, text: "a")

        let t = TestTransport(status: 201, responseBody: #"{"duplicate":false}"#)
        let engine = try makeEngine(transport: t)
        _ = try engine.open()
        let seedCycle = try await engine.runOnce()
        XCTAssertEqual(seedCycle.captured, 0)

        // G2 is captured, enqueued, and fully acked → outbox is empty.
        try addMessage(at: storeURL, guid: "G2", peer: peer, text: "b")
        let c1 = try await engine.runOnce()
        XCTAssertEqual(c1.captured, 1)
        XCTAssertEqual(c1.sent, 1)
        XCTAssertEqual(c1.backlog, 0, "batch fully acked; outbox now empty")

        // G3 arrives while the outbox is empty. Only a persisted cursor
        // (=2) captures it; a MAX(store_id)-derived cursor would
        // re-seed to 3 and skip it.
        try addMessage(at: storeURL, guid: "G3", peer: peer, text: "c")
        let c2 = try await engine.runOnce()
        XCTAssertEqual(c2.captured, 1, "G3 captured via the persisted cursor")
        XCTAssertEqual(c2.sent, 1)
        XCTAssertEqual(c2.backlog, 0)
        XCTAssertEqual(try engine.outbox.cursor(), 3)
    }

    /// A replayed duplicate (2xx + {"duplicate":true}) is counted and
    /// still acked (idempotent replay).
    func testReplayCountsDuplicate() async throws {
        try addMessage(at: storeURL, guid: "G1", peer: peer, text: "a")

        let t = TestTransport(status: 200, responseBody: #"{"duplicate":true}"#)
        let engine = try makeEngine(transport: t)
        _ = try engine.open()
        _ = try await engine.runOnce() // seeds cursor at 1

        try addMessage(at: storeURL, guid: "G2", peer: peer, text: "b")
        let cycle = try await engine.runOnce()
        XCTAssertEqual(cycle.captured, 1)
        XCTAssertEqual(cycle.sent, 1)
        XCTAssertEqual(cycle.replayed, 1)
        XCTAssertEqual(cycle.backlog, 0)
    }

    /// 3xx (e.g. an SSO redirect) is not delivery: the entry stays queued.
    func testHTTP302RedirectRetainsBacklog() async throws {
        try addMessage(at: storeURL, guid: "G1", peer: peer, text: "a")

        let t = TestTransport(status: 302, responseBody: "")
        let engine = try makeEngine(transport: t)
        _ = try engine.open()
        _ = try await engine.runOnce() // seeds cursor at 1

        try addMessage(at: storeURL, guid: "G2", peer: peer, text: "b")
        let cycle = try await engine.runOnce()
        XCTAssertEqual(cycle.sent, 0)
        XCTAssertEqual(cycle.failed, 1)
        XCTAssertEqual(cycle.backlog, 1)
    }

    /// 4xx from ingest: entry is NOT acked, backlog retained, and the
    /// next cycle retries it (a bad token fails loudly, never drops data).
    func testHTTP4xxRetainsBacklogAndRetries() async throws {
        try addMessage(at: storeURL, guid: "G1", peer: peer, text: "a")

        let t = TestTransport(status: 401, responseBody: #"{"detail":"bad token"}"#)
        let engine = try makeEngine(transport: t)
        _ = try engine.open()
        _ = try await engine.runOnce() // seeds cursor at 1

        try addMessage(at: storeURL, guid: "G2", peer: peer, text: "b")
        let first = try await engine.runOnce()
        XCTAssertEqual(first.failed, 1)
        XCTAssertEqual(first.sent, 0)
        XCTAssertEqual(first.backlog, 1, "unacked on 4xx")

        let second = try await engine.runOnce()
        XCTAssertEqual(second.sent, 0)
        XCTAssertEqual(second.backlog, 1)
        XCTAssertEqual(t.callCount, 2, "retried next cycle")
    }

    /// Empty-text rows are skipped by the store tail, not sent.
    func testEmptyTextRowsAreSkipped() async throws {
        try addMessage(at: storeURL, guid: "G1", peer: peer, text: "a")

        let t = TestTransport(status: 201, responseBody: #"{"duplicate":false}"#)
        let engine = try makeEngine(transport: t)
        _ = try engine.open()
        _ = try await engine.runOnce() // seeds cursor at 1

        try addMessage(at: storeURL, guid: "G2", peer: peer, text: "")
        try addMessage(at: storeURL, guid: "G3", peer: peer, text: "real")
        let cycle = try await engine.runOnce()
        XCTAssertEqual(cycle.captured, 1, "empty-body row G2 must be skipped")
        XCTAssertEqual(cycle.sent, 1)
        XCTAssertEqual(cycle.backlog, 0)
    }
}
