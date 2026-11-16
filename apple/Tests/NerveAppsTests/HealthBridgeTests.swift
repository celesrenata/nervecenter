import XCTest
import NerveApps

final class HealthBridgeTests: XCTestCase {
    private let t0 = Date(timeIntervalSince1970: 1_800_000_000)

    private func sample(_ kind: HealthKind = .stepCount, offset: TimeInterval = 0) -> HealthSamplePayload {
        HealthSamplePayload(kind: kind, startAt: t0.addingTimeInterval(offset),
                            endAt: t0.addingTimeInterval(offset + 60), value: 10)
    }

    func testKindUnitsMatchServerContract() {
        XCTAssertEqual(HealthKind.bodyMass.unit, "kg")
        XCTAssertEqual(HealthKind.sleep.unit, "min")
        XCTAssertEqual(HealthKind.restingHeartRate.unit, "bpm")
        XCTAssertEqual(HealthKind.allCases.count, 7)
    }

    func testMergeSleepJoinsCloseSegmentsIntoOneNight() {
        let segs = [
            (start: t0, end: t0.addingTimeInterval(3 * 3600)),
            (start: t0.addingTimeInterval(3 * 3600 + 600), end: t0.addingTimeInterval(7 * 3600)),
            (start: t0.addingTimeInterval(30 * 3600), end: t0.addingTimeInterval(36 * 3600)),
        ]
        let nights = HealthBridge.mergeSleep(segments: segs)
        XCTAssertEqual(nights.count, 2)
        XCTAssertEqual(nights[0].value, 410)
        XCTAssertEqual(nights[0].kind, "sleep")
        XCTAssertEqual(nights[1].value, 6 * 60)
    }

    func testMergeSleepIgnoresEmptyAndInvertedSegments() {
        XCTAssertTrue(HealthBridge.mergeSleep(segments: []).isEmpty)
        XCTAssertTrue(HealthBridge.mergeSleep(segments: [(start: t0, end: t0)]).isEmpty)
    }

    func testChunkSplitsUnderServerCap() {
        let samples = (0..<950).map { sample(offset: TimeInterval($0)) }
        let parts = HealthBridge.chunk(samples)
        XCTAssertEqual(parts.map(\.count), [400, 400, 150])
        XCTAssertTrue(HealthBridge.chunk([]).isEmpty)
    }

    func testRequestCarriesAuthHeadersAndSnakeCaseBody() throws {
        let base = URL(string: "https://example.test/api")!
        let req = try HealthBridge.makeRequest(
            baseURL: base, deviceID: "relay-1", token: "tok",
            batch: HealthBatchPayload(deviceID: "relay-1", samples: [sample()])
        )
        XCTAssertEqual(req.url?.path, "/api/health/ingest")
        XCTAssertEqual(req.httpMethod, "POST")
        XCTAssertEqual(req.value(forHTTPHeaderField: "Authorization"), "Bearer tok")
        XCTAssertEqual(req.value(forHTTPHeaderField: "X-Nerve-Device"), "relay-1")
        let body = String(decoding: req.httpBody ?? Data(), as: UTF8.self)
        XCTAssertTrue(body.contains("\"device_id\""))
        XCTAssertTrue(body.contains("\"start_at\""))
    }

    func testClientRefusesToSendWhenNotPaired() async {
        let client = HealthSyncClient(apiBase: URL(string: "https://x.test/api")!, deviceID: nil, token: nil) { _ in
            XCTFail("must not hit the network when unpaired")
            return (200, Data())
        }
        let result = await client.send([sample()])
        XCTAssertEqual(result, .failure(.notPaired))
    }

    func testClientSumsBatchesAndStopsOnUnauthorized() async {
        let ok = #"{"received":400,"stored":300,"skipped":100,"sleep_records":1}"#.data(using: .utf8)!
        let client = HealthSyncClient(apiBase: URL(string: "https://x.test/api")!, deviceID: "d", token: "t") { _ in (200, ok) }
        let samples = (0..<800).map { sample(offset: TimeInterval($0)) }
        guard case .success(let total) = await client.send(samples) else { return XCTFail("expected success") }
        XCTAssertEqual(total.received, 800)
        XCTAssertEqual(total.stored, 600)
        XCTAssertEqual(total.sleepRecords, 2)

        let denied = HealthSyncClient(apiBase: URL(string: "https://x.test/api")!, deviceID: "d", token: "bad") { _ in (401, Data()) }
        let result = await denied.send(samples)
        XCTAssertEqual(result, .failure(.unauthorized))
    }

    func testSyncStateRecordsSuccessAndFailure() {
        var state = HealthSyncState()
        state.recordFailure("offline")
        XCTAssertEqual(state.lastError, "offline")
        state.recordSuccess(HealthIngestResult(received: 5, stored: 3, skipped: 2, sleepRecords: 0), at: t0)
        XCTAssertNil(state.lastError)
        XCTAssertEqual(state.sentTotal, 5)
        XCTAssertEqual(state.storedTotal, 3)
        XCTAssertEqual(state.lastSyncAt, t0)
    }
}
