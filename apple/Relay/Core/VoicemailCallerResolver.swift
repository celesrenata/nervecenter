import Foundation
import SQLite3

/// Resolves a display name for a voicemail caller. Order:
///
/// 1. Contacts, by phone number (last 10 digits).
/// 2. Call history (`ZCALLRECORD`): same number, with the call's `ZDATE`
///    within 120 s of the voicemail time. `ZCALLUUID` in the voicemail store
///    does not match call history ids, so it is not used.
/// 3. nil. The caller's number is still sent; the name is optional.
///
/// Both sources are opened read-only and cached for `refreshInterval` seconds.
/// A source that cannot be read (no permission, missing) contributes nothing.
public final class VoicemailCallerResolver: @unchecked Sendable {
    public static let defaultContactsRoot = "~/Library/Application Support/AddressBook"
    public static let defaultCallHistory =
        "~/Library/Application Support/CallHistoryDB/CallHistory.storedata"
    public static let matchWindowSeconds: TimeInterval = 120

    private struct CallEntry { let key: String; let name: String; let unix: TimeInterval }

    private let contactsRoot: URL?
    private let callHistory: URL?
    private let refreshInterval: TimeInterval
    private let now: @Sendable () -> Date
    private let lock = NSLock()
    private var contactNames: [String: String] = [:]
    private var calls: [CallEntry] = []
    private var loadedAt: Date?

    public init(
        contactsRoot: URL? = RelayConfig.expand(VoicemailCallerResolver.defaultContactsRoot),
        callHistory: URL? = RelayConfig.expand(VoicemailCallerResolver.defaultCallHistory),
        refreshInterval: TimeInterval = 600,
        now: @escaping @Sendable () -> Date = { Date() }
    ) {
        self.contactsRoot = contactsRoot
        self.callHistory = callHistory
        self.refreshInterval = refreshInterval
        self.now = now
    }

    /// Last 10 digits, or nil when the value has fewer than 7 digits
    /// (short codes, non-numeric senders).
    public static func normalize(_ phone: String) -> String? {
        let digits = phone.filter { $0.isASCII && $0.isNumber }
        guard digits.count >= 7 else { return nil }
        return String(digits.suffix(10))
    }

    public func name(forPhone phone: String, receivedAtUnix: TimeInterval) -> String? {
        guard let key = Self.normalize(phone) else { return nil }
        lock.lock()
        defer { lock.unlock() }
        refreshIfNeeded()
        if let n = contactNames[key] { return n }
        return calls
            .filter { $0.key == key && abs($0.unix - receivedAtUnix) < Self.matchWindowSeconds }
            .min { abs($0.unix - receivedAtUnix) < abs($1.unix - receivedAtUnix) }?
            .name
    }

    private func refreshIfNeeded() {
        if let loadedAt, now().timeIntervalSince(loadedAt) < refreshInterval { return }
        loadedAt = now()
        contactNames = [:]
        if let root = contactsRoot, let records = try? ContactsReader.read(root: root) {
            for r in records {
                for p in r.phones {
                    if let k = Self.normalize(p.number), contactNames[k] == nil {
                        contactNames[k] = r.displayName
                    }
                }
            }
        }
        calls = callHistory.map(Self.readCallHistory) ?? []
    }

    private static func readCallHistory(_ url: URL) -> [CallEntry] {
        guard FileManager.default.fileExists(atPath: url.path) else { return [] }
        var handle: OpaquePointer?
        guard sqlite3_open_v2(url.path, &handle, SQLITE_OPEN_READONLY | SQLITE_OPEN_NOMUTEX, nil) == SQLITE_OK,
              let db = handle else {
            if let handle { sqlite3_close(handle) }
            return []
        }
        defer { sqlite3_close(db) }
        var stmt: OpaquePointer?
        let sql = "SELECT ZADDRESS, ZNAME, ZDATE FROM ZCALLRECORD WHERE ZNAME IS NOT NULL AND ZNAME != '' AND ZADDRESS IS NOT NULL"
        guard sqlite3_prepare_v2(db, sql, -1, &stmt, nil) == SQLITE_OK, let stmt else { return [] }
        defer { sqlite3_finalize(stmt) }
        var out: [CallEntry] = []
        while sqlite3_step(stmt) == SQLITE_ROW {
            let address: String?
            if sqlite3_column_type(stmt, 0) == SQLITE_BLOB {
                let n = Int(sqlite3_column_bytes(stmt, 0))
                address = n > 0 ? sqlite3_column_blob(stmt, 0).flatMap {
                    String(data: Data(bytes: $0, count: n), encoding: .utf8)
                } : nil
            } else {
                address = ncCStr(sqlite3_column_text(stmt, 0))
            }
            guard let address, let key = normalize(address),
                  let name = ncCStr(sqlite3_column_text(stmt, 1)) else { continue }
            out.append(CallEntry(
                key: key, name: name,
                unix: sqlite3_column_double(stmt, 2) + MessageStore.darwinEpochUnix))
        }
        return out
    }
}
