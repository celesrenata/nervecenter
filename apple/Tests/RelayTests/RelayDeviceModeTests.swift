import XCTest
import SQLite3
@testable import NerveRelayCore

/// Records every request, including its body.
final class RecordingTransport: RelayTransport, @unchecked Sendable {
    private(set) var requests: [URLRequest] = []
    let status: Int
    let responseBody: String
    init(status: Int = 200, responseBody: String = #"{"ok":true,"stored":1,"skipped":0}"#) {
        self.status = status
        self.responseBody = responseBody
    }
    func send(_ request: URLRequest) async throws -> RelayHTTPResult {
        requests.append(request)
        return RelayHTTPResult(status: status, body: Data(responseBody.utf8))
    }
}

final class RelayDeviceModeTests: XCTestCase {
    private var dir: URL!
    private var url: URL!
    private var db: OpaquePointer!

    override func setUpWithError() throws {
        dir = FileManager.default.temporaryDirectory.appendingPathComponent("relay-dev-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        url = dir.appendingPathComponent("chat.db")
        db = try makeRealShapedStore(at: url)
    }

    override func tearDownWithError() throws {
        sqlite3_close(db)
        try? FileManager.default.removeItem(at: dir)
    }

    private func engine(_ transport: RecordingTransport, device: String?, token: String) -> RelayEngine {
        let config = RelayConfig(
            baseURL: URL(string: "https://relay.example.test")!, token: token,
            storePath: url, outboxPath: dir.appendingPathComponent("outbox.db"),
            batchSize: 50, relayDeviceID: device
        )
        return RelayEngine(config: config, transport: transport)
    }

    private func body(of request: URLRequest) throws -> [String: Any] {
        try XCTUnwrap(JSONSerialization.jsonObject(with: try XCTUnwrap(request.httpBody)) as? [String: Any])
    }

    func testDeviceModePostsABatchToTheRelayEndpointWithTheDeviceToken() async throws {
        try insert(db, guid: "G0", text: "seed", body: nil)
        let t = RecordingTransport()
        let e = engine(t, device: "relay-abc123", token: "device-secret")
        _ = try e.open()
        _ = try await e.runOnce()  // seeds the cursor at the existing max
        try insert(db, guid: "G1", text: "hello from the relay", body: nil)
        let cycle = try await e.runOnce()
        XCTAssertEqual(cycle.captured, 1)
        XCTAssertEqual(cycle.sent, 1)
        let req = try XCTUnwrap(t.requests.first)
        XCTAssertEqual(req.url?.absoluteString, "https://relay.example.test/api/relay/messages")
        XCTAssertEqual(req.value(forHTTPHeaderField: "Authorization"), "Bearer device-secret")
        let payload = try body(of: req)
        XCTAssertEqual(payload["device_id"] as? String, "relay-abc123")
        let messages = try XCTUnwrap(payload["messages"] as? [[String: Any]])
        XCTAssertEqual(messages.count, 1)
        let m = messages[0]
        XCTAssertEqual(m["body"] as? String, "hello from the relay")
        XCTAssertEqual(m["direction"] as? String, "inbound")
        XCTAssertEqual(m["channel"] as? String, "imessage")
        XCTAssertEqual(m["peer_identifier"] as? String, "+15550000001")
        XCTAssertNotNil(m["received_at"] as? String, "legacy `timestamp` is renamed to `received_at`")
        XCTAssertNil(m["timestamp"])
        XCTAssertTrue(m["message_id"] is String)
    }

    func testLegacyModeIsUnchanged() async throws {
        try insert(db, guid: "G0", text: "seed", body: nil)
        let t = RecordingTransport(status: 201, responseBody: #"{"duplicate":false}"#)
        let e = engine(t, device: nil, token: "legacy")
        _ = try e.open()
        _ = try await e.runOnce()
        try insert(db, guid: "G1", text: "legacy path", body: nil)
        _ = try await e.runOnce()
        let req = try XCTUnwrap(t.requests.first)
        XCTAssertEqual(req.url?.path, "/api/messages/ingest")
        XCTAssertNil(try body(of: req)["device_id"])
    }

    func testRelayResponsesCountDuplicatesCorrectly() {
        XCTAssertTrue(RelayEngine.isDuplicate(Data(#"{"ok":true,"stored":0,"skipped":1}"#.utf8)))
        XCTAssertFalse(RelayEngine.isDuplicate(Data(#"{"ok":true,"stored":1,"skipped":0}"#.utf8)))
        XCTAssertFalse(RelayEngine.isDuplicate(Data(#"{"ok":true,"stored":0,"skipped":0}"#.utf8)))
        XCTAssertTrue(RelayEngine.isDuplicate(Data(#"{"duplicate":true}"#.utf8)))
        XCTAssertFalse(RelayEngine.isDuplicate(Data("not json".utf8)))
    }

    func testMalformedPayloadIsNotConvertedSilently() {
        XCTAssertNil(RelayEngine.relayBatchBody(payloadJSON: "not json", deviceID: "d"))
        XCTAssertNil(RelayEngine.relayBatchBody(payloadJSON: #"{"body":"no id"}"#, deviceID: "d"))
    }

    func testAnUnauthorizedDeviceTokenKeepsTheMessageQueued() async throws {
        try insert(db, guid: "G0", text: "seed", body: nil)
        let t = RecordingTransport(status: 401, responseBody: #"{"detail":"relay request is not authorized"}"#)
        let e = engine(t, device: "relay-abc123", token: "revoked")
        _ = try e.open()
        _ = try await e.runOnce()
        try insert(db, guid: "G1", text: "must not be lost", body: nil)
        let cycle = try await e.runOnce()
        XCTAssertEqual(cycle.failed, 1)
        XCTAssertEqual(cycle.backlog, 1, "a rejected batch stays in the durable outbox")
    }

    func testAdoptingTheImportCursorOnlyHappensOnce() throws {
        try insert(db, guid: "G1", text: "a", body: nil)
        try insert(db, guid: "G2", text: "b", body: nil)
        let e = engine(RecordingTransport(), device: "relay-abc123", token: "t")
        _ = try e.open()
        XCTAssertTrue(try e.adoptCursorIfUnset(1), "a fresh outbox adopts the import's cursor")
        XCTAssertEqual(try e.outbox.cursor(), 1)
        XCTAssertFalse(try e.adoptCursorIfUnset(2), "an existing cursor is never overwritten")
        XCTAssertFalse(try e.adoptCursorIfUnset(0))
        XCTAssertEqual(try e.outbox.cursor(), 1)
    }

    func testAdoptedCursorMeansMessagesAfterTheImportAreCaptured() async throws {
        try insert(db, guid: "G1", text: "imported earlier", body: nil)  // 1
        try insert(db, guid: "G2", text: "arrived after the import", body: nil)  // 2
        let t = RecordingTransport()
        let e = engine(t, device: "relay-abc123", token: "t")
        _ = try e.open()
        XCTAssertTrue(try e.adoptCursorIfUnset(1))
        let cycle = try await e.runOnce()
        XCTAssertEqual(cycle.captured, 1, "without adoption the first run would seed at 2 and skip it")
        let messages = try XCTUnwrap(try body(of: XCTUnwrap(t.requests.first))["messages"] as? [[String: Any]])
        XCTAssertEqual(messages.first?["body"] as? String, "arrived after the import")
    }
}
