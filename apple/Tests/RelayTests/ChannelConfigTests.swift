import XCTest
@testable import NerveRelayCore

/// Phase 4 D5' — per-channel config surface (parse/expand/template,
/// env-only enables), the folder capture source, and the mail-connector
/// plane's env-only credential gate + registry client.
final class ChannelConfigTests: XCTestCase {
    private var dir: URL!

    override func setUpWithError() throws {
        dir = FileManager.default.temporaryDirectory
            .appendingPathComponent("relay-chcfg-\(UUID().uuidString)")
        try? FileManager.default.removeItem(at: dir)
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
    }

    override func tearDownWithError() throws {
        try? FileManager.default.removeItem(at: dir)
    }

    private func writeConfig(_ text: String) -> URL {
        let url = dir.appendingPathComponent("relay.conf")
        try! text.write(to: url, atomically: true, encoding: .utf8)
        return url
    }

    private func baseConfig() -> String {
        // Trailing newline is load-bearing: tests append per-channel keys to
        // this block; without it the first appended key glues onto the
        // `outbox_path` line and the parser drops both.
        return """
            base_url = https://relay.test
            token = t
            store_path = /tmp/chat.db
            outbox_path = \(dir.path)/relay/outbox.db

            """
    }

    // -- defaults ------------------------------------------------------------

    func testChannelsDisabledByDefault() throws {
        let cfg = try RelayConfig.load(path: writeConfig(baseConfig()))
        for channel in RelayChannel.allCases {
            XCTAssertFalse(
                cfg.channels[channel].enabled,
                "\(channel) must be opt-in, not on by default"
            )
        }
        XCTAssertTrue(cfg.channels.enabledChannels.isEmpty)
        // Defaults are §70-bounded retention, not unbounded.
        let email = cfg.channels.email
        XCTAssertEqual(email.maxEntries, RelayRetention.defaultMaxEntries)
        XCTAssertEqual(email.retentionDays, RelayRetention.defaultDays)
        XCTAssertGreaterThan(email.batchSize, 0)
        XCTAssertGreaterThan(email.interval, 0)
    }

    // -- per-channel parsing ---------------------------------------------------

    func testChannelKeysParseAndExpand() throws {
        let src = dir.appendingPathComponent("capture")
        let cfg = try RelayConfig.load(path: writeConfig(baseConfig() + """
            email_enabled = true
            email_batch = 25
            email_interval = 4
            email_max_entries = 50
            email_retention_days = 14
            email_source_dir = \(src.path)
            voicemail_enabled = 1
            """))
        XCTAssertEqual(cfg.channels.email.enabled, true)
        XCTAssertEqual(cfg.channels.email.batchSize, 25)
        XCTAssertEqual(cfg.channels.email.interval, 4)
        XCTAssertEqual(cfg.channels.email.maxEntries, 50)
        XCTAssertEqual(cfg.channels.email.retentionDays, 14)
        XCTAssertEqual(cfg.channels.email.sourceDir, src)
        XCTAssertEqual(cfg.channels.voicemail.enabled, true, "1 counts as enabled")
        XCTAssertFalse(cfg.channels.calendar.enabled)
        XCTAssertFalse(cfg.channels.contacts.enabled)
        XCTAssertEqual(cfg.channels.enabledChannels, [.email, .voicemail])
    }

    func testChannelRetentionFloorAtZero() throws {
        let cfg = try RelayConfig.load(path: writeConfig(baseConfig() + """
            calendar_enabled = true
            calendar_max_entries = 0
            calendar_retention_days = 0
            """))
        XCTAssertEqual(cfg.channels.calendar.maxEntries, 0, "0 = no entry cap (legal)")
        XCTAssertEqual(cfg.channels.calendar.retentionDays, 0, "0 = no age cap (legal)")
        XCTAssertEqual(cfg.channels.enabledChannels, [.calendar])
    }

    func testChannelEnvEnableOverridesFile() throws {
        let url = writeConfig(baseConfig())
        setenv("NERVE_RELAY_CALENDAR_ENABLED", "true", 1)
        setenv("NERVE_RELAY_EMAIL_ENABLED", "false", 1)
        defer {
            unsetenv("NERVE_RELAY_CALENDAR_ENABLED")
            unsetenv("NERVE_RELAY_EMAIL_ENABLED")
        }
        let cfg = try RelayConfig.load(path: url)
        XCTAssertTrue(cfg.channels.calendar.enabled, "env enable must turn the channel on")
        XCTAssertFalse(cfg.channels.email.enabled)
        XCTAssertEqual(cfg.channels.enabledChannels, [.calendar])
    }

    func testChannelSourceDirMustBeLocal() throws {
        // A remote "capture dir" is rejected (local-only, §47): the default
        // dir wins instead — the channel stays usable, not broken.
        let cfg = try RelayConfig.load(path: writeConfig(baseConfig() + """
            contacts_enabled = true
            contacts_source_dir = https://evil.example/capture
            """))
        XCTAssertNotEqual(
            cfg.channels.contacts.sourceDir.path,
            "https://evil.example/capture"
        )
        XCTAssertEqual(
            cfg.channels.contacts.sourceDir,
            ChannelConfig.defaultSourceDirURL,
            "remote source dir is refused — the expanded local default is used"
        )
    }

