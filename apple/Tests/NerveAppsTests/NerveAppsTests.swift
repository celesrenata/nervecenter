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

    // MARK: - §27/§60/§67 watch attention engine (gaps4 T003-08)

    private func attentionItem(
        _ id: String,
        priority: Double,
        due: Date? = nil,
        status: String = "NOW"
    ) -> AttentionWireItem {
        AttentionWireItem(
            attentionID: id,
            reason: "reason-\(id)",
            urgency: 0.5,
            importance: 0.5,
            priority: priority,
            due: due,
            status: status,
            entities: ["entity-\(id)"]
        )
    }

    /// Ranking mirrors the backend: priority desc, earliest due first
    /// (no-due last), stable id tiebreak (spec §27).
    func testAttentionRankOrder() {
        let now = Date()
        let later = now.addingTimeInterval(3600)
        let items = [
            attentionItem("b", priority: 0.5, due: later, status: "TODAY"),
            attentionItem("a", priority: 0.9, due: now),
            attentionItem("c", priority: 0.5, due: now),
            attentionItem("d", priority: 0.9), // no due — loses to "a"
        ]
        let ranked = WatchAttentionEngine.rank(items)
        XCTAssertEqual(ranked.map(\.attentionID), ["a", "d", "c", "b"])
    }

    /// Wrist-action records replay through the escalation machine:
    /// snooze hides until the window expires, then wakes as `.open`;
    /// resolve hides; escalate stays live (spec §60).
    func testAttentionLocalStateWristRecords() {
        let now = Date()
        let snoozed = AttentionActionRecord(
            attentionID: "x",
            action: .snooze,
            requestedAt: now.addingTimeInterval(-7200),
            snoozeMinutes: 60
        )
        // Snoozed and the window has not expired → not awake. The snooze
        // was requested at now-7200 with a 60 min window, so it expires at
        // now-3600; at now-5400 the item is still asleep.
        let midWindow = WatchAttentionEngine.localState(
            for: "x", in: [snoozed], asOf: now.addingTimeInterval(-5400)
        )
        guard case .snoozed = midWindow else {
            return XCTFail("expected snoozed, got \(midWindow)")
        }
        XCTAssertFalse(AttentionEscalation.isAwake(midWindow, at: now.addingTimeInterval(-5400)))
        // Window expired → settles back to plain open.
        let awake = WatchAttentionEngine.localState(
            for: "x", in: [snoozed], asOf: now.addingTimeInterval(1)
        )
        XCTAssertEqual(awake, .open)
        XCTAssertTrue(AttentionEscalation.isAwake(awake, at: now.addingTimeInterval(1)))

        let resolved = AttentionActionRecord(
            attentionID: "y", action: .resolve, requestedAt: now
        )
        let resolvedState = WatchAttentionEngine.localState(
            for: "y", in: [resolved], asOf: now
        )
        XCTAssertEqual(resolvedState, .resolved)
        XCTAssertFalse(AttentionEscalation.isAwake(resolvedState, at: now))

        let escalated = AttentionActionRecord(
            attentionID: "z", action: .escalate, requestedAt: now
        )
        let escalatedState = WatchAttentionEngine.localState(
            for: "z", in: [escalated], asOf: now
        )
        XCTAssertEqual(escalatedState, .escalated)
        XCTAssertTrue(AttentionEscalation.isAwake(escalatedState, at: now))
    }

    /// Complication + header copy (spec §67/§27): "n NEED YOU" / "✓ CLEAR" /
    /// honest "–" when the phone has not published the personal plane.
    func testAttentionComplicationAndHeaderTexts() {
        let now = Date()
        let published = AttentionFeedState(
            published: true,
            items: [attentionItem("a", priority: 0.9), attentionItem("b", priority: 0.4)]
        )
        XCTAssertEqual(WatchAttentionEngine.needYouCount(feed: published, asOf: now), 2)
        XCTAssertEqual(
            WatchAttentionEngine.complicationText(feed: published, asOf: now),
            "2 NEED YOU"
        )
        XCTAssertEqual(
            WatchAttentionEngine.headerText(feed: published, asOf: now),
            "2 things need you"
        )

        let one = AttentionFeedState(
            published: true, items: [attentionItem("a", priority: 0.9)]
        )
        XCTAssertEqual(
            WatchAttentionEngine.headerText(feed: one, asOf: now),
            "1 thing needs you"
        )

        let empty = AttentionFeedState(published: true, items: [])
        XCTAssertEqual(WatchAttentionEngine.complicationText(feed: empty, asOf: now), "✓ CLEAR")
        XCTAssertEqual(WatchAttentionEngine.headerText(feed: empty, asOf: now), "All clear")

        let absent = AttentionFeedState(published: false, items: [])
        XCTAssertEqual(WatchAttentionEngine.complicationText(feed: absent, asOf: now), "–")
        XCTAssertEqual(WatchAttentionEngine.headerText(feed: absent, asOf: now), "")
        XCTAssertEqual(WatchAttentionEngine.needYouCount(feed: absent, asOf: now), 0)
    }

    /// `cached-state.json` attention round-trip: the wire uses snake_case
    /// keys with ISO-8601 dates; the decoder is tolerant of missing
    /// optionals; `attention: null` / missing file both mean "not
    /// published" (spec §61).
    func testAttentionJSONRoundTrip() throws {
        let due = Date(timeIntervalSince1970: 1_700_000_000)
        let item = AttentionWireItem(
            attentionID: "a-1",
            reason: "Call pharmacy",
            urgency: 0.8,
            importance: 0.9,
            priority: 0.85,
            due: due,
            status: "NOW",
            why: ["deadline"],
            suggestedAction: "call",
            entities: ["Pharmacy"]
        )
        let file = AttentionFile(attention: [item])
        guard let data = NerveJSON.encode(file) else {
            throw XCTSkip("encode failed")
        }
        // Wire keys are snake_case (NerveJSON contract).
        let text = String(decoding: data, as: UTF8.self)
        XCTAssertTrue(text.contains("\"attention\""))
        XCTAssertTrue(text.contains("\"attention_id\""), "expected snake_case attention_id in \(text)")
        XCTAssertTrue(text.contains("\"suggested_action\""), "expected snake_case suggested_action in \(text)")

        guard let decoded = WatchAttentionJSON.decodeAttentionFile(from: data) else {
            throw XCTSkip("decode failed")
        }
        let rows = decoded.attention ?? []
        XCTAssertEqual(rows.count, 1)
        XCTAssertEqual(rows[0].attentionID, "a-1")
        XCTAssertEqual(rows[0].due, due)
        XCTAssertEqual(rows[0].suggestedAction, "call")
        XCTAssertEqual(rows[0].entities, ["Pharmacy"])

        // `attention: null` → the key is present-but-empty → not published
        // (honest: the phone has not written the personal plane).
        let nullJSON = Data("{\"attention\":null}".utf8)
        let nullFile = try XCTUnwrap(WatchAttentionJSON.decodeAttentionFile(from: nullJSON))
        XCTAssertNil(nullFile.attention)

        // Absent key and malformed bytes both decode to the honest shape.
        let absentFile = try XCTUnwrap(
            WatchAttentionJSON.decodeAttentionFile(from: Data("{}".utf8))
        )
        XCTAssertNil(absentFile.attention)
        XCTAssertNil(WatchAttentionJSON.decodeAttentionFile(from: Data("not json".utf8)))
    }

    // MARK: - §68/§69 personal widget content derivation (gaps4 D4' / T003-09)

    /// A fresh install (no personal fields published) must derive to an honest
    /// empty section: nothing populated, no "needs you" line, no fabricated
    /// counts.
    func testPersonalContentFreshInstallIsHonestEmpty() {
        let content = PersonalWidgetContent.make(from: Data("{}".utf8))

        XCTAssertEqual(content.attentionCount, 0)
        XCTAssertEqual(content.waitingOnCount, 0)
        XCTAssertNil(content.topAttentionTitle)
        XCTAssertNil(content.topCommitmentTitle)
        XCTAssertNil(content.topAttentionUrgency)
        XCTAssertNil(content.topCommitmentDueAt)
        XCTAssertFalse(content.hasData)
        XCTAssertFalse(content.needsAttention)
        XCTAssertNil(content.attentionLine)
        XCTAssertNil(content.waitingLine)
        XCTAssertEqual(content.actionCount, 0)
    }

    /// Corrupt / non-object bytes degrade to the honest empty section, never to
    /// fabricated data.
    func testPersonalContentCorruptBytesAreHonestEmpty() {
        let content = PersonalWidgetContent.make(from: Data("not json".utf8))
        XCTAssertFalse(content.hasData)
        XCTAssertNil(content.attentionLine)

        // A bare array is the wrong shape for the object contract.
        let arrayContent = PersonalWidgetContent.make(from: Data("[1,2,3]".utf8))
        XCTAssertFalse(arrayContent.hasData)
        XCTAssertEqual(arrayContent.attentionCount, 0)
    }

    /// The full personal wire must decode losslessly with the exact
    /// snake_case §69 keys the phone writes, and aggregate counts correctly.
    func testPersonalContentDecodesSnakeCaseWire() {
        let wire = """
        {
          "attention_count": 3,
          "top_attention_title": "Pharmacy",
          "top_attention_urgency": 0.9,
          "top_commitment_title": "Dinner",
          "top_commitment_due_at": "2026-10-20T23:30:00Z",
          "waiting_on_count": 2,
          "system_health": "healthy",
          "active_incidents": 0
        }
        """
        let content = PersonalWidgetContent.make(from: Data(wire.utf8))

        XCTAssertTrue(content.hasData)
        // Counts aggregate to the honest values.
        XCTAssertEqual(content.attentionCount, 3)
        XCTAssertEqual(content.waitingOnCount, 2)
        XCTAssertEqual(content.actionCount, 5)
        // Top attention carries title + urgency.
        XCTAssertEqual(content.topAttentionTitle, "Pharmacy")
        XCTAssertEqual(content.topAttentionUrgency, 0.9)
        XCTAssertEqual(content.urgencyLabel, "urgent")
        // Top commitment carries title + due.
        XCTAssertEqual(content.topCommitmentTitle, "Dinner")
        XCTAssertNotNil(content.topCommitmentDueAt)
        // The engineering fields in the same file are ignored (additive).
        XCTAssertTrue(content.needsAttention)
    }

    /// An out-of-range or negative wire value is clamped to an honest range,
    /// never surfaced raw.
    func testPersonalContentClampsOutOfRangeValues() {
        let wire = """
        {
          "attention_count": -4,
          "top_attention_urgency": 1.7,
          "waiting_on_count": -1
        }
        """
        let content = PersonalWidgetContent.make(from: Data(wire.utf8))

        XCTAssertEqual(content.attentionCount, 0)
        XCTAssertEqual(content.waitingOnCount, 0)
        XCTAssertEqual(content.topAttentionUrgency, 1.0)
        XCTAssertEqual(content.urgencyLabel, "urgent")
        // The wire explicitly published `waiting_on_count` (present, negative),
        // so the block renders — but clamped to the honest zero, not the raw -1.
        XCTAssertEqual(content.waitingLine, "0 waiting")
        // `attention_count` was also present, so the queue is a known-empty one.
        XCTAssertEqual(content.attentionLine, "Clear")
    }

    /// A blank title is treated as absent (nil), not rendered.
    func testPersonalContentBlankTitleIsNil() {
        let wire = "{\"top_attention_title\": \"   \", \"attention_count\": 1}"
        let content = PersonalWidgetContent.make(from: Data(wire.utf8))
        XCTAssertNil(content.topAttentionTitle)
        XCTAssertEqual(content.attentionCount, 1)
    }

    /// A genuinely *known* empty queue (data published, zero items) honestly
    /// reads "Clear" — distinct from a fresh install, which reads nothing.
    func testPersonalContentKnownEmptyQueueIsClear() {
        let wire = "{\"attention_count\": 0, \"waiting_on_count\": 0}"
        let content = PersonalWidgetContent.make(from: Data(wire.utf8))

        XCTAssertTrue(content.hasData)
        XCTAssertFalse(content.needsAttention)
        XCTAssertEqual(content.attentionLine, "Clear")
        XCTAssertEqual(content.waitingLine, "0 waiting")
    }

    /// A non-zero queue reads "N need you"; the urgency label only appears
    /// when an urgency score is known.
    func testPersonalContentNeedsYouLineAndUrgency() {
        let wire = "{\"attention_count\": 3, \"top_attention_urgency\": 0.4}"
        let content = PersonalWidgetContent.make(from: Data(wire.utf8))
        XCTAssertEqual(content.attentionLine, "3 need you")
        XCTAssertEqual(content.urgencyLabel, "normal")

        let noUrgency = PersonalWidgetContent.make(from: Data("{\"attention_count\": 2}".utf8))
        XCTAssertEqual(noUrgency.attentionLine, "2 need you")
        XCTAssertNil(noUrgency.urgencyLabel)
    }

    /// The commitment due label renders relative day labels deterministically
    /// for a fixed reference date, and `nil` when no due time is published.
    func testPersonalContentDueLabel() {
        // A fixed reference with a known time zone so the day math is
        // deterministic.
        let calendar = Calendar(identifier: .gregorian)
        let components = DateComponents(
            timeZone: TimeZone(identifier: "UTC"),
            year: 2026, month: 10, day: 20, hour: 12, minute: 0
        )
        guard let reference = calendar.date(from: components) else {
            return XCTFail("could not build reference date")
        }

        // No due time → nil.
        let none = PersonalWidgetContent.make(from: Data("{}".utf8))
        XCTAssertNil(none.dueLabel(reference: reference))

        // Same calendar day → "today"; next day → "tomorrow"; past →
        // "overdue"; further out → a dated string.
        func makeContent(dayOffset: Int) -> PersonalWidgetContent {
            let due = calendar.date(byAdding: .day, value: dayOffset, to: reference) ?? reference
            let iso = ISO8601DateFormatter().string(from: due)
            let wire = "{\"top_commitment_due_at\": \"\(iso)\"}"
            return PersonalWidgetContent.make(from: Data(wire.utf8))
        }

        XCTAssertEqual(makeContent(dayOffset: 0).dueLabel(reference: reference), "today")
        XCTAssertEqual(makeContent(dayOffset: 1).dueLabel(reference: reference), "tomorrow")
        XCTAssertEqual(makeContent(dayOffset: -1).dueLabel(reference: reference), "overdue")
        let farAway = makeContent(dayOffset: 10).dueLabel(reference: reference)
        XCTAssertNotNil(farAway)
    }
}