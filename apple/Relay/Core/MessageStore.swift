import Foundation
import SQLite3

/// A single normalized message row as read from the Messages database.
///
/// Field names mirror the Messages schema (`chat.db`) so the schema guard
/// can verify every column it relies on exists before we touch it.
public struct RawMessage: Sendable, Equatable {
    /// Stable unique key for the outbox. Uses `id`, which is unique per row
    /// (not per handle), so replays are idempotent on the NerveCenter side.
    public let id: Int64
    /// `item_message` GUID — surfaced to the backend as `message_id`.
    public let guid: String
    /// `handle.id` (phone number or email) of the conversation.
    public let peerIdentifier: String
    /// `is_from_me` — 0 = inbound, 1 = outbound.
    public let isFromMe: Int32
    /// `text` — empty for media/tapback-only rows (those are skipped,
    /// not sent as empty-body messages).
    public let body: String
    /// `is_from_me == 0` and service == "iMessage" → iMessage, else SMS.
    public let service: String
    /// `date` — **nanoseconds since 2001-01-01 00:00:00 UTC** (the Darwin
    /// reference date). The normalizer converts to real epoch seconds.
    public let dateNanos: Int64
}

public enum StoreError: Error, Equatable {
    /// The store file is missing (no permission, no sync, or wrong path).
    case missing(String)
    /// We could not open the file even read-only (usually Full Disk
    /// Access has not been granted to the relay process).
    case openFailed(String)
    /// The schema is not recognizable — we refuse to parse rather than
    /// risk misreading data (prompt2 §11: "detect schema incompatibility",
    /// "fail closed when parsing is uncertain").
    case schemaIncompatible(String)
    /// A row failed to read (NULL where we expected a value, etc.).
    case rowError(String)
}

/// Read-only tail of the macOS Messages store (`~/Library/Messages/chat.db`).
///
/// prompt2 §11 Strategy A:
/// - operate **read-only** against source message storage (opened
///   `SQLITE_OPEN_READONLY`; no writes, no `PRAGMA` that mutates);
/// - never modify the Messages database;
/// - detect schema incompatibility before any parse (see
///   `assertSchema`, and `schemaVersion` for observability).
///
/// The reader is deliberately tiny: one `SELECT` against
/// `message`/`handle`/`chat_message`, ordered by `id`, filtered by
/// `id > :cursor`. The WAL journal mode means readers do not block the
/// Messages app, and Messages' own writer does not block us.
public final class MessageStore {
    public let url: URL
    private var db: OpaquePointer?

    /// Darwin reference date: 2001-01-01T00:00:00Z == Unix 978307200 s.
    public static let darwinEpochUnix: TimeInterval = 978_307_200

    public init(url: URL) {
        self.url = url
    }

    deinit {
        close()
    }

    public enum OpenResult {
        case ok(schemaVersion: String)
        case notFound
    }

    /// Open read-only and verify the schema. Idempotent.
    @discardableResult
    public func open() throws -> OpenResult {
        if db != nil { return .ok(schemaVersion: try self.schemaVersion()) }
        guard FileManager.default.fileExists(atPath: url.path) else {
            throw StoreError.missing(url.path)
        }
        var handle: OpaquePointer?
        // READONLY: the relay must never be able to write the store,
        // regardless of process permissions. NO_MUTEX is safe because
        // the store is accessed by a single thread (the relay loop).
        _ = sqlite3_open_v2(
            url.path, &handle,
            SQLITE_OPEN_READONLY | SQLITE_OPEN_NOMUTEX, nil
        )
        guard handle != nil else {
            // open_v2 failed before a handle existed (e.g. OOM) —
            // sqlite3_errmsg(nil) is undefined, so report generically.
            throw StoreError.openFailed("sqlite3_open_v2 failed to open \(url.path)")
        }
        self.db = handle
        // Fail closed on schema mismatch BEFORE any data is read.
        _ = try assertSchema()
        return .ok(schemaVersion: try schemaVersion())
    }

    public func close() {
        if let db { sqlite3_close(db); self.db = nil }
    }

    /// Messages store schema marker: the `chat_message` join table is what
    /// links a message to its conversation; its presence plus the exact
    /// columns we SELECT is what we consider "recognizable". If any of
    /// these is missing we throw `schemaIncompatible` and the relay
    /// refuses to run rather than guess.
    public func assertSchema() throws {
        guard let db else { throw StoreError.openFailed("store not opened") }
        // Real Messages databases differ from the original fixtures in two
        // names: the message key is `ROWID` (not `id`), and the chat link table
        // is `chat_message_join` (not `chat_message`). Either spelling is
        // accepted; a table or column missing under every spelling still
        // refuses (fail closed), and an unknown table never matches by accident.
        let required: [(tables: [String], anyOf: [String])] = [
            (["message"], ["ROWID", "id"]),
            (["message"], ["guid"]),
            (["message"], ["text"]),
            (["message"], ["is_from_me"]),
            (["message"], ["service"]),
            (["message"], ["date"]),
            (["message"], ["item_type"]),
            (["handle"], ["id"]),
            (["chat_message_join", "chat_message"], ["message_id"]),
        ]
        for (tables, anyOf) in required {
            var found = false
            for table in tables {
                var stmt: OpaquePointer?
                let q = "PRAGMA table_info(\(table))"
                guard sqlite3_prepare_v2(db, q, -1, &stmt, nil) == SQLITE_OK, let stmt else {
                    throw StoreError.schemaIncompatible("cannot introspect table \(table)")
                }
                defer { sqlite3_finalize(stmt) }
                while sqlite3_step(stmt) == SQLITE_ROW {
                    let name = sqlite3_column_text(stmt, 1).flatMap { String(cString: $0) }
                    if let name, anyOf.contains(where: { $0.caseInsensitiveCompare(name) == .orderedSame }) {
                        found = true
                        break
                    }
                }
                if found { break }
            }
            if !found {
                throw StoreError.schemaIncompatible(
                    "table \(tables.joined(separator: " or ")) is missing column \(anyOf.joined(separator: " or ")) — refusing to parse an unrecognized schema"
                )
            }
        }
    }