    func testChannelOutboxPathsArePerChannelAndLocal() throws {
        let cfg = try RelayConfig.load(path: writeConfig(baseConfig()))
        for channel in RelayChannel.allCases {
            let path = cfg.channelOutboxPath(channel)
            // Local file under the configured outbox dir (never remote).
            XCTAssertTrue(
                path.path.hasPrefix(dir.path),
                "\(channel) outbox must be local: \(path.path)"
            )
            XCTAssertTrue(
                path.path.contains("/channels/\(channel.outboxSlug)"),
                "one durable file per plane: \(path.path)"
            )
        }
        XCTAssertNotEqual(
            cfg.channelOutboxPath(.email).path,
            cfg.channelOutboxPath(.voicemail).path,
            "channel outboxes must be distinct files"
        )
    }

    // -- template ------------------------------------------------------------

    func testTemplateHasChannelSurfaceAndNoSecrets() throws {
        let template = RelayConfig.template()
        for key in [
            "email_enabled = false",
            "voicemail_enabled = false",
            "calendar_enabled = false",
            "contacts_enabled = false",
        ] {
            XCTAssertTrue(
                template.contains(key),
                "template must ship the opt-in switch for the channel: \(key)"
            )
        }
        XCTAssertTrue(
            template.contains("NERVE_RELAY_MAIL_TOKEN"),
            "the mailbox credential must be documented as env-only in the template"
        )
        // Tokens are env-only: the template has a placeholder, not a value,
        // and never a mail credential.
        XCTAssertFalse(template.contains("mail_token"), "no mailbox token may appear in the template")
        // The template must round-trip through the loader.
        let url = writeConfig(template)
        let cfg = try RelayConfig.load(path: url)
        XCTAssertTrue(cfg.channels.enabledChannels.isEmpty, "template channels stay off")
        XCTAssertEqual(cfg.channels.email.sourceDir, ChannelConfig.defaultSourceDirURL)
    }

    // -- folder capture source --------------------------------------------------

    private func writeCapture(_ channel: RelayChannel, _ json: String) -> URL {
        let src = dir.appendingPathComponent("capture-\(channel.rawValue)")
        try? FileManager.default.createDirectory(at: src, withIntermediateDirectories: true)
        try! json.write(to: src.appendingPathComponent("\(channel.rawValue).json"),
                        atomically: true, encoding: .utf8)
        return src
    }

    func testFolderSourceMissingFileYieldsEmpty() async throws {
        let src = FolderChannelSource(
            channel: .email,
            sourceDir: dir.appendingPathComponent("nowhere")
        )
        let records = try await src.fetch()
        XCTAssertEqual(records, [], "no capture file → nothing captured (not an error)")
        XCTAssertEqual(src.fileURL.lastPathComponent, "email.json")
    }

    func testFolderSourceDecodesAllPlanes() async throws {
        let emailSrc = writeCapture(.email, """
            [{
                "message_id": "m1",
                "direction": "RECEIVED",
                "from_address": "a@example.com",
                "to_address": "me@example.com",
                "subject": "hi",
                "timestamp_unix": 1705276800
            }]
            """)
        let emails = try await FolderChannelSource(
            channel: .email, sourceDir: emailSrc
        ).fetch()
        XCTAssertEqual(emails.count, 1)
        guard case .email(let m) = emails[0] else {
            return XCTFail("expected email record, got \(emails[0])")
        }
        XCTAssertEqual(m.messageID, "m1")
        XCTAssertEqual(m.toAddress, "me@example.com")
        XCTAssertEqual(m.source, "personal-relay", "source defaults when absent")
        XCTAssertEqual(m.body, "", "missing body normalizes to empty, not a crash")

        let vmSrc = writeCapture(.voicemail, """
            [{"voicemail_id": "vm1", "caller_phone": "+1555", "transcript": "t",
              "received_at_unix": 1705276800}]
            """)
        let vms = try await FolderChannelSource(
            channel: .voicemail, sourceDir: vmSrc
        ).fetch()
        XCTAssertEqual(vms.first?.stableID, "vm1")

        let calSrc = writeCapture(.calendar, """
            [{"event_id": "e1", "title": "T", "start_unix": 1705276800,
              "end_unix": 1705277800, "participants": ["a@b.c"]}]
            """)
        let events = try await FolderChannelSource(
            channel: .calendar, sourceDir: calSrc
        ).fetch()
        XCTAssertEqual(events.first?.stableID, "e1")

        let contactsSrc = writeCapture(.contacts, """
            [{"person_id": "p1", "display_name": "Bob", "is_vip": true}]
            """)
        let people = try await FolderChannelSource(
            channel: .contacts, sourceDir: contactsSrc
        ).fetch()
        XCTAssertEqual(people.first?.stableID, "p1")
    }

