import CryptoKit
import Foundation
import SQLite3

// =============================================================================
// Mac Contacts -> NerveCenter (owner: "contacts: ok", NS-ADR-097).
//
// Reads the AddressBook databases READ-ONLY (the relay's Full Disk Access):
// `~/Library/Application Support/AddressBook/Sources/*/AddressBook-v22.abcddb`
// plus the top-level one. Only names, phone numbers, email addresses and the
// small thumbnail photo are read. Contacts with no phone and no email are
// skipped (nothing could match them to a conversation).
//
// `ContactsSync` posts a full snapshot to `POST /api/relay/contacts` in
// size-bounded chunks, but only when the snapshot's SHA-256 changed since
// the last fully delivered one.
// =============================================================================

public struct ContactPhone: Codable, Equatable, Sendable {
    public let number: String
    public let label: String?
}

public struct ContactEmail: Codable, Equatable, Sendable {
    public let address: String
    public let label: String?
}

public struct ContactRecord: Codable, Equatable, Sendable {
    public let uid: String
    public let displayName: String
    public let phones: [ContactPhone]
    public let emails: [ContactEmail]
    /// Base64 thumbnail (JPEG or PNG), when present and small enough.
    public let photoB64: String?
    public let photoType: String?

    enum CodingKeys: String, CodingKey {
        case uid, phones, emails
        case displayName = "display_name"
        case photoB64 = "photo_b64"
        case photoType = "photo_type"
    }
}

public enum ContactsReader {
    /// Thumbnails larger than this are skipped (they are normally a few KiB).
    public static let maxPhotoBytes = 96 * 1024

    /// Every AddressBook database under `root` (the top-level one and each source).
    public static func databases(root: URL) -> [URL] {
        let fm = FileManager.default
        var out: [URL] = []
        let top = root.appendingPathComponent("AddressBook-v22.abcddb")
        if fm.fileExists(atPath: top.path) { out.append(top) }
        let sources = root.appendingPathComponent("Sources")
        for name in ((try? fm.contentsOfDirectory(atPath: sources.path)) ?? []).sorted() {
            let db = sources.appendingPathComponent(name).appendingPathComponent("AddressBook-v22.abcddb")
            if fm.fileExists(atPath: db.path) { out.append(db) }
        }
        return out
    }

    /// Contacts from all databases, deduplicated by unique id, sorted by uid.
    public static func read(root: URL) throws -> [ContactRecord] {
        var byUID: [String: ContactRecord] = [:]
        for db in databases(root: root) {
            for record in try read(database: db) where byUID[record.uid] == nil {
                byUID[record.uid] = record
            }
        }
        return byUID.values.sorted { $0.uid < $1.uid }
    }

    public static func read(database url: URL) throws -> [ContactRecord] {
        var handle: OpaquePointer?
        guard sqlite3_open_v2("file:\(url.path)?mode=ro", &handle,
                              SQLITE_OPEN_READONLY | SQLITE_OPEN_URI, nil) == SQLITE_OK,
              let db = handle else {
            sqlite3_close(handle)
            throw StoreError.openFailed("contacts: cannot open \(url.lastPathComponent)")
        }
        defer { sqlite3_close(db) }
        var phones: [Int64: [ContactPhone]] = [:]
        try rows(db, "SELECT ZOWNER, ZFULLNUMBER, ZLABEL FROM ZABCDPHONENUMBER WHERE ZFULLNUMBER IS NOT NULL ORDER BY ZORDERINGINDEX") { s in
            guard let number = text(s, 1)?.trimmingCharacters(in: .whitespaces), !number.isEmpty else { return }
            phones[sqlite3_column_int64(s, 0), default: []].append(ContactPhone(number: number, label: cleanLabel(text(s, 2))))
        }
        var emails: [Int64: [ContactEmail]] = [:]
        try rows(db, "SELECT ZOWNER, ZADDRESS, ZLABEL FROM ZABCDEMAILADDRESS WHERE ZADDRESS IS NOT NULL ORDER BY ZORDERINGINDEX") { s in
            guard let address = text(s, 1)?.trimmingCharacters(in: .whitespaces), !address.isEmpty else { return }
            emails[sqlite3_column_int64(s, 0), default: []].append(ContactEmail(address: address, label: cleanLabel(text(s, 2))))
        }
        let imagesDir = url.deletingLastPathComponent().appendingPathComponent("Images")
        var out: [ContactRecord] = []
        try rows(db, """
            SELECT Z_PK, ZUNIQUEID, ZFIRSTNAME, ZLASTNAME, ZNICKNAME, ZORGANIZATION, ZTHUMBNAILIMAGEDATA
            FROM ZABCDRECORD WHERE ZUNIQUEID IS NOT NULL
            """) { s in
            let pk = sqlite3_column_int64(s, 0)
            guard let uid = text(s, 1) else { return }
            let p = phones[pk] ?? [], e = emails[pk] ?? []
            guard !p.isEmpty || !e.isEmpty else { return }
            let name = displayName(first: text(s, 2), last: text(s, 3), nickname: text(s, 4), organization: text(s, 5))
            guard let name else { return }
            let photo = thumbnail(blob(s, 6), imagesDir: imagesDir)
            out.append(ContactRecord(uid: uid, displayName: name, phones: p, emails: e,
                                     photoB64: photo?.0.base64EncodedString(), photoType: photo?.1))
        }
        return out
    }

    static func displayName(first: String?, last: String?, nickname: String?, organization: String?) -> String? {
        let full = [first, last].compactMap { $0?.trimmingCharacters(in: .whitespaces) }
            .filter { !$0.isEmpty }.joined(separator: " ")
        for candidate in [full, nickname ?? "", organization ?? ""] {
            let trimmed = candidate.trimmingCharacters(in: .whitespaces)
            if !trimmed.isEmpty { return trimmed }
        }
        return nil
    }

