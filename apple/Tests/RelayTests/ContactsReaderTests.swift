import XCTest
import SQLite3
@testable import NerveRelayCore

/// A synthetic AddressBook-v22.abcddb with the columns the reader uses.
private func makeAddressBook(at url: URL, _ sql: String) throws {
    try FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
    var db: OpaquePointer?
    guard sqlite3_open(url.path, &db) == SQLITE_OK, let db else { throw NSError(domain: "t", code: 1) }
    defer { sqlite3_close(db) }
    let ddl = """
        CREATE TABLE ZABCDRECORD (Z_PK INTEGER PRIMARY KEY, ZUNIQUEID TEXT, ZFIRSTNAME TEXT,
            ZLASTNAME TEXT, ZNICKNAME TEXT, ZORGANIZATION TEXT, ZTHUMBNAILIMAGEDATA BLOB);
        CREATE TABLE ZABCDPHONENUMBER (Z_PK INTEGER PRIMARY KEY, ZOWNER INTEGER, ZFULLNUMBER TEXT,
            ZLABEL TEXT, ZORDERINGINDEX INTEGER);
        CREATE TABLE ZABCDEMAILADDRESS (Z_PK INTEGER PRIMARY KEY, ZOWNER INTEGER, ZADDRESS TEXT,
            ZLABEL TEXT, ZORDERINGINDEX INTEGER);
        \(sql)
        """
    var err: UnsafeMutablePointer<CChar>?
    guard sqlite3_exec(db, ddl, nil, nil, &err) == SQLITE_OK else {
        let msg = err.map { String(cString: $0) } ?? "ddl"
        throw NSError(domain: "t", code: 2, userInfo: [NSLocalizedDescriptionKey: msg])
    }
}

private struct ScriptedTransport: RelayTransport {
    let status: Int
    let sink: Sink
    final class Sink: @unchecked Sendable { var requests: [URLRequest] = [] }
    func send(_ request: URLRequest) async throws -> RelayHTTPResult {
        sink.requests.append(request)
        return RelayHTTPResult(status: status, body: Data())
    }
}

final class ContactsReaderTests: XCTestCase {
    private var root: URL!

    override func setUpWithError() throws {
        root = FileManager.default.temporaryDirectory.appendingPathComponent("ab-\(UUID().uuidString)")
        // JPEG thumbnail with the one-byte 0x01 prefix the AddressBook uses.
        try makeAddressBook(at: root.appendingPathComponent("Sources/S1/AddressBook-v22.abcddb"), """
            INSERT INTO ZABCDRECORD VALUES (1, 'U1', 'Ada', 'Lovelace', NULL, NULL, X'01FFD8FFE000');
            INSERT INTO ZABCDRECORD VALUES (2, 'U2', NULL, NULL, NULL, 'Acme Bank', NULL);
            INSERT INTO ZABCDRECORD VALUES (3, 'U3', 'No', 'Handles', NULL, NULL, NULL);
            INSERT INTO ZABCDRECORD VALUES (4, 'U4', NULL, NULL, NULL, NULL, NULL);
            INSERT INTO ZABCDPHONENUMBER VALUES (1, 1, '(555) 123-4567', '_$!<Mobile>!$_', 0);
            INSERT INTO ZABCDEMAILADDRESS VALUES (1, 1, 'ada@example.com', 'work', 0);
            INSERT INTO ZABCDEMAILADDRESS VALUES (2, 2, 'help@acme.example', NULL, 0);
            INSERT INTO ZABCDPHONENUMBER VALUES (2, 4, '+15550000000', NULL, 0);
            """)
        // A second source repeating U1 (deduplicated by unique id).
        try makeAddressBook(at: root.appendingPathComponent("Sources/S2/AddressBook-v22.abcddb"), """
            INSERT INTO ZABCDRECORD VALUES (9, 'U1', 'Ada', 'Duplicate', NULL, NULL, NULL);
            INSERT INTO ZABCDPHONENUMBER VALUES (9, 9, '555', NULL, 0);
            """)
    }