    // -- mail-connector plane -----------------------------------------------------

    func testMailPlaneFailsClosedWithoutMailToken() async throws {
        unsetenv(NerveRelayMailTokenEnvVar)
        let plane = MailConnectorPlane(config: MailConnectorPlane.Config())
        do {
            _ = try await plane.fetch()
            XCTFail("expected noMailCredential")
        } catch let e as MailCaptureError {
            guard case MailCaptureError.noMailCredential = e else {
                return XCTFail("expected noMailCredential, got \(e)")
            }
        } catch {
            XCTFail("expected noMailCredential, got \(error)")
        }
    }

    func testMailPlaneWithTokenYieldsEmptyHostSide() async throws {
        setenv(NerveRelayMailTokenEnvVar, "not-a-real-credential", 1)
        defer { unsetenv(NerveRelayMailTokenEnvVar) }
        let plane = MailConnectorPlane(config: MailConnectorPlane.Config())
        let records = try await plane.fetch()
        XCTAssertEqual(
            records, [],
            "without a live host-side IMAP implementation the plane yields " +
            "nothing rather than fabricating mail; the mailbox is never touched"
        )
    }

    func testMailPlaneMailTokenEnvLookup() {
        unsetenv(NerveRelayMailTokenEnvVar)
        XCTAssertNil(MailConnectorPlane.mailToken())
        setenv(NerveRelayMailTokenEnvVar, "  ", 1)
        defer { unsetenv(NerveRelayMailTokenEnvVar) }
        XCTAssertNil(MailConnectorPlane.mailToken(), "blank token counts as absent")
        setenv(NerveRelayMailTokenEnvVar, "real", 1)
        XCTAssertEqual(MailConnectorPlane.mailToken(), "real")
    }

    // -- mail-connector registry client --------------------------------------------

    private final class RegistryTransport: RelayTransport, @unchecked Sendable {
        let status: Int
        let body: String
        private(set) var lastRequest: URLRequest?
        init(status: Int, body: String) {
            self.status = status
            self.body = body
        }
        func send(_ request: URLRequest) async throws -> RelayHTTPResult {
            lastRequest = request
            return RelayHTTPResult(
                status: status, body: Data(body.utf8)
            )
        }
    }

    func testRegistryClientListsConnectors() async throws {
        let body = """
            {"data": [{"connector_id": "c1", "account": "me@example.com",
              "mailbox": "INBOX", "host": "imap.example.com", "port": 993,
              "ssl": true, "auth_method": "oauth2", "status": "ok",
              "token_ref": "keychain://mail", "last_checked_at": "2026-10-01T00:00:00Z"}],
             "meta": {"total": 1}}
            """
        let transport = RegistryTransport(status: 200, body: body)
        let client = MailConnectorRegistryClient(
            transport: transport,
            baseURL: URL(string: "https://relay.test")!,
            token: "tok"
        )
        let connectors = try await client.list()
        XCTAssertEqual(connectors.count, 1)
        XCTAssertEqual(connectors[0].connectorID, "c1")
        XCTAssertEqual(connectors[0].account, "me@example.com")
        XCTAssertEqual(connectors[0].host, "imap.example.com")
        XCTAssertEqual(connectors[0].authMethod, "oauth2")
        XCTAssertEqual(connectors[0].status, "ok")
        XCTAssertEqual(connectors[0].tokenRef, "keychain://mail")
        // Registry request: GET /api/mail-connectors with bearer auth.
        let req = try XCTUnwrap(transport.lastRequest)
        XCTAssertEqual(req.url?.path, "/api/mail-connectors")
        XCTAssertEqual(req.httpMethod, "GET")
        XCTAssertEqual(
            req.value(forHTTPHeaderField: "Authorization"), "Bearer tok"
        )
    }

    func testRegistryClientUnconfiguredIsEmpty() async throws {
        let client = MailConnectorRegistryClient(
            transport: RegistryTransport(status: 200, body: #"{"data": [], "meta": {"total": 0}}"#),
            baseURL: URL(string: "https://relay.test")!,
            token: "tok"
        )
        let connectors = try await client.list()
        XCTAssertEqual(connectors.count, 0, "no connectors registered → empty list")
    }

    func testRegistryClientHTTPErrorThrows() async throws {
        let client = MailConnectorRegistryClient(
            transport: RegistryTransport(status: 503, body: "unavailable"),
            baseURL: URL(string: "https://relay.test")!,
            token: "tok"
        )
        do {
            _ = try await client.list()
            XCTFail("expected http error")
        } catch let e as MailConnectorRegistryError {
            guard case MailConnectorRegistryError.http(let status, let body) = e else {
                return XCTFail("expected http case, got \(e)")
            }
            XCTAssertEqual(status, 503)
            XCTAssertEqual(body, "unavailable")
        }
    }
}
