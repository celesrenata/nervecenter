import XCTest
import NerveApps
import NerveModels

final class NerveAppsTests: XCTestCase {
    func testAskAnswerProvenance() {
        let evidence = [
            Evidence(
                id: "1",
                sourceType: .toolResult,
                sourceRef: "test-tool",
                content: "Test evidence",
                capturedAt: Date(),
                trust: 0.9
            )
        ]
        let answer = AskAnswer(
            answer: "Test answer",
            evidence: evidence,
            traceID: "test-trace"
        )
        
        XCTAssertEqual(answer.provenanceSummary, "1 evidence record · trace test-tra…")
        XCTAssertTrue(answer.hasInspectableEvidence)
    }

    func testEvidenceCitation() {
        let evidence = Evidence(
            id: "1",
            sourceType: .webPage,
            sourceRef: "https://example.com",
            content: "Test content",
            capturedAt: Date(),
            trust: 0.8
        )
        let citation = EvidenceCitation(evidence: evidence)

        XCTAssertEqual(citation.sourceLabel, "Web page")
    }

    // MARK: - §66 watch approvals.json wire contract (gaps4 D6)

    /// An empty approvals array must encode to a valid, decodable `[]` —
    /// the exact bytes the phone writes when it holds no rows, and the exact
    /// bytes the Watch's decoder maps to "no approvals pending".
    func testApprovalsJSONEmptyEncodesToEmptyArray() {
        guard let data = WatchApprovalsJSON.encode([]) else {
            return XCTFail("encoding an empty approvals array must not fail")
        }
        XCTAssertEqual(String(decoding: data, as: UTF8.self), "[]")
        XCTAssertEqual(WatchApprovalsJSON.decode(data), [])
    }

    /// A real row must round-trip losslessly with the exact snake_case wire
    /// keys the Watch decoder reads (`change_type`, `risk_level`, `status`).
    func testApprovalsJSONRowRoundTripsWithSnakeCaseKeys() {
        let row = ApprovalWireRow(
            id: "pc-1",
            title: "Retire legacy goal",
            description: "Goal 'Q3 metrics' is stale.",
            riskLevel: 1,
            changeType: "goal_change",
            status: "pending_approval",
            diff: ["goal_id": .string("g-1"), "progress": .int(100)],
            rollback: ["action": .string("restore_goal")]
        )
        guard let data = WatchApprovalsJSON.encode([row]) else {
            return XCTFail("encoding an approvals array must not fail")
        }

        // The raw wire keys must be the snake_case contract literals, not
        // camelCase — this is what the Watch's `decodeApprovals` reads.
        let object = (try? JSONSerialization.jsonObject(with: data)) as? [[String: Any]]
        XCTAssertEqual(object?.first?.keys.sorted(), [
            "change_type", "description", "diff", "id",
            "risk_level", "rollback", "status", "title",
        ])

        let decoded = WatchApprovalsJSON.decode(data)
        XCTAssertEqual(decoded.count, 1)
        XCTAssertEqual(decoded[0].id, "pc-1")
        XCTAssertEqual(decoded[0].title, "Retire legacy goal")
        XCTAssertEqual(decoded[0].description, "Goal 'Q3 metrics' is stale.")
        XCTAssertEqual(decoded[0].riskLevel, 1)
        XCTAssertEqual(decoded[0].changeType, "goal_change")
        XCTAssertEqual(decoded[0].status, "pending_approval")
        XCTAssertEqual(decoded[0].diff?["goal_id"]?.stringValue, "g-1")
        XCTAssertEqual(decoded[0].diff?["progress"]?.intValue, 100)
        XCTAssertEqual(decoded[0].rollback?["action"]?.stringValue, "restore_goal")
    }

    /// The decoder degrades honestly: absent/garbage/wrong-shape data yields
    /// `[]` (never throws, never invents rows), and a row missing its
    /// optional fields still decodes with `nil`/empty defaults.
    func testApprovalsJSONDecodeIsTolerant() {
        XCTAssertEqual(WatchApprovalsJSON.decode(Data()), [])
        XCTAssertEqual(WatchApprovalsJSON.decode(Data("not json".utf8)), [])
        // A bare object is the wrong shape for an array contract.
        XCTAssertEqual(WatchApprovalsJSON.decode(Data("{}".utf8)), [])

        // A row missing its optional fields still decodes with honest defaults.
        let minimalJSON = "[{\"id\":\"x\",\"title\":\"t\",\"risk_level\":2,"
            + "\"change_type\":\"other\",\"status\":\"pending_approval\"}]"
        let rows = WatchApprovalsJSON.decode(Data(minimalJSON.utf8))
        XCTAssertEqual(rows.count, 1)
        XCTAssertEqual(rows[0].id, "x")
        XCTAssertEqual(rows[0].description, nil)
        XCTAssertEqual(rows[0].diff, nil)
        XCTAssertEqual(rows[0].rollback, nil)
    }
}