import Foundation
import SQLite3

/// One voicemail row from the Mac's FaceTime/Phone message store. Holds
/// metadata and Apple's own transcript only; the audio (`ZMESSAGEFILE`) is
/// never selected.
public struct VoicemailRow: Sendable, Equatable {
    /// `ZRECORDUUID` as upper-case hex: stable across restarts.
    public let id: String
    /// `ZFROM` (caller number, usually E.164).
    public let phone: String
    /// `ZDATECREATED` converted to Unix seconds.
    public let receivedAtUnix: TimeInterval
    public let durationSeconds: Int
    public let isRead: Bool
    /// `ZDATEDELETED` as Unix seconds; nil when not deleted.
    public let deletedAtUnix: TimeInterval?
    /// Decoded Apple transcript, nil when absent or undecodable.
    public let transcript: String?

    public var isDeleted: Bool { deletedAtUnix != nil }
    public var hasTranscript: Bool { !(transcript ?? "").isEmpty }
}

/// Counts only: safe to print or log.
public struct VoicemailStoreSummary: Sendable, Equatable {
    public var found = 0
    /// Not deleted and the transcript decoded to non-empty text.
    public var transcribed = 0
    /// Not deleted and no usable transcript (held back from NerveCenter).
    public var withheld = 0
    public var deleted = 0
    /// Not deleted and unread.
    public var unread = 0
    public init() {}
}

/// Read-only reader for
/// `~/Library/Group Containers/group.com.apple.FaceTime/com.apple.facetimemessagestored/Data Store/FaceTimeMessageStore-local.sqlitedb`.
///
/// Opened `SQLITE_OPEN_READONLY` per call (so each read sees the current WAL
/// state and no handle outlives a scan). Only `ZMESSAGETYPE = 1` rows with
/// `ZFILETYPE = 'amr'` are voicemails. Fails closed on an unrecognized schema.
public struct VoicemailStoreReader: Sendable {
    public static let defaultPath =
        "~/Library/Group Containers/group.com.apple.FaceTime/com.apple.facetimemessagestored/Data Store/FaceTimeMessageStore-local.sqlitedb"

    public let url: URL

    public init(url: URL) { self.url = url }

    static let requiredColumns = [
        "ZMESSAGETYPE", "ZFILETYPE", "ZRECORDUUID", "ZFROM", "ZDATECREATED",
        "ZDATEDELETED", "ZDURATION", "ZISREAD", "ZTRANSCRIPT",
    ]

    /// Every voicemail row, oldest first. Rows without a record UUID or number
    /// cannot be identified and are skipped.
    public func readAll() throws -> [VoicemailRow] {
        guard FileManager.default.fileExists(atPath: url.path) else {
            throw StoreError.missing(url.path)
        }
        var handle: OpaquePointer?
        let rc = sqlite3_open_v2(
            url.path, &handle, SQLITE_OPEN_READONLY | SQLITE_OPEN_NOMUTEX, nil)
        guard rc == SQLITE_OK, let db = handle else {
            if let handle { sqlite3_close(handle) }
            throw StoreError.openFailed("sqlite3_open_v2 rc=\(rc)")
        }
        defer { sqlite3_close(db) }
        try assertSchema(db)

        // ZMESSAGEFILE (audio) is deliberately not selected.
        let sql = """
            SELECT ZRECORDUUID, ZFROM, ZDATECREATED, ZDURATION, ZISREAD,
                   ZDATEDELETED, ZTRANSCRIPT
            FROM ZSTOREDMESSAGE
            WHERE ZMESSAGETYPE = 1 AND ZFILETYPE = 'amr'
            ORDER BY ZDATECREATED ASC, Z_PK ASC
            """
        var stmt: OpaquePointer?
        guard sqlite3_prepare_v2(db, sql, -1, &stmt, nil) == SQLITE_OK, let stmt else {
            throw StoreError.rowError("prepare failed")
        }
        defer { sqlite3_finalize(stmt) }

        var rows: [VoicemailRow] = []
        while true {
            let step = sqlite3_step(stmt)
            if step == SQLITE_DONE { break }
            guard step == SQLITE_ROW else {
                throw StoreError.rowError("step rc=\(step)")
            }
            guard let idBytes = blob(stmt, 0), !idBytes.isEmpty,
                  let phone = ncCStr(sqlite3_column_text(stmt, 1)), !phone.isEmpty
            else { continue }
            let created = sqlite3_column_type(stmt, 2) == SQLITE_NULL
                ? 0 : sqlite3_column_double(stmt, 2)
            let deleted: TimeInterval? = sqlite3_column_type(stmt, 5) == SQLITE_NULL
                ? nil : sqlite3_column_double(stmt, 5) + MessageStore.darwinEpochUnix
            let transcript = blob(stmt, 6).flatMap { VoicemailTranscriptDecoder.decode($0) }
            rows.append(VoicemailRow(
                id: idBytes.map { String(format: "%02X", $0) }.joined(),
                phone: phone,
                receivedAtUnix: created + MessageStore.darwinEpochUnix,
                durationSeconds: Int(max(0, sqlite3_column_double(stmt, 3)).rounded()),
                isRead: sqlite3_column_int(stmt, 4) != 0,
                deletedAtUnix: deleted,
                transcript: transcript
            ))
        }
        return rows
    }

    /// Counts for the dry run and logs (no content).
    public func summarize() throws -> VoicemailStoreSummary {
        Self.summarize(try readAll())
    }

    public static func summarize(_ rows: [VoicemailRow]) -> VoicemailStoreSummary {
        var s = VoicemailStoreSummary()
        s.found = rows.count
        for r in rows {
            if r.isDeleted { s.deleted += 1; continue }
            if r.hasTranscript { s.transcribed += 1 } else { s.withheld += 1 }
            if !r.isRead { s.unread += 1 }
        }
        return s
    }

    private func blob(_ stmt: OpaquePointer, _ col: Int32) -> Data? {
        guard sqlite3_column_type(stmt, col) == SQLITE_BLOB else { return nil }
        let n = Int(sqlite3_column_bytes(stmt, col))
        guard n > 0, let p = sqlite3_column_blob(stmt, col) else { return nil }
        return Data(bytes: p, count: n)
    }

    private func assertSchema(_ db: OpaquePointer) throws {
        var stmt: OpaquePointer?
        guard sqlite3_prepare_v2(db, "PRAGMA table_info(ZSTOREDMESSAGE)", -1, &stmt, nil) == SQLITE_OK,
              let stmt else {
            // Typically an unreadable file (no Full Disk Access) or not a database.
            throw StoreError.openFailed("cannot read schema")
        }
        defer { sqlite3_finalize(stmt) }
        var present = Set<String>()
        while sqlite3_step(stmt) == SQLITE_ROW {
            if let name = ncCStr(sqlite3_column_text(stmt, 1)) { present.insert(name.uppercased()) }
        }
        let missing = Self.requiredColumns.filter { !present.contains($0) }
        guard missing.isEmpty else {
            throw StoreError.schemaIncompatible(
                present.isEmpty ? "table ZSTOREDMESSAGE missing"
                    : "ZSTOREDMESSAGE lacks \(missing.joined(separator: ","))")
        }
    }
}
