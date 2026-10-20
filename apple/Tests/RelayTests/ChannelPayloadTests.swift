import XCTest
@testable import NerveRelayCore

/// Phase 4 D5' — payload/normalization tests for the four new capture
/// planes (email, voicemail, calendar, contacts). The fail-closed
/// `payloadJSON` encoder is the boundary that keeps poison rows from
/// wedging a channel outbox, so every plane gets contract coverage
/// against the confirmed backend ingest fields (snake_case).
final class ChannelPayloadTests: XCTestCase {

    private let encoder: JSONEncoder = {
        let e = JSONEncoder()
        e.outputFormatting = [.sortedKeys]
        return e
    }()

    private func object(_ json: String) throws -> [String: Any] {
        guard let data = json.data(using: .utf8),
              let obj = try JSONSerialization.jsonObject(with: data) as? [String: Any]
        else {
            XCTFail("payload is not a JSON object: \(json)")
            return [:]
        }
        return obj
    }

    // -- fixtures ----------------------------------------------------------

    private func emailRecord(
        id: String = "msg-1",
        direction: String = "RECEIVED"
    ) -> ChannelRecord {
        .email(MailMessage(
            messageID: id,
            direction: direction,
            fromAddress: "alice@example.com",
            toAddress: "me@example.com",
            senderName: "Alice",
            subject: "Re: plan",
            body: "Hello",
            timestampUnix: 1705276800,
            source: "personal-relay"
        ))
    }

    private func voicemailRecord() -> ChannelRecord {
        .voicemail(VoicemailCapture(
            voicemailID: "vm-1",
            callerPhone: "+15551230000",
            callerName: "Bob",
            transcript: "left the package at the door",
            durationSeconds: 42,
            receivedAtUnix: 1705276900,
            source: "share-sheet"
        ))
    }

    private func calendarRecord(allDay: Bool = false) -> ChannelRecord {
        .calendar(CalendarEventCapture(
            eventID: "ev-1",
            title: "Standup",
            description: "daily sync",
            startUnix: allDay ? nil : 1705363200,
            endUnix: allDay ? nil : 1705366800,
            allDay: allDay,
            location: "Room 2",
            participants: ["alice@example.com", "bob@example.com"],
            reminderAtUnix: allDay ? nil : 1705360600,
            source: "calendar-sync"
        ))
    }

    private func contactRecord() -> ChannelRecord {
        .contact(ContactCapture(
            personID: "p-1",
            displayName: "Bob",
            emails: ["bob@example.com"],
            phones: ["+15551230001"],
            notes: "prefers text",
            isVIP: true,
            source: "imported",
            updatedAtUnix: 1705276900
        ))
    }

    // -- email -------------------------------------------------------------

    func testEmailPayloadMatchesBackendContract() throws {
        let json = try ChannelEngine.payloadJSON(for: emailRecord(), encoder: encoder)
        let p = try object(json)
        XCTAssertEqual(p["message_id"] as? String, "msg-1")
        XCTAssertEqual(p["direction"] as? String, "RECEIVED")
        XCTAssertEqual(p["from_address"] as? String, "alice@example.com")
        XCTAssertEqual(p["to_address"] as? String, "me@example.com")
        XCTAssertEqual(p["sender_name"] as? String, "Alice")
        XCTAssertEqual(p["subject"] as? String, "Re: plan")
        XCTAssertEqual(p["body"] as? String, "Hello")
        XCTAssertEqual(p["source"] as? String, "personal-relay")
        XCTAssertEqual(
            p["timestamp"] as? String,
            ISO8601DateFormatter.iso8601Second
                .string(from: Date(timeIntervalSince1970: 1705276800))
        )
    }

    func testEmailDirectionIsNormalizedToUpper() throws {
        let json = try ChannelEngine.payloadJSON(
            for: emailRecord(direction: "  sent "), encoder: encoder
        )
        XCTAssertEqual(try object(json)["direction"] as? String, "SENT")
    }

    func testEmailNilSenderNameIsOmitted() throws {
        let record = ChannelRecord.email(MailMessage(
            messageID: "msg-2", direction: "RECEIVED",
            fromAddress: "alice@example.com", toAddress: "me@example.com",
            senderName: nil, subject: "s", body: "b",
            timestampUnix: 1705276800, source: "personal-relay"
        ))
        let p = try object(try ChannelEngine.payloadJSON(for: record, encoder: encoder))
        XCTAssertNil(p["sender_name"], "nil sender_name must be omitted, not null")
    }

    func testEmailBadDirectionThrows() {
        XCTAssertThrowsError(
            try ChannelEngine.payloadJSON(for: emailRecord(direction: "FORWARD"), encoder: encoder)
        ) { error in
            guard case ChannelNormalizationError.badDirection(let d) = error else {
                return XCTFail("expected badDirection, got \(error)")
            }
            XCTAssertEqual(d, "FORWARD")
        }
    }

