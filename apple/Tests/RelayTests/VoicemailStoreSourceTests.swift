import XCTest
@testable import NerveRelayCore

private final class VMBodyRecordingTransport: RelayTransport, @unchecked Sendable {
    private let lock = NSLock()
    private var bodies: [[String: Any]] = []
    var sent: [[String: Any]] { lock.lock(); defer { lock.unlock() }; return bodies }
    func send(_ request: URLRequest) async throws -> RelayHTTPResult {
        if let b = request.httpBody, let o = try? JSONSerialization.jsonObject(with: b) as? [String: Any] {
            lock.lock(); bodies.append(o); lock.unlock()
        }
        return RelayHTTPResult(status: 201, body: Data())
    }
}

final class VoicemailStoreSourceTests: XCTestCase {
    var dir: URL!
    var dbURL: URL { dir.appendingPathComponent("store.sqlitedb") }
    var stateURL: URL { dir.appendingPathComponent("state/vm-state.json") }

    override func setUpWithError() throws {
        dir = FileManager.default.temporaryDirectory.appendingPathComponent("vms-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
    }
    override func tearDownWithError() throws { try? FileManager.default.removeItem(at: dir) }

    private func engine(_ transport: VMBodyRecordingTransport) -> ChannelEngine {
        var cfg = RelayConfig(
            baseURL: URL(string: "https://relay.test")!, token: "t",
            storePath: URL(fileURLWithPath: "/tmp/x.db"),
            outboxPath: dir.appendingPathComponent("ob/outbox.db"))
        cfg.channels[.voicemail].enabled = true
        let resolver = VoicemailCallerResolver(contactsRoot: nil, callHistory: nil)
        let source = VoicemailStoreSource(
            reader: VoicemailStoreReader(url: dbURL), resolver: resolver, stateURL: stateURL)
        let e = ChannelEngine(channel: .voicemail, config: cfg, source: source, transport: transport)
        e.backoffBase = 0
        return e
    }

    private func run(_ e: ChannelEngine) async throws -> ChannelCycle {
        _ = try e.open()
        return try await e.runOnce()
    }

    func testSendsOnceThenNothing() async throws {
        try makeVoicemailStore(at: dbURL, rows: [
            FixtureVoicemail(pk: 1, uuidHex: "A1", isRead: true, transcript: "hello"),
        ])
        let t = VMBodyRecordingTransport()
        let e = engine(t)
        let c1 = try await run(e)
        XCTAssertEqual(c1.captured, 1)
        let c2 = try await e.runOnce()
        XCTAssertEqual(c2.captured, 0)
        XCTAssertEqual(t.sent.count, 1)
        let p = t.sent[0]
        XCTAssertEqual(p["voicemail_id"] as? String, "A1")
        XCTAssertEqual(p["source"] as? String, "mac-phone-store")
        XCTAssertEqual(p["read"] as? Bool, true)
        XCTAssertEqual(p["deleted"] as? Bool, false)
        XCTAssertEqual(p["transcript"] as? String, "hello")
    }

    func testPendingRowWithheldThenSentWhenTranscriptArrives() async throws {
        try makeVoicemailStore(at: dbURL, rows: [FixtureVoicemail(pk: 1, uuidHex: "B1")])
        let t = VMBodyRecordingTransport()
        let e = engine(t)
        let c1 = try await run(e)
        XCTAssertEqual(c1.captured, 0)
        XCTAssertTrue(t.sent.isEmpty)

        let blob = FakeVMTranscript.archive(FakeVMTranscript(text: "late words"))
        let hex = blob.map { String(format: "%02X", $0) }.joined()
        try updateVoicemailStore(dbURL, "UPDATE ZSTOREDMESSAGE SET ZTRANSCRIPT = X'\(hex)' WHERE Z_PK = 1")
        let c2 = try await e.runOnce()
        XCTAssertEqual(c2.captured, 1)
        XCTAssertEqual(t.sent.first?["transcript"] as? String, "late words")
    }

    func testRowDeletedBeforeFirstSeenIsNeverSent() async throws {
        try makeVoicemailStore(at: dbURL, rows: [
            FixtureVoicemail(pk: 1, uuidHex: "C1", deleted: 10, transcript: "gone"),
        ])
        let t = VMBodyRecordingTransport()
        let c = try await run(engine(t))
        XCTAssertEqual(c.captured, 0)
        XCTAssertTrue(t.sent.isEmpty)
    }

    func testDeletedAfterSendYieldsOneRetraction() async throws {
        try makeVoicemailStore(at: dbURL, rows: [
            FixtureVoicemail(pk: 1, uuidHex: "D1", transcript: "keep"),
            FixtureVoicemail(pk: 2, uuidHex: "D2", transcript: "remove me"),
        ])
        let t = VMBodyRecordingTransport()
        let e = engine(t)
        _ = try await run(e)
        XCTAssertEqual(t.sent.count, 2)

        try updateVoicemailStore(dbURL, "UPDATE ZSTOREDMESSAGE SET ZDATEDELETED = 50 WHERE Z_PK = 2")
        let c = try await e.runOnce()
        XCTAssertEqual(c.captured, 1)
        let last = t.sent.last!
        XCTAssertEqual(last["voicemail_id"] as? String, "D2")
        XCTAssertEqual(last["deleted"] as? Bool, true)

        let again = try await e.runOnce()
        XCTAssertEqual(again.captured, 0)
        XCTAssertEqual(t.sent.count, 3)
    }

    func testRowRemovedFromStoreRetracts() async throws {
        try makeVoicemailStore(at: dbURL, rows: [FixtureVoicemail(pk: 1, uuidHex: "E1", transcript: "x")])
        let t = VMBodyRecordingTransport()
        let e = engine(t)
        _ = try await run(e)
        try updateVoicemailStore(dbURL, "DELETE FROM ZSTOREDMESSAGE WHERE Z_PK = 1")
        let c = try await e.runOnce()
        XCTAssertEqual(c.captured, 1)
        XCTAssertEqual(t.sent.last?["deleted"] as? Bool, true)
        XCTAssertEqual(t.sent.last?["voicemail_id"] as? String, "E1")
        let again = try await e.runOnce()
        XCTAssertEqual(again.captured, 0)
    }

    func testLostStateFileDoesNotResendOrRetractWrongly() async throws {
        try makeVoicemailStore(at: dbURL, rows: [FixtureVoicemail(pk: 1, uuidHex: "F1", transcript: "x")])
        let t = VMBodyRecordingTransport()
        let e = engine(t)
        _ = try await run(e)
        try FileManager.default.removeItem(at: stateURL)
        let c = try await e.runOnce()
        XCTAssertEqual(c.captured, 0, "seen index dedupes the re-yielded record")
        XCTAssertEqual(t.sent.count, 1)
        XCTAssertNotEqual(t.sent.last?["deleted"] as? Bool, true)
    }

    func testStateFileHoldsIdsOnly() async throws {
        try makeVoicemailStore(at: dbURL, rows: [
            FixtureVoicemail(pk: 1, uuidHex: "A9", from: "+15550100077", transcript: "secret words"),
        ])
        _ = try await run(engine(VMBodyRecordingTransport()))
        let text = try String(contentsOf: stateURL, encoding: .utf8)
        XCTAssertTrue(text.contains("A9"))
        XCTAssertFalse(text.contains("secret"))
        XCTAssertFalse(text.contains("5550100077"))
    }

    func testUnreadableStoreThrowsWithoutTouchingOutbox() async throws {
        let t = VMBodyRecordingTransport()
        let e = engine(t)   // db file does not exist
        _ = try e.open()
        do { _ = try await e.runOnce(); XCTFail("expected throw") } catch {}
        XCTAssertTrue(t.sent.isEmpty)
    }
}
