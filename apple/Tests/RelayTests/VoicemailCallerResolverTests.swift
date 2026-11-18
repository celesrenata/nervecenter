import XCTest
import SQLite3
@testable import NerveRelayCore

final class VoicemailCallerResolverTests: XCTestCase {
    var dir: URL!
    var contactsRoot: URL { dir.appendingPathComponent("AddressBook") }
    var historyURL: URL { dir.appendingPathComponent("CallHistory.storedata") }

    override func setUpWithError() throws {
        dir = FileManager.default.temporaryDirectory.appendingPathComponent("res-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: contactsRoot, withIntermediateDirectories: true)
    }
    override func tearDownWithError() throws { try? FileManager.default.removeItem(at: dir) }

    private func writeContacts(_ sql: String) throws {
        try updateVoicemailStore(contactsRoot.appendingPathComponent("AddressBook-v22.abcddb"), """
            CREATE TABLE ZABCDRECORD (Z_PK INTEGER PRIMARY KEY, ZUNIQUEID TEXT, ZFIRSTNAME TEXT,
                ZLASTNAME TEXT, ZNICKNAME TEXT, ZORGANIZATION TEXT, ZTHUMBNAILIMAGEDATA BLOB);
            CREATE TABLE ZABCDPHONENUMBER (Z_PK INTEGER PRIMARY KEY, ZOWNER INTEGER, ZFULLNUMBER TEXT,
                ZLABEL TEXT, ZORDERINGINDEX INTEGER);
            CREATE TABLE ZABCDEMAILADDRESS (Z_PK INTEGER PRIMARY KEY, ZOWNER INTEGER, ZADDRESS TEXT,
                ZLABEL TEXT, ZORDERINGINDEX INTEGER);
            \(sql)
            """)
    }

    private func writeHistory(_ sql: String) throws {
        try updateVoicemailStore(historyURL, """
            CREATE TABLE ZCALLRECORD (Z_PK INTEGER PRIMARY KEY, ZADDRESS BLOB, ZNAME VARCHAR, ZDATE TIMESTAMP);
            \(sql)
            """)
    }

    private func resolver() -> VoicemailCallerResolver {
        VoicemailCallerResolver(contactsRoot: contactsRoot, callHistory: historyURL)
    }

    private let received: TimeInterval = 978_307_200 + 1000

    func testContactMatchAcrossFormats() throws {
        try writeContacts("""
            INSERT INTO ZABCDRECORD VALUES (1, 'U1', 'Test', 'Person', NULL, NULL, NULL);
            INSERT INTO ZABCDPHONENUMBER VALUES (1, 1, '(555) 010-0001', NULL, 0);
            """)
        let r = resolver()
        XCTAssertEqual(r.name(forPhone: "+15550100001", receivedAtUnix: received), "Test Person")
        XCTAssertEqual(r.name(forPhone: "5550100001", receivedAtUnix: received), "Test Person")
    }

    func testCallHistoryFallbackInsideWindow() throws {
        try writeHistory("INSERT INTO ZCALLRECORD VALUES (1, CAST('+15550100002' AS BLOB), 'Fallback Name', 1060);")
        XCTAssertEqual(resolver().name(forPhone: "+15550100002", receivedAtUnix: received), "Fallback Name")
    }

    func testCallHistoryOutsideWindowIsNil() throws {
        try writeHistory("INSERT INTO ZCALLRECORD VALUES (1, '+15550100002', 'Fallback Name', 1000);")
        XCTAssertNil(resolver().name(forPhone: "+15550100002", receivedAtUnix: received + 500))
    }

    func testContactsWinOverHistory() throws {
        try writeContacts("""
            INSERT INTO ZABCDRECORD VALUES (1, 'U1', 'Contact', 'Name', NULL, NULL, NULL);
            INSERT INTO ZABCDPHONENUMBER VALUES (1, 1, '+15550100003', NULL, 0);
            """)
        try writeHistory("INSERT INTO ZCALLRECORD VALUES (1, '+15550100003', 'History Name', 1000);")
        XCTAssertEqual(resolver().name(forPhone: "+15550100003", receivedAtUnix: received), "Contact Name")
    }

    func testNoMatchAndNonNumericAreNil() throws {
        try writeContacts("")
        try writeHistory("")
        let r = resolver()
        XCTAssertNil(r.name(forPhone: "+15550100009", receivedAtUnix: received))
        XCTAssertNil(r.name(forPhone: "Unknown", receivedAtUnix: received))
        XCTAssertNil(r.name(forPhone: "12345", receivedAtUnix: received))
    }

    func testMissingSourcesAreHarmless() {
        let r = VoicemailCallerResolver(contactsRoot: dir.appendingPathComponent("none"),
                                        callHistory: dir.appendingPathComponent("none.db"))
        XCTAssertNil(r.name(forPhone: "+15550100001", receivedAtUnix: received))
    }

    func testNormalize() {
        XCTAssertEqual(VoicemailCallerResolver.normalize("+1 (555) 010-0001"), "5550100001")
        XCTAssertNil(VoicemailCallerResolver.normalize("abc"))
    }
}