    /// `_$!<Mobile>!$_` -> `Mobile`; custom labels pass through.
    static func cleanLabel(_ raw: String?) -> String? {
        guard var label = raw, !label.isEmpty else { return nil }
        if label.hasPrefix("_$!<"), label.hasSuffix(">!$_") {
            label = String(label.dropFirst(4).dropLast(4))
        }
        return label
    }

    /// The thumbnail bytes and MIME type. The column holds the image with a
    /// one-byte prefix (0x01), or 0x02 plus a file name under `Images/`.
    static func thumbnail(_ data: Data?, imagesDir: URL) -> (Data, String)? {
        guard var data, !data.isEmpty else { return nil }
        if data.first == 0x01 {
            data = data.dropFirst()
        } else if data.first == 0x02 {
            let ref = String(decoding: data.dropFirst(), as: UTF8.self)
                .trimmingCharacters(in: CharacterSet(charactersIn: "\0").union(.whitespaces))
            guard !ref.isEmpty, !ref.contains("/"),
                  let file = try? Data(contentsOf: imagesDir.appendingPathComponent(ref)) else { return nil }
            data = file
        }
        data = Data(data)
        guard data.count <= maxPhotoBytes else { return nil }
        if data.starts(with: [0xFF, 0xD8]) { return (data, "image/jpeg") }
        if data.starts(with: [0x89, 0x50, 0x4E, 0x47]) { return (data, "image/png") }
        return nil
    }

    private static func rows(_ db: OpaquePointer, _ sql: String, _ each: (OpaquePointer) -> Void) throws {
        var stmt: OpaquePointer?
        guard sqlite3_prepare_v2(db, sql, -1, &stmt, nil) == SQLITE_OK, let stmt else {
            throw StoreError.openFailed("contacts: unexpected schema (\(String(cString: sqlite3_errmsg(db))))")
        }
        defer { sqlite3_finalize(stmt) }
        while sqlite3_step(stmt) == SQLITE_ROW { each(stmt) }
    }

    private static func text(_ s: OpaquePointer, _ i: Int32) -> String? {
        guard let c = sqlite3_column_text(s, i) else { return nil }
        return String(cString: c)
    }

    private static func blob(_ s: OpaquePointer, _ i: Int32) -> Data? {
        guard let p = sqlite3_column_blob(s, i) else { return nil }
        return Data(bytes: p, count: Int(sqlite3_column_bytes(s, i)))
    }
}

/// Posts a changed contacts snapshot to `POST /api/relay/contacts`.
public struct ContactsSync {
    /// Upper bound per request body (the route's ingress cap is 4 MiB).
    public static let maxChunkBytes = 2 * 1024 * 1024
    public static let maxChunkContacts = 200

    public let config: RelayConfig
    public let transport: RelayTransport
    /// Where the last fully delivered snapshot hash is kept.
    public let stateFile: URL

    public init(config: RelayConfig, transport: RelayTransport = URLSessionRelayTransport(), stateFile: URL) {
        self.config = config
        self.transport = transport
        self.stateFile = stateFile
    }

    public static func digest(_ contacts: [ContactRecord]) -> String {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.sortedKeys]
        let data = (try? encoder.encode(contacts)) ?? Data()
        return SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
    }

    /// Chunks bounded by count and encoded size.
    public static func chunks(_ contacts: [ContactRecord]) -> [[ContactRecord]] {
        var out: [[ContactRecord]] = []
        var current: [ContactRecord] = []
        var size = 0
        for c in contacts {
            let bytes = ((try? JSONEncoder().encode(c))?.count ?? 0) + 1
            if !current.isEmpty && (current.count >= maxChunkContacts || size + bytes > maxChunkBytes) {
                out.append(current)
                current = []
                size = 0
            }
            current.append(c)
            size += bytes
        }
        if !current.isEmpty { out.append(current) }
        return out
    }

    /// Sends the snapshot if it changed. Returns the number of contacts sent
    /// (0 when unchanged). The hash is saved only after every chunk succeeded.
    @discardableResult
    public func syncIfChanged(_ contacts: [ContactRecord]) async throws -> Int {
        guard let device = config.relayDeviceID else { return 0 }
        let hash = Self.digest(contacts)
        if (try? String(contentsOf: stateFile, encoding: .utf8))?.trimmingCharacters(in: .whitespacesAndNewlines) == hash {
            return 0
        }
        let parts = Self.chunks(contacts)
        for (index, part) in parts.enumerated() {
            var url = config.baseURL
            url.appendPathComponent("api")
            url.appendPathComponent("relay")
            url.appendPathComponent("contacts")
            var request = URLRequest(url: url)
            request.httpMethod = "POST"
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.setValue("Bearer \(config.token)", forHTTPHeaderField: "Authorization")
            request.timeoutInterval = 60
            let body: [String: Any] = [
                "device_id": device,
                "snapshot": hash,
                "chunk": index,
                "chunks": parts.count,
                "contacts": try JSONSerialization.jsonObject(with: JSONEncoder().encode(part)),
            ]
            request.httpBody = try JSONSerialization.data(withJSONObject: body)
            let result = try await transport.send(request)
            guard (200..<300).contains(result.status) else {
                throw RelayTransportError.http(status: result.status,
                                               body: String(decoding: result.body.prefix(200), as: UTF8.self))
            }
        }
        try? FileManager.default.createDirectory(at: stateFile.deletingLastPathComponent(),
                                                 withIntermediateDirectories: true)
        try hash.write(to: stateFile, atomically: true, encoding: .utf8)
        return contacts.count
    }
}
