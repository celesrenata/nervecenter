import XCTest
@testable import NerveRelayCore

final class VoicemailStoreWatcherTests: XCTestCase {
    var dir: URL!
    var storeURL: URL { dir.appendingPathComponent("FaceTimeMessageStore-local.sqlitedb") }

    override func setUpWithError() throws {
        let base = FileManager.default.temporaryDirectory.appendingPathComponent("vw-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: base, withIntermediateDirectories: true)
        dir = base.resolvingSymlinksInPath()
    }
    override func tearDownWithError() throws { try? FileManager.default.removeItem(at: dir) }

    func testWaitTimesOutWhenIdle() async {
        let s = WakeSignal()
        let t0 = Date()
        let woke = await s.wait(timeout: 0.2)
        XCTAssertFalse(woke)
        XCTAssertGreaterThanOrEqual(Date().timeIntervalSince(t0), 0.15)
    }

    func testSignalBeforeWaitCoalesces() async {
        let s = WakeSignal()
        s.signal(); s.signal(); s.signal()
        let first = await s.wait(timeout: 1)
        XCTAssertTrue(first)
        let second = await s.wait(timeout: 0.1)
        XCTAssertFalse(second)
    }

    func testSignalWakesWaiter() async {
        let s = WakeSignal()
        DispatchQueue.global().asyncAfter(deadline: .now() + 0.1) { s.signal() }
        let woke = await s.wait(timeout: 5)
        XCTAssertTrue(woke)
    }

    func testMatchesOnlyStoreFamily() {
        let w = VoicemailStoreWatcher(storeURL: storeURL)
        XCTAssertTrue(w.matches(storeURL.path))
        XCTAssertTrue(w.matches(storeURL.path + "-wal"))
        XCTAssertTrue(w.matches(storeURL.path + "-shm"))
        XCTAssertFalse(w.matches(dir.appendingPathComponent("other.txt").path))
        XCTAssertFalse(w.matches(dir.appendingPathComponent("Assets/x.amr").path))
    }

    func testStoreWriteFiresSignal() async throws {
        let w = VoicemailStoreWatcher(storeURL: storeURL, latency: 0.1)
        XCTAssertTrue(w.start())
        defer { w.stop() }
        try await Task.sleep(nanoseconds: 300_000_000)
        try Data("x".utf8).write(to: URL(fileURLWithPath: storeURL.path + "-wal"))
        let woke = await w.signal.wait(timeout: 5)
        XCTAssertTrue(woke)
    }

    func testUnrelatedFileDoesNotFire() async throws {
        let w = VoicemailStoreWatcher(storeURL: storeURL, latency: 0.1)
        XCTAssertTrue(w.start())
        defer { w.stop() }
        try await Task.sleep(nanoseconds: 300_000_000)
        try Data("x".utf8).write(to: dir.appendingPathComponent("unrelated.txt"))
        let woke = await w.signal.wait(timeout: 1)
        XCTAssertFalse(woke)
    }

    func testStopReleasesStreamAndIsIdempotent() async throws {
        let w = VoicemailStoreWatcher(storeURL: storeURL, latency: 0.1)
        XCTAssertTrue(w.start())
        w.stop()
        w.stop()
        try await Task.sleep(nanoseconds: 200_000_000)
        try Data("x".utf8).write(to: storeURL)
        let woke = await w.signal.wait(timeout: 0.5)
        XCTAssertFalse(woke, "no events after stop")
    }
}
