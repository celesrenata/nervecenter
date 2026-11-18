import XCTest
import SQLite3
@testable import NerveRelayCore

/// Synthetic fixture rows only: fake numbers (555 range) and invented text.
struct FixtureVoicemail {
    var pk: Int
    var type = 1
    var fileType = "amr"
    var uuidHex: String
    var from: String? = "+15550100001"
    var created: Double = 700_000_000        // Core Data seconds
    var duration: Double = 12.4
    var isRead = false
    var deleted: Double? = nil
    var transcript: String? = nil
}

func hexBlobLiteral(_ hex: String) -> String { "X'\(hex)'" }

func sqlLiteral(_ s: String?) -> String {
    guard let s else { return "NULL" }
    return "'" + s.replacingOccurrences(of: "'", with: "''") + "'"
}

/// Creates a store with the real ZSTOREDMESSAGE column names.
func makeVoicemailStore(at url: URL, rows: [FixtureVoicemail], omitColumn: String? = nil) throws {
    try FileManager.default.createDirectory(
        at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
    var db: OpaquePointer?
    guard sqlite3_open(url.path, &db) == SQLITE_OK, let db else { throw NSError(domain: "t", code: 1) }
    defer { sqlite3_close(db) }
    var cols = [
        "Z_PK INTEGER PRIMARY KEY", "ZMESSAGETYPE INTEGER", "ZFILETYPE VARCHAR", "ZRECORDUUID BLOB",
        "ZFROM VARCHAR", "ZDATECREATED TIMESTAMP", "ZDATEDELETED TIMESTAMP", "ZDURATION FLOAT",
        "ZISREAD INTEGER", "ZTRANSCRIPT BLOB", "ZMESSAGEFILE BLOB",
    ]
    if let omitColumn { cols.removeAll { $0.hasPrefix(omitColumn + " ") } }
    var sql = "CREATE TABLE ZSTOREDMESSAGE (\(cols.joined(separator: ", ")));"
    if omitColumn == nil {
        for r in rows {
            let tr: String
            if let t = r.transcript {
                let blob = FakeVMTranscript.archive(FakeVMTranscript(text: t))
                tr = "X'" + blob.map { String(format: "%02X", $0) }.joined() + "'"
            } else { tr = "NULL" }
            sql += """
                INSERT INTO ZSTOREDMESSAGE (Z_PK, ZMESSAGETYPE, ZFILETYPE, ZRECORDUUID, ZFROM, ZDATECREATED,
                    ZDATEDELETED, ZDURATION, ZISREAD, ZTRANSCRIPT, ZMESSAGEFILE)
                VALUES (\(r.pk), \(r.type), \(sqlLiteral(r.fileType)), \(hexBlobLiteral(r.uuidHex)),
                    \(sqlLiteral(r.from)), \(r.created), \(r.deleted.map { String($0) } ?? "NULL"),
                    \(r.duration), \(r.isRead ? 1 : 0), \(tr), X'DEADBEEF');
                """
        }
    }
    var err: UnsafeMutablePointer<CChar>?
    guard sqlite3_exec(db, sql, nil, nil, &err) == SQLITE_OK else {
        throw NSError(domain: "t", code: 2, userInfo: [NSLocalizedDescriptionKey: err.map { String(cString: $0) } ?? ""])
    }
}

func updateVoicemailStore(_ url: URL, _ sql: String) throws {
    var db: OpaquePointer?
    guard sqlite3_open(url.path, &db) == SQLITE_OK, let db else { throw NSError(domain: "t", code: 1) }
    defer { sqlite3_close(db) }
    guard sqlite3_exec(db, sql, nil, nil, nil) == SQLITE_OK else { throw NSError(domain: "t", code: 3) }
}

final class VoicemailStoreReaderTests: XCTestCase {
    var dir: URL!
    var dbURL: URL { dir.appendingPathComponent("Data Store/FaceTimeMessageStore-local.sqlitedb") }

    override func setUpWithError() throws {
        dir = FileManager.default.temporaryDirectory.appendingPathComponent("vm-\(UUID().uuidString)")
    }
    override func tearDownWithError() throws {
        try? FileManager.default.removeItem(at: dir)
    }

    func testReadsOnlyVoicemailRowsAndMetadata() throws {
        try makeVoicemailStore(at: dbURL, rows: [
            FixtureVoicemail(pk: 1, uuidHex: "AA01", created: 700_000_000, duration: 12.4, isRead: true, transcript: "hello one"),
            FixtureVoicemail(pk: 2, type: 0, uuidHex: "AA02", transcript: "not a voicemail"),
            FixtureVoicemail(pk: 3, fileType: "mp4", uuidHex: "AA03", transcript: "wrong type"),
            FixtureVoicemail(pk: 4, uuidHex: "AA04", from: "+15550100004"),
        ])
        let rows = try VoicemailStoreReader(url: dbURL).readAll()
        XCTAssertEqual(rows.map(\.id), ["AA01", "AA04"])
        let first = rows[0]
        XCTAssertEqual(first.phone, "+15550100001")
        XCTAssertEqual(first.durationSeconds, 12)
        XCTAssertTrue(first.isRead)
        XCTAssertEqual(first.transcript, "hello one")
        XCTAssertFalse(first.isDeleted)
        XCTAssertNil(rows[1].transcript)
        XCTAssertFalse(rows[1].hasTranscript)
    }

    func testCoreDataDateConversion() throws {
        try makeVoicemailStore(at: dbURL, rows: [
            FixtureVoicemail(pk: 1, uuidHex: "BB01", created: 0, deleted: 100),
        ])
        let r = try VoicemailStoreReader(url: dbURL).readAll()[0]
        XCTAssertEqual(r.receivedAtUnix, 978_307_200, accuracy: 0.001)
        XCTAssertEqual(r.deletedAtUnix ?? 0, 978_307_300, accuracy: 0.001)
    }

    func testSummaryCounts() throws {
        try makeVoicemailStore(at: dbURL, rows: [
            FixtureVoicemail(pk: 1, uuidHex: "01", transcript: "a"),
            FixtureVoicemail(pk: 2, uuidHex: "02", isRead: true, transcript: "b"),
            FixtureVoicemail(pk: 3, uuidHex: "03"),
            FixtureVoicemail(pk: 4, uuidHex: "04", deleted: 5, transcript: "c"),
        ])
        let s = try VoicemailStoreReader(url: dbURL).summarize()
        XCTAssertEqual(s.found, 4)
        XCTAssertEqual(s.transcribed, 2)
        XCTAssertEqual(s.withheld, 1)
        XCTAssertEqual(s.deleted, 1)
        XCTAssertEqual(s.unread, 2)
    }

    func testRowsWithoutIdOrNumberAreSkipped() throws {
        try makeVoicemailStore(at: dbURL, rows: [
            FixtureVoicemail(pk: 1, uuidHex: "CC01", from: nil),
            FixtureVoicemail(pk: 2, uuidHex: "CC02"),
        ])
        XCTAssertEqual(try VoicemailStoreReader(url: dbURL).readAll().map(\.id), ["CC02"])
    }

    func testMissingFileThrowsMissing() {
        XCTAssertThrowsError(try VoicemailStoreReader(url: dbURL).readAll()) {
            guard case StoreError.missing = $0 else { return XCTFail("\($0)") }
        }
    }

    func testMissingTableOrColumnFailsClosed() throws {
        try makeVoicemailStore(at: dbURL, rows: [], omitColumn: "ZTRANSCRIPT")
        XCTAssertThrowsError(try VoicemailStoreReader(url: dbURL).readAll()) {
            guard case StoreError.schemaIncompatible = $0 else { return XCTFail("\($0)") }
        }
        let other = dir.appendingPathComponent("other.sqlitedb")
        try updateVoicemailStoreCreatingEmpty(other)
        XCTAssertThrowsError(try VoicemailStoreReader(url: other).readAll()) {
            guard case StoreError.schemaIncompatible = $0 else { return XCTFail("\($0)") }
        }
    }

    func testReadingDoesNotChangeTheFile() throws {
        try makeVoicemailStore(at: dbURL, rows: [FixtureVoicemail(pk: 1, uuidHex: "DD01", transcript: "x")])
        let before = try Data(contentsOf: dbURL)
        _ = try VoicemailStoreReader(url: dbURL).readAll()
        _ = try VoicemailStoreReader(url: dbURL).summarize()
        XCTAssertEqual(try Data(contentsOf: dbURL), before)
    }

    private func updateVoicemailStoreCreatingEmpty(_ url: URL) throws {
        try FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        try updateVoicemailStore(url, "CREATE TABLE unrelated (a INTEGER);")
    }
}
