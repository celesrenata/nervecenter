import XCTest
import SQLite3
@testable import NerveRelayCore

/// A synthetic `attributedBody` typedstream blob carrying `text`. Only the bytes
/// the decoder relies on are real: the `NSString` class marker, the `+` UTF-8
/// type byte and the typedstream length prefix.
private func blob(_ text: String, lengthPrefix: [UInt8]? = nil) -> Data {
    let utf8 = Array(text.utf8)
    var prefix = lengthPrefix
    if prefix == nil {
        if utf8.count < 0x80 {
            prefix = [UInt8(utf8.count)]
        } else if utf8.count <= 0xFFFF {
            prefix = [0x81, UInt8(utf8.count & 0xFF), UInt8(utf8.count >> 8)]
        } else {
            prefix = [0x82, UInt8(utf8.count & 0xFF), UInt8((utf8.count >> 8) & 0xFF),
                      UInt8((utf8.count >> 16) & 0xFF), UInt8((utf8.count >> 24) & 0xFF)]
        }
    }
    var out: [UInt8] = [0x04, 0x0B]
    out += Array("streamtyped".utf8)
    out += [0x81, 0xE8, 0x03, 0x84, 0x01, 0x40, 0x84, 0x84, 0x84]
    out += Array("NSAttributedString".utf8)
    out += [0x00, 0x84, 0x84]
    out += Array("NSObject".utf8)
    out += [0x00, 0x85, 0x92, 0x84, 0x84, 0x84]
    out += Array("NSString".utf8)
    out += [0x01, 0x94, 0x84, 0x01, 0x2B]
    out += prefix!
    out += utf8
    out += [0x86, 0x84, 0x02, 0x69, 0x49, 0x01, 0x0B, 0x92, 0x84, 0x84, 0x84]
    return Data(out)
}

final class AttributedBodyDecoderTests: XCTestCase {
    func testShortBody() {
        XCTAssertEqual(AttributedBodyDecoder.decode(blob("Lunch at noon?")), "Lunch at noon?")
    }

    func testTwoByteLengthPrefix() {
        let text = String(repeating: "abcdefghij", count: 30)  // 300 bytes
        XCTAssertEqual(AttributedBodyDecoder.decode(blob(text)), text)
    }

    func testFourByteLengthPrefix() {
        let text = String(repeating: "x", count: 70_000)
        XCTAssertEqual(AttributedBodyDecoder.decode(blob(text)), text)
    }

    func testMultibyteAndEmoji() {
        let text = "café 🎉 日本語"
        XCTAssertEqual(AttributedBodyDecoder.decode(blob(text)), text)
    }

    func testAttachmentOnlyMessageIsNotABody() {
        XCTAssertNil(AttributedBodyDecoder.decode(blob("\u{FFFC}")))
        XCTAssertNil(AttributedBodyDecoder.decode(blob(" \u{FFFC} \n")))
    }

    func testObjectReplacementIsStrippedFromRealText() {
        XCTAssertEqual(AttributedBodyDecoder.decode(blob("look \u{FFFC} here")), "look  here")
    }

    func testMalformedBlobsFailClosed() {
        XCTAssertNil(AttributedBodyDecoder.decode(Data()))
        XCTAssertNil(AttributedBodyDecoder.decode(Data([0x04, 0x0B, 0x01, 0x02])))
        // marker present but no '+' type byte
        XCTAssertNil(AttributedBodyDecoder.decode(Data(Array("NSString".utf8) + Array(repeating: 0x00, count: 30))))
        // length longer than the bytes that follow
        XCTAssertNil(AttributedBodyDecoder.decode(blob("hi", lengthPrefix: [0x7F])))
        // unknown length-prefix form
        XCTAssertNil(AttributedBodyDecoder.decode(blob("hi", lengthPrefix: [0x90])))
        // zero length
        XCTAssertNil(AttributedBodyDecoder.decode(blob("", lengthPrefix: [0x00])))
        // truncated inside the 2-byte length
        var data = [UInt8](blob("hello"))
        let at = data.lastIndex(of: 0x2B)!
        data = Array(data[...at]) + [0x81, 0x10]
        XCTAssertNil(AttributedBodyDecoder.decode(Data(data)))
    }