    func testEmailEmptyIDAndPeerThrow() {
        let noID = ChannelRecord.email(MailMessage(
            messageID: "", direction: "RECEIVED", fromAddress: "a@b.c",
            toAddress: "me@example.com", senderName: nil, subject: "s",
            body: "b", timestampUnix: 1705276800, source: "x"
        ))
        XCTAssertThrowsError(try ChannelEngine.payloadJSON(for: noID, encoder: encoder))
        let noPeer = ChannelRecord.email(MailMessage(
            messageID: "msg-3", direction: "RECEIVED", fromAddress: "",
            toAddress: "me@example.com", senderName: nil, subject: "s",
            body: "b", timestampUnix: 1705276800, source: "x"
        ))
        XCTAssertThrowsError(try ChannelEngine.payloadJSON(for: noPeer, encoder: encoder))
    }

    // -- voicemail ---------------------------------------------------------

    func testVoicemailPayloadMatchesBackendContract() throws {
        let json = try ChannelEngine.payloadJSON(for: voicemailRecord(), encoder: encoder)
        let p = try object(json)
        XCTAssertEqual(p["voicemail_id"] as? String, "vm-1")
        XCTAssertEqual(p["caller_phone"] as? String, "+15551230000")
        XCTAssertEqual(p["caller_name"] as? String, "Bob")
        XCTAssertEqual(p["transcript"] as? String, "left the package at the door")
        XCTAssertEqual(p["duration_seconds"] as? Int, 42)
        XCTAssertEqual(
            p["received_at"] as? String,
            ISO8601DateFormatter.iso8601Second
                .string(from: Date(timeIntervalSince1970: 1705276900))
        )
        XCTAssertEqual(p["source"] as? String, "share-sheet")
    }

    func testVoicemailEmptyCallerThrows() {
        let bad = ChannelRecord.voicemail(VoicemailCapture(
            voicemailID: "vm-2", callerPhone: "", callerName: nil,
            transcript: "t", durationSeconds: 1, receivedAtUnix: 0,
            source: "imported"
        ))
        XCTAssertThrowsError(try ChannelEngine.payloadJSON(for: bad, encoder: encoder))
    }

    // -- calendar ----------------------------------------------------------

    func testCalendarPayloadMatchesBackendContract() throws {
        let json = try ChannelEngine.payloadJSON(for: calendarRecord(), encoder: encoder)
        let p = try object(json)
        XCTAssertEqual(p["event_id"] as? String, "ev-1")
        XCTAssertEqual(p["title"] as? String, "Standup")
        XCTAssertEqual(p["description"] as? String, "daily sync")
        XCTAssertEqual(p["all_day"] as? Bool, false)
        XCTAssertEqual(p["location"] as? String, "Room 2")
        XCTAssertEqual(p["participants"] as? [String],
                       ["alice@example.com", "bob@example.com"])
        XCTAssertEqual(
            p["start"] as? String,
            ISO8601DateFormatter.iso8601Second
                .string(from: Date(timeIntervalSince1970: 1705363200))
        )
        XCTAssertEqual(
            p["reminder_at"] as? String,
            ISO8601DateFormatter.iso8601Second
                .string(from: Date(timeIntervalSince1970: 1705360600))
        )
        XCTAssertEqual(p["source"] as? String, "calendar-sync")
    }

    func testAllDayCalendarPayloadOmitsTimestamps() throws {
        let json = try ChannelEngine.payloadJSON(
            for: calendarRecord(allDay: true), encoder: encoder
        )
        let p = try object(json)
        XCTAssertEqual(p["all_day"] as? Bool, true)
        // Backend contract: ISO-8601, nil for all-day — the keys are
        // omitted entirely (nil), not sent as "null".
        XCTAssertNil(p["start"], "all-day events carry no start timestamp")
        XCTAssertNil(p["end"])
        XCTAssertNil(p["reminder_at"])
    }

    func testCalendarEmptyTitleThrows() {
        let bad = ChannelRecord.calendar(CalendarEventCapture(
            eventID: "ev-2", title: "", description: nil,
            startUnix: nil, endUnix: nil, allDay: true, location: nil,
            participants: [], reminderAtUnix: nil, source: "calendar-sync"
        ))
        XCTAssertThrowsError(try ChannelEngine.payloadJSON(for: bad, encoder: encoder))
    }

    // -- contacts ----------------------------------------------------------