    /// `PRAGMA user_version` — reported in `status` for observability.
    public func schemaVersion() throws -> String {
        guard let db else { throw StoreError.openFailed("store not opened") }
        var stmt: OpaquePointer?
        guard sqlite3_prepare_v2(db, "PRAGMA user_version", -1, &stmt, nil) == SQLITE_OK,
              let stmt
        else { throw StoreError.schemaIncompatible("cannot read user_version") }
        defer { sqlite3_finalize(stmt) }
        guard sqlite3_step(stmt) == SQLITE_ROW else { return "unknown" }
        return ncCStr(sqlite3_column_text(stmt, 0)) ?? "unknown"
    }

    /// Result of one scan: the readable rows, plus how far the scan got.
    ///
    /// `scannedMaxID` is the highest row id examined, INCLUDING rows that were
    /// skipped as unreadable. Callers advance their cursor to it, so a batch
    /// made only of unreadable rows can never stall the relay.
    public struct TailScan: Equatable {
        public var rows: [RawMessage]
        public var scannedMaxID: Int64
        public var unreadable: Int
    }

    /// Tail rows with `ROWID > cursor`, at most `limit` scanned, in id order.
    ///
    /// Only `item_type = 0` rows are considered (tapbacks, media and system
    /// rows are skipped). The body is `text` when present, otherwise the text
    /// decoded from `attributedBody` (see `AttributedBodyDecoder`).
    /// A row with no readable body, or a NULL guid / peer / service, is
    /// skipped and counted in `unreadable`; it never aborts the scan.
    public func scan(afterCursor cursor: Int64, limit: Int) throws -> TailScan {
        guard let db else { throw StoreError.openFailed("store not opened") }
        let body = try messageHasColumn("attributedBody")
        let sql = """
            SELECT m.ROWID, m.guid, h.id, m.is_from_me, m.text, m.service, m.date,
                   \(body ? "m.attributedBody" : "NULL")
              FROM message m
              JOIN handle h ON m.handle_id = h.ROWID
             WHERE m.ROWID > ?
               AND m.item_type = 0
             ORDER BY m.ROWID ASC
             LIMIT ?
        """
        var stmt: OpaquePointer?
        guard sqlite3_prepare_v2(db, sql, -1, &stmt, nil) == SQLITE_OK else {
            throw StoreError.openFailed(String(cString: sqlite3_errmsg(db)))
        }
        defer { sqlite3_finalize(stmt) }
        sqlite3_bind_int64(stmt, 1, cursor)
        sqlite3_bind_int(stmt, 2, Int32(limit))

        var out: [RawMessage] = []
        var scannedMax = cursor
        var unreadable = 0
        while sqlite3_step(stmt) == SQLITE_ROW {
            let id = sqlite3_column_int64(stmt, 0)
            scannedMax = max(scannedMax, id)
            let guid = ncCStr(sqlite3_column_text(stmt, 1))
            let peer = ncCStr(sqlite3_column_text(stmt, 2))
            let isFromMe = sqlite3_column_int(stmt, 3)
            let service = ncCStr(sqlite3_column_text(stmt, 5))
            let dateNanos = sqlite3_column_int64(stmt, 6)
            var bodyText = ncCStr(sqlite3_column_text(stmt, 4)) ?? ""
            if bodyText.isEmpty, sqlite3_column_type(stmt, 7) == SQLITE_BLOB {
                let n = Int(sqlite3_column_bytes(stmt, 7))
                if n > 0, let ptr = sqlite3_column_blob(stmt, 7) {
                    bodyText = AttributedBodyDecoder.decode(Data(bytes: ptr, count: n)) ?? ""
                }
            }
            guard let guid, let peer, let service, !bodyText.isEmpty else {
                unreadable += 1
                continue
            }
            out.append(RawMessage(
                id: id,
                guid: guid,
                peerIdentifier: peer,
                isFromMe: Int32(isFromMe),
                body: bodyText,
                service: service,
                dateNanos: dateNanos
            ))
        }
        return TailScan(rows: out, scannedMaxID: scannedMax, unreadable: unreadable)
    }