    func testInvalidUTF8FailsClosed() {
        var data = [UInt8](blob("abcd"))
        let at = data.lastIndex(of: 0x2B)! + 2
        data[at] = 0xFF
        data[at + 1] = 0xFE
        XCTAssertNil(AttributedBodyDecoder.decode(Data(data)))
    }

    func testOversizedLengthIsRefused() {
        let tooBig: [UInt8] = [0x82, 0xFF, 0xFF, 0xFF, 0x7F]
        XCTAssertNil(AttributedBodyDecoder.decode(blob("hi", lengthPrefix: tooBig)))
    }
}

// MARK: - a database shaped like a real chat.db

private let nsv = unsafeBitCast(-1, to: sqlite3_destructor_type.self)

private func makeRealShapedStore(at url: URL) throws -> OpaquePointer {
    var db: OpaquePointer?
    guard sqlite3_open(url.path, &db) == SQLITE_OK, let db else { throw NSError(domain: "t", code: 1) }
    let ddl = """
        CREATE TABLE handle (ROWID INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL, service TEXT);
        CREATE TABLE message (
            ROWID INTEGER PRIMARY KEY AUTOINCREMENT,
            guid TEXT UNIQUE NOT NULL,
            text TEXT,
            handle_id INTEGER DEFAULT 0,
            service TEXT,
            date INTEGER,
            is_from_me INTEGER DEFAULT 0,
            item_type INTEGER DEFAULT 0,
            attributedBody BLOB
        );
        CREATE TABLE chat_message_join (chat_id INTEGER, message_id INTEGER, message_date INTEGER DEFAULT 0);
        INSERT INTO handle (id, service) VALUES ('+15550000001', 'iMessage');
    """
    guard sqlite3_exec(db, ddl, nil, nil, nil) == SQLITE_OK else { throw NSError(domain: "t", code: 2) }
    return db
}

private func insert(_ db: OpaquePointer, guid: String, text: String?, body: Data?, service: String? = "iMessage") throws {
    var stmt: OpaquePointer?
    let sql = "INSERT INTO message (guid, text, handle_id, service, date, item_type, attributedBody) VALUES (?,?,1,?,?,0,?)"
    guard sqlite3_prepare_v2(db, sql, -1, &stmt, nil) == SQLITE_OK else { throw NSError(domain: "t", code: 3) }
    defer { sqlite3_finalize(stmt) }
    sqlite3_bind_text(stmt, 1, guid, -1, nsv)
    if let text { sqlite3_bind_text(stmt, 2, text, -1, nsv) } else { sqlite3_bind_null(stmt, 2) }
    if let service { sqlite3_bind_text(stmt, 3, service, -1, nsv) } else { sqlite3_bind_null(stmt, 3) }
    sqlite3_bind_int64(stmt, 4, 780_000_000_000_000_000)
    if let body {
        _ = body.withUnsafeBytes { sqlite3_bind_blob(stmt, 5, $0.baseAddress, Int32(body.count), nsv) }
    } else {
        sqlite3_bind_null(stmt, 5)
    }
    guard sqlite3_step(stmt) == SQLITE_DONE else { throw NSError(domain: "t", code: 4) }
}

final class RealShapeStoreTests: XCTestCase {
    private var dir: URL!
    private var url: URL!
    private var db: OpaquePointer!