    func testContactPayloadMatchesBackendContract() throws {
        let json = try ChannelEngine.payloadJSON(for: contactRecord(), encoder: encoder)
        let p = try object(json)
        XCTAssertEqual(p["person_id"] as? String, "p-1")
        XCTAssertEqual(p["display_name"] as? String, "Bob")
        XCTAssertEqual(p["emails"] as? [String], ["bob@example.com"])
        XCTAssertEqual(p["phones"] as? [String], ["+15551230001"])
        XCTAssertEqual(p["notes"] as? String, "prefers text")
        XCTAssertEqual(p["is_vip"] as? Bool, true)
        XCTAssertEqual(p["source"] as? String, "imported")
        XCTAssertEqual(
            p["timestamp"] as? String,
            ISO8601DateFormatter.iso8601Second
                .string(from: Date(timeIntervalSince1970: 1705276900))
        )
    }

    func testContactNilTimestampIsOmitted() throws {
        let record = ChannelRecord.contact(ContactCapture(
            personID: "p-2", displayName: "Ada", emails: [], phones: [],
            notes: nil, isVIP: false, source: "personal-relay", updatedAtUnix: nil
        ))
        let p = try object(try ChannelEngine.payloadJSON(for: record, encoder: encoder))
        XCTAssertNil(p["timestamp"])
        XCTAssertNil(p["notes"])
        XCTAssertEqual(p["is_vip"] as? Bool, false)
    }

    func testContactEmptyPersonIDThrows() {
        let bad = ChannelRecord.contact(ContactCapture(
            personID: "", displayName: "Bob", emails: [], phones: [],
            notes: nil, isVIP: false, source: "imported", updatedAtUnix: nil
        ))
        XCTAssertThrowsError(try ChannelEngine.payloadJSON(for: bad, encoder: encoder))
    }

    // -- ChannelRecord helpers ----------------------------------------------

    func testChannelAndStableIDMapping() {
        XCTAssertEqual(emailRecord().channel, .email)
        XCTAssertEqual(emailRecord().stableID, "msg-1")
        XCTAssertEqual(voicemailRecord().channel, .voicemail)
        XCTAssertEqual(voicemailRecord().stableID, "vm-1")
        XCTAssertEqual(calendarRecord().channel, .calendar)
        XCTAssertEqual(calendarRecord().stableID, "ev-1")
        XCTAssertEqual(contactRecord().channel, .contacts)
        XCTAssertEqual(contactRecord().stableID, "p-1")
    }

    func testChannelOutboxSlugsAndEndpoints() {
        XCTAssertEqual(RelayChannel.email.outboxSlug, "email.db")
        XCTAssertEqual(RelayChannel.voicemail.outboxSlug, "voicemail.db")
        XCTAssertEqual(RelayChannel.calendar.outboxSlug, "calendar.db")
        XCTAssertEqual(RelayChannel.contacts.outboxSlug, "contacts.db")
        XCTAssertEqual(RelayChannel.email.endpoint, ["api", "email", "ingest"])
        XCTAssertEqual(RelayChannel.voicemail.endpoint, ["api", "voicemail", "ingest"])
        XCTAssertEqual(RelayChannel.calendar.endpoint, ["api", "calendar", "ingest"])
        XCTAssertEqual(RelayChannel.contacts.endpoint, ["api", "contacts", "ingest"])
    }

    // -- request wiring ------------------------------------------------------

    private func makeConfig() -> RelayConfig {
        RelayConfig(
            baseURL: URL(string: "https://relay.test")!,
            token: "test-token",
            storePath: URL(fileURLWithPath: "/tmp/chat.db"),
            outboxPath: URL(fileURLWithPath: "/tmp/relay/outbox.db")
        )
    }

    func testMakeRequestTargetsChannelEndpointWithBearerAuth() throws {
        let entry = OutboxEntry(seq: 1, storeID: 0, payloadJSON: "{}")
        for (channel, path) in [
            (RelayChannel.email, "/api/email/ingest"),
            (RelayChannel.voicemail, "/api/voicemail/ingest"),
            (RelayChannel.calendar, "/api/calendar/ingest"),
            (RelayChannel.contacts, "/api/contacts/ingest")
        ] {
            let request = ChannelEngine.makeRequest(
                channel: channel, config: makeConfig(), entry: entry
            )
            XCTAssertEqual(request.url?.path, path, "\(channel) must hit \(path)")
            XCTAssertEqual(request.httpMethod, "POST")
            XCTAssertEqual(
                request.value(forHTTPHeaderField: "Authorization"), "Bearer test-token"
            )
            XCTAssertEqual(
                request.value(forHTTPHeaderField: "Content-Type"), "application/json"
            )
            XCTAssertEqual(
                request.httpBody.map { String(data: $0, encoding: .utf8) }, "{}"
            )
        }
    }
}