    override func tearDownWithError() throws {
        try? FileManager.default.removeItem(at: root)
    }

    func testReadsNamesHandlesLabelsAndPhoto() throws {
        let contacts = try ContactsReader.read(root: root)
        XCTAssertEqual(contacts.map(\.uid), ["U1", "U2"])  // no handles / no name skipped
        let ada = contacts[0]
        XCTAssertEqual(ada.displayName, "Ada Lovelace")  // first source wins
        XCTAssertEqual(ada.phones, [ContactPhone(number: "(555) 123-4567", label: "Mobile")])
        XCTAssertEqual(ada.emails, [ContactEmail(address: "ada@example.com", label: "work")])
        XCTAssertEqual(ada.photoType, "image/jpeg")
        XCTAssertEqual(Data(base64Encoded: ada.photoB64 ?? ""), Data([0xFF, 0xD8, 0xFF, 0xE0, 0x00]))
        XCTAssertEqual(contacts[1].displayName, "Acme Bank")  // organization fallback
        XCTAssertNil(contacts[1].photoB64)
    }

    func testThumbnailRejectsNonImagesAndOversize() {
        let dir = root.appendingPathComponent("none")
        XCTAssertNil(ContactsReader.thumbnail(Data([0x01, 0x00, 0x01]), imagesDir: dir))
        let big = Data([0x01, 0xFF, 0xD8]) + Data(count: ContactsReader.maxPhotoBytes)
        XCTAssertNil(ContactsReader.thumbnail(big, imagesDir: dir))
        XCTAssertNil(ContactsReader.thumbnail(Data([0x02]) + Data("../x".utf8), imagesDir: dir))
    }

    func testChunksAreBoundedByCount() {
        let many = (0..<450).map {
            ContactRecord(uid: "u\($0)", displayName: "n", phones: [ContactPhone(number: "1", label: nil)],
                          emails: [], photoB64: nil, photoType: nil)
        }
        XCTAssertEqual(ContactsSync.chunks(many).map(\.count), [200, 200, 50])
    }

    func testSyncPostsOnlyWhenChangedAndSavesHashAfterSuccess() async throws {
        let contacts = try ContactsReader.read(root: root)
        let config = RelayConfig(baseURL: URL(string: "https://nc.example")!, token: "tok",
                                 storePath: root.appendingPathComponent("chat.db"),
                                 outboxPath: root.appendingPathComponent("outbox.db"),
                                 relayDeviceID: "dev-1")
        let state = root.appendingPathComponent("state/contacts.sha256")

        let failing = ScriptedTransport(status: 500, sink: .init())
        do {
            _ = try await ContactsSync(config: config, transport: failing, stateFile: state).syncIfChanged(contacts)
            XCTFail("expected an HTTP error")
        } catch {}
        XCTAssertFalse(FileManager.default.fileExists(atPath: state.path))  // not marked delivered

        let ok = ScriptedTransport(status: 200, sink: .init())
        let sync = ContactsSync(config: config, transport: ok, stateFile: state)
        let sent = try await sync.syncIfChanged(contacts)
        XCTAssertEqual(sent, 2)
        let request = try XCTUnwrap(ok.sink.requests.first)
        XCTAssertEqual(request.url?.absoluteString, "https://nc.example/api/relay/contacts")
        XCTAssertEqual(request.value(forHTTPHeaderField: "Authorization"), "Bearer tok")
        let body = try XCTUnwrap(JSONSerialization.jsonObject(with: request.httpBody ?? Data()) as? [String: Any])
        XCTAssertEqual(body["device_id"] as? String, "dev-1")
        XCTAssertEqual((body["contacts"] as? [[String: Any]])?.first?["display_name"] as? String, "Ada Lovelace")

        let again = try await sync.syncIfChanged(contacts)
        XCTAssertEqual(again, 0)  // unchanged snapshot: nothing sent
        XCTAssertEqual(ok.sink.requests.count, 1)
    }
}