    override func setUpWithError() throws {
        dir = FileManager.default.temporaryDirectory.appendingPathComponent("relay-real-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        url = dir.appendingPathComponent("chat.db")
        db = try makeRealShapedStore(at: url)
    }

    override func tearDownWithError() throws {
        sqlite3_close(db)
        try? FileManager.default.removeItem(at: dir)
    }

    func testRealSchemaIsRecognized() throws {
        let store = MessageStore(url: url)
        XCTAssertNoThrow(try store.open())
    }

    func testReadsTextAndAttributedBodyAndSkipsTheRest() throws {
        try insert(db, guid: "G1", text: "plain text", body: nil)                       // 1 readable
        try insert(db, guid: "G2", text: nil, body: blob("only in the blob"))           // 2 readable
        try insert(db, guid: "G3", text: nil, body: blob("\u{FFFC}"))                   // 3 attachment only
        try insert(db, guid: "G4", text: nil, body: nil)                                // 4 nothing at all
        try insert(db, guid: "G5", text: "no service", body: nil, service: nil)         // 5 NULL service
        let store = MessageStore(url: url)
        try store.open()
        let scan = try store.scan(afterCursor: 0, limit: 100)
        XCTAssertEqual(scan.rows.map(\.body), ["plain text", "only in the blob"])
        XCTAssertEqual(scan.rows.map(\.id), [1, 2])
        XCTAssertEqual(scan.unreadable, 3, "attachment-only, empty and NULL-service rows are counted, not fatal")
        XCTAssertEqual(scan.scannedMaxID, 5, "the scan reports how far it got, skipped rows included")
        XCTAssertEqual(try store.maxID(), 5)
    }

    func testAllUnreadableBatchStillReportsProgress() throws {
        try insert(db, guid: "G1", text: "ok", body: nil)
        for i in 2...4 { try insert(db, guid: "G\(i)", text: nil, body: nil) }
        let store = MessageStore(url: url)
        try store.open()
        let scan = try store.scan(afterCursor: 1, limit: 3)
        XCTAssertTrue(scan.rows.isEmpty)
        XCTAssertEqual(scan.unreadable, 3)
        XCTAssertEqual(scan.scannedMaxID, 4)
        XCTAssertEqual(try store.scan(afterCursor: 4, limit: 3).scannedMaxID, 4, "nothing left: no progress")
    }

    func testShapeAndDecodeStatsAreCountsOnly() throws {
        try insert(db, guid: "G1", text: "t", body: nil)
        try insert(db, guid: "G2", text: nil, body: blob("a"))
        try insert(db, guid: "G3", text: nil, body: blob("\u{FFFC}"))
        try insert(db, guid: "G4", text: nil, body: nil)
        let store = MessageStore(url: url)
        try store.open()
        let shape = try store.shape()
        XCTAssertEqual(shape.textRows, 1)
        XCTAssertEqual(shape.bodyOnlyRows, 2)
        XCTAssertEqual(shape.emptyRows, 1)
        XCTAssertTrue(shape.hasAttributedBody)
        XCTAssertEqual(shape.newestDateDigits, 18)
        let stats = try store.bodyDecodeStats()
        XCTAssertEqual(stats.decodable, 1)
        XCTAssertEqual(stats.undecodable, 1)
    }

    func testGuardStillRefusesAnUnknownSchema() throws {
        var other: OpaquePointer?
        let bad = dir.appendingPathComponent("bad.db")
        XCTAssertEqual(sqlite3_open(bad.path, &other), SQLITE_OK)
        sqlite3_exec(other, "CREATE TABLE message (ROWID INTEGER, something_else TEXT);", nil, nil, nil)
        sqlite3_close(other)
        XCTAssertThrowsError(try MessageStore(url: bad).open()) { error in
            guard case StoreError.schemaIncompatible = error else { return XCTFail("\(error)") }
        }
    }

    /// The engine must move past a batch made only of unreadable rows, or it
    /// would re-read the same batch forever and never reach newer messages.
    func testEngineAdvancesPastAnAllUnreadableBatch() async throws {
        try insert(db, guid: "G1", text: "seed", body: nil)
        let outbox = dir.appendingPathComponent("outbox.db")
        let config = RelayConfig(
            baseURL: URL(string: "http://127.0.0.1:9")!, token: "tok",
            storePath: url, outboxPath: outbox, batchSize: 3
        )
        let transport = TestTransport(status: 201, responseBody: #"{"duplicate":false}"#)
        let engine = RelayEngine(config: config, transport: transport)
        _ = try engine.open()
        _ = try await engine.runOnce()  // first run seeds the cursor at 1
        for i in 2...4 { try insert(db, guid: "U\(i)", text: nil, body: nil) }
        try insert(db, guid: "G5", text: "after the gap", body: nil)
        let first = try await engine.runOnce()  // scans 2,3,4: all unreadable
        XCTAssertEqual(first.captured, 0)
        XCTAssertEqual(try engine.outbox.cursor(), 4, "cursor moved past the unreadable rows")
        let second = try await engine.runOnce()  // now reaches the readable row
        XCTAssertEqual(second.captured, 1)
        XCTAssertEqual(second.sent, 1)
    }
}