    /// Readable rows only. Prefer `scan`, which also reports how far it
    /// got; this form is kept for callers that only need the rows.
    public func tail(afterCursor cursor: Int64, limit: Int) throws -> [RawMessage] {
        try scan(afterCursor: cursor, limit: limit).rows
    }

    /// Whether `message` has a column of this name (case-insensitive).
    func messageHasColumn(_ name: String) throws -> Bool {
        guard let db else { throw StoreError.openFailed("store not opened") }
        var stmt: OpaquePointer?
        guard sqlite3_prepare_v2(db, "PRAGMA table_info(message)", -1, &stmt, nil) == SQLITE_OK,
              let stmt else { throw StoreError.schemaIncompatible("cannot introspect table message") }
        defer { sqlite3_finalize(stmt) }
        while sqlite3_step(stmt) == SQLITE_ROW {
            if let n = sqlite3_column_text(stmt, 1).flatMap({ String(cString: $0) }),
               n.caseInsensitiveCompare(name) == .orderedSame { return true }
        }
        return false
    }

    /// Counts-only: of the rows whose text lives only in `attributedBody`,
    /// how many the decoder can read. The text is decoded in memory and
    /// discarded; only the two counts leave this function.
    public func bodyDecodeStats() throws -> (decodable: Int64, undecodable: Int64) {
        guard let db else { throw StoreError.openFailed("store not opened") }
        guard try messageHasColumn("attributedBody") else { return (0, 0) }
        let sql = """
            SELECT attributedBody FROM message
             WHERE item_type = 0 AND (text IS NULL OR text = '') AND attributedBody IS NOT NULL
        """
        var stmt: OpaquePointer?
        guard sqlite3_prepare_v2(db, sql, -1, &stmt, nil) == SQLITE_OK, let stmt
        else { throw StoreError.openFailed(String(cString: sqlite3_errmsg(db))) }
        defer { sqlite3_finalize(stmt) }
        var ok: Int64 = 0
        var bad: Int64 = 0
        while sqlite3_step(stmt) == SQLITE_ROW {
            let n = Int(sqlite3_column_bytes(stmt, 0))
            if n > 0, let ptr = sqlite3_column_blob(stmt, 0),
               AttributedBodyDecoder.decode(Data(bytes: ptr, count: n)) != nil {
                ok += 1
            } else {
                bad += 1
            }
        }
        return (ok, bad)
    }

    /// Counts-only description of the store's shape, for `status`. Never
    /// selects message content: only COUNTs of NULL/non-NULL predicates and
    /// the digit length of the newest `date` (to tell seconds from
    /// nanoseconds). `bodyOnly` is rows with no `text` but an
    /// `attributedBody` blob (how recent macOS stores most message text).
    public struct Shape: Equatable {
        public var textRows: Int64
        public var bodyOnlyRows: Int64
        public var emptyRows: Int64
        public var hasAttributedBody: Bool
        public var newestDateDigits: Int
    }

    public func shape() throws -> Shape {
        guard let db else { throw StoreError.openFailed("store not opened") }
        let body = try messageHasColumn("attributedBody")
        let sql = """
            SELECT
              COALESCE(SUM(text IS NOT NULL AND text <> ''), 0),
              \(body ? "COALESCE(SUM((text IS NULL OR text = '') AND attributedBody IS NOT NULL), 0)" : "0"),
              COALESCE(SUM((text IS NULL OR text = '') \(body ? "AND attributedBody IS NULL" : "")), 0),
              COALESCE(LENGTH(CAST(MAX(date) AS TEXT)), 0)
              FROM message WHERE item_type = 0
        """
        var stmt: OpaquePointer?
        guard sqlite3_prepare_v2(db, sql, -1, &stmt, nil) == SQLITE_OK, let stmt
        else { throw StoreError.openFailed(String(cString: sqlite3_errmsg(db))) }
        defer { sqlite3_finalize(stmt) }
        guard sqlite3_step(stmt) == SQLITE_ROW else {
            return Shape(textRows: 0, bodyOnlyRows: 0, emptyRows: 0,
                         hasAttributedBody: body, newestDateDigits: 0)
        }
        return Shape(
            textRows: sqlite3_column_int64(stmt, 0),
            bodyOnlyRows: sqlite3_column_int64(stmt, 1),
            emptyRows: sqlite3_column_int64(stmt, 2),
            hasAttributedBody: body,
            newestDateDigits: Int(sqlite3_column_int64(stmt, 3))
        )
    }

    /// The highest `id` currently in the store — used by `init` to seed
    /// the outbox cursor without re-sending history on first run.
    public func maxID() throws -> Int64 {
        guard let db else { throw StoreError.openFailed("store not opened") }
        var stmt: OpaquePointer?
        guard sqlite3_prepare_v2(db, "SELECT COALESCE(MAX(ROWID), 0) FROM message", -1, &stmt, nil) == SQLITE_OK,
              let stmt
        else { throw StoreError.openFailed(String(cString: sqlite3_errmsg(db))) }
        defer { sqlite3_finalize(stmt) }
        guard sqlite3_step(stmt) == SQLITE_ROW else { return 0 }
        return sqlite3_column_int64(stmt, 0)
    }
}

