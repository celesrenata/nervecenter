import Foundation
import SQLite3

/// `SQLITE_TRANSIENT` — SQLite copies the bound string (the C constant
/// is a function pointer and has no Swift symbol).
let kSQLITE_TRANSIENT = unsafeBitCast(-1, to: sqlite3_destructor_type.self)

/// A pending outbound message: the normalized payload as JSON plus a
/// stable `seq` so replay is always in the same order it was captured
/// (prompt2 §48: "Ordering and original timestamps must be preserved").
public struct OutboxEntry: Sendable, Equatable {
    public let seq: Int64
    public let storeID: Int64
    public let payloadJSON: String
}

public enum OutboxError: Error, Equatable {
    case notOpened
    case openFailed(String)
    case execFailed(String)
}

/// Durable outbox behind a local SQLite file.
///
/// prompt2 §48 (Offline Behavior):
/// - When NerveCenter is unavailable the relay **queues** normalized
///   events here; when connectivity returns, events **replay** in `seq`
///   order.
/// - The backend returns `200` for a replayed duplicate `message_id`
///   (vs `201` for new), so replay is idempotent end-to-end.
///
/// The outbox is the only file the relay ever writes to — the Messages
/// store is opened read-only (§47 "never modify the Messages database",
/// "never expose Messages storage remotely").
public final class Outbox {
    public let url: URL
    private var db: OpaquePointer?

    public init(url: URL) {
        self.url = url
    }

    deinit {
        close()
    }

    /// Open (creating parent dirs) and ensure the schema. Idempotent.
    @discardableResult
    public func open() throws -> Int {
        if db != nil { return try count() }
        try FileManager.default.createDirectory(
            at: url.deletingLastPathComponent(),
            withIntermediateDirectories: true
        )
        var handle: OpaquePointer?
        guard sqlite3_open_v2(
            url.path, &handle, SQLITE_OPEN_READWRITE | SQLITE_OPEN_CREATE | SQLITE_OPEN_NOMUTEX, nil
        ) == SQLITE_OK, let handle
        else {
            throw OutboxError.openFailed(String(cString: sqlite3_errmsg(handle)))
        }
        self.db = handle
        let ddl = """
            CREATE TABLE IF NOT EXISTS outbox (
                seq        INTEGER PRIMARY KEY AUTOINCREMENT,
                store_id   INTEGER NOT NULL,
                payload    TEXT    NOT NULL,
                created_at TEXT    NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_outbox_seq ON outbox(seq);
            CREATE TABLE IF NOT EXISTS meta (
                key   TEXT PRIMARY KEY,
                value INTEGER NOT NULL
            );
            -- §70 local-first: delivered rows are archived, never dropped.
            -- Pruning (`prune`) may trim this table by config bounds only.
            CREATE TABLE IF NOT EXISTS delivered (
                seq        INTEGER NOT NULL,
                store_id   INTEGER NOT NULL,
                payload    TEXT    NOT NULL,
                created_at TEXT    NOT NULL,
                acked_at   TEXT    NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_delivered_ack ON delivered(acked_at, seq);
            -- Local idempotency index: stable record ids already handled
            -- (queued or archived) by this channel's outbox.
            CREATE TABLE IF NOT EXISTS seen (
                id   TEXT PRIMARY KEY,
                at   TEXT NOT NULL
            );
        """
        _ = try execRaw(handle, ddl)
        // Migration: pre-cursor outboxes have the table but no row.
        var hasCursorRow = false
        if let value = try? scalar("SELECT value FROM meta WHERE key='cursor'"), value > 0 {
            hasCursorRow = true
        }
        if !hasCursorRow {
            _ = try? execRaw(handle,
                "INSERT OR IGNORE INTO meta (key, value) VALUES ('cursor', 0)")
        }
        return try count()
    }

    /// Single-row scalar read (0 for NULL/missing).
    private func scalar(_ sql: String) throws -> Int64 {
        guard let db else { return 0 }
        var stmt: OpaquePointer?
        guard sqlite3_prepare_v2(db, sql, -1, &stmt, nil) == SQLITE_OK, let stmt
        else { return 0 }
        defer { sqlite3_finalize(stmt) }
        guard sqlite3_step(stmt) == SQLITE_ROW else { return 0 }
        return sqlite3_column_int64(stmt, 0)
    }

    public func close() {
        if let db { sqlite3_close(db); self.db = nil }
    }

    /// Insert normalized payloads (in order). Returns the new `seq`s.
    /// `store_id` is the chat.db row id — used to advance the store
    /// cursor after a successful send batch.
    @discardableResult
    public func enqueue(storeID: Int64, payloads: [String]) throws -> [Int64] {
        guard let db else { throw OutboxError.notOpened }
        guard !payloads.isEmpty else { return [] }
        _ = try execRaw(db, "BEGIN IMMEDIATE")
        do {
            var stmt: OpaquePointer?
            guard sqlite3_prepare_v2(
                db,
                "INSERT INTO outbox (store_id, payload, created_at) VALUES (?, ?, ?)",
                -1, &stmt, nil
            ) == SQLITE_OK, let stmt
            else { throw OutboxError.execFailed(String(cString: sqlite3_errmsg(db))) }

            var seqs: [Int64] = []
            for payload in payloads {
                sqlite3_reset(stmt)
                sqlite3_clear_bindings(stmt)
                sqlite3_bind_int64(stmt, 1, storeID)
                sqlite3_bind_text(stmt, 2, payload, -1, kSQLITE_TRANSIENT)
                sqlite3_bind_text(
                    stmt, 3,
                    ISO8601DateFormatter.iso8601Second.string(from: Date()),
                    -1, kSQLITE_TRANSIENT
                )
                guard sqlite3_step(stmt) == SQLITE_DONE else {
                    throw OutboxError.execFailed(String(cString: sqlite3_errmsg(db)))
                }
                seqs.append(sqlite3_last_insert_rowid(db))
            }
            sqlite3_finalize(stmt)
            // Advance the capture cursor in the same transaction so it
            // cannot lag behind the rows it covers (§48: no re-capture).
            try setCursor(storeID)
            _ = try execRaw(db, "COMMIT")
            return seqs
        } catch {
            _ = try? execRaw(db, "ROLLBACK")
            throw error
        }
    }

    /// The next unacked batch, oldest first (FIFO — ordering contract §48).
    /// Only *undelivered* rows: delivery moves a row to the local archive
    /// (`delivered`, §70 local-first retention) instead of dropping it.
    public func pending(limit: Int) throws -> [OutboxEntry] {
        guard let db else { throw OutboxError.notOpened }
        var stmt: OpaquePointer?
        guard sqlite3_prepare_v2(
            db, "SELECT seq, store_id, payload FROM outbox ORDER BY seq ASC LIMIT ?", -1, &stmt, nil
        ) == SQLITE_OK, let stmt
        else { throw OutboxError.execFailed(String(cString: sqlite3_errmsg(db))) }
        defer { sqlite3_finalize(stmt) }
        sqlite3_bind_int(stmt, 1, Int32(limit))

        var out: [OutboxEntry] = []
        while sqlite3_step(stmt) == SQLITE_ROW {
            guard let payload = ncCStr(sqlite3_column_text(stmt, 2)) else { continue }
            out.append(OutboxEntry(
                seq: sqlite3_column_int64(stmt, 0),
                storeID: sqlite3_column_int64(stmt, 1),
                payloadJSON: payload
            ))
        }
        return out
    }

    /// Archive successfully-delivered entries. `seqs` are the acked rows.
    ///
    /// §70 local-first: acking **moves** each row from `outbox` into the
    /// local `delivered` archive inside one transaction — the local history
    /// is never deleted on send, and a crash mid-ack leaves the row pending
    /// (replayed), not lost. Bounded retention trims `delivered` later via
    /// `prune(...)`; the pending `outbox` rows are never pruned.
    @discardableResult
    public func ack(_ seqs: [Int64]) throws -> Int {
        guard let db else { throw OutboxError.notOpened }
        guard !seqs.isEmpty else { return 0 }
        _ = try execRaw(db, "BEGIN IMMEDIATE")
        do {
            var select: OpaquePointer?
            guard sqlite3_prepare_v2(
                db, "SELECT seq, store_id, payload, created_at FROM outbox WHERE seq = ?",
                -1, &select, nil
            ) == SQLITE_OK, let select else {
                throw OutboxError.execFailed(String(cString: sqlite3_errmsg(db)))
            }
            var move: OpaquePointer?
            guard sqlite3_prepare_v2(
                db,
                "INSERT INTO delivered (seq, store_id, payload, created_at, acked_at) " +
                    "VALUES (?, ?, ?, ?, ?)",
                -1, &move, nil
            ) == SQLITE_OK, let move else {
                throw OutboxError.execFailed(String(cString: sqlite3_errmsg(db)))
            }
            var del: OpaquePointer?
            guard sqlite3_prepare_v2(db, "DELETE FROM outbox WHERE seq = ?", -1, &del, nil) == SQLITE_OK,
                  let del
            else { throw OutboxError.execFailed(String(cString: sqlite3_errmsg(db))) }

            var moved = 0
            let now = ISO8601DateFormatter.iso8601Second.string(from: Date())
            for seq in seqs {
                sqlite3_reset(select)
                sqlite3_clear_bindings(select)
                sqlite3_bind_int64(select, 1, seq)
                guard sqlite3_step(select) == SQLITE_ROW else {
                    // Not pending (already archived) — idempotent no-op.
                    continue
                }
                sqlite3_reset(move)
                sqlite3_clear_bindings(move)
                sqlite3_bind_int64(move, 1, seq)
                sqlite3_bind_int64(move, 2, sqlite3_column_int64(select, 1))
                if let payload = ncCStr(sqlite3_column_text(select, 2)) {
                    sqlite3_bind_text(move, 3, payload, -1, kSQLITE_TRANSIENT)
                }
                if let created = ncCStr(sqlite3_column_text(select, 3)) {
                    sqlite3_bind_text(move, 4, created, -1, kSQLITE_TRANSIENT)
                }
                sqlite3_bind_text(move, 5, now, -1, kSQLITE_TRANSIENT)
                guard sqlite3_step(move) == SQLITE_DONE else {
                    throw OutboxError.execFailed(String(cString: sqlite3_errmsg(db)))
                }
                sqlite3_reset(del)
                sqlite3_clear_bindings(del)
                sqlite3_bind_int64(del, 1, seq)
                guard sqlite3_step(del) == SQLITE_DONE else {
                    throw OutboxError.execFailed(String(cString: sqlite3_errmsg(db)))
                }
                moved += 1
            }
            sqlite3_finalize(select)
            sqlite3_finalize(move)
            sqlite3_finalize(del)
            _ = try execRaw(db, "COMMIT")
            return moved
        } catch {
            _ = try? execRaw(db, "ROLLBACK")
            throw error
        }
    }

    /// The highest `store_id` currently queued. The engine uses this as
    /// the capture cursor: rows up to and including it are already in
    /// the queue (sent or not), so the store tail starts after it.
    public func maxStoreID() throws -> Int64 {
        guard let db else { return 0 }
        var stmt: OpaquePointer?
        guard sqlite3_prepare_v2(
            db, "SELECT COALESCE(MAX(store_id), 0) FROM outbox", -1, &stmt, nil
        ) == SQLITE_OK, let stmt
        else { throw OutboxError.execFailed(String(cString: sqlite3_errmsg(db))) }
        defer { sqlite3_finalize(stmt) }
        guard sqlite3_step(stmt) == SQLITE_ROW else { return 0 }
        return sqlite3_column_int64(stmt, 0)
    }

    public func count() throws -> Int {
        guard let db else { return 0 }
        var stmt: OpaquePointer?
        guard sqlite3_prepare_v2(db, "SELECT COUNT(*) FROM outbox", -1, &stmt, nil) == SQLITE_OK,
              let stmt
        else { throw OutboxError.execFailed(String(cString: sqlite3_errmsg(db))) }
        defer { sqlite3_finalize(stmt) }
        guard sqlite3_step(stmt) == SQLITE_ROW else { return 0 }
        return Int(sqlite3_column_int64(stmt, 0))
    }

    /// Rows in the local `delivered` archive (§70 local-first history).
    public func deliveredCount() throws -> Int {
        guard let db else { return 0 }
        var stmt: OpaquePointer?
        guard sqlite3_prepare_v2(db, "SELECT COUNT(*) FROM delivered", -1, &stmt, nil) == SQLITE_OK,
              let stmt
        else { throw OutboxError.execFailed(String(cString: sqlite3_errmsg(db))) }
        defer { sqlite3_finalize(stmt) }
        guard sqlite3_step(stmt) == SQLITE_ROW else { return 0 }
        return Int(sqlite3_column_int64(stmt, 0))
    }

    /// Local idempotency: was this stable record id already handled
    /// (queued or archived) by this outbox?
    public func seen(_ id: String) -> Bool {
        guard let db else { return false }
        var stmt: OpaquePointer?
        guard sqlite3_prepare_v2(db, "SELECT 1 FROM seen WHERE id = ?", -1, &stmt, nil) == SQLITE_OK,
              let stmt
        else { return false }
        defer { sqlite3_finalize(stmt) }
        sqlite3_bind_text(stmt, 1, id, -1, kSQLITE_TRANSIENT)
        return sqlite3_step(stmt) == SQLITE_ROW
    }

    /// Remember a stable record id. Called after a successful enqueue so a
    /// source that re-yields the record between polls is not re-captured.
    public func markSeen(_ id: String) throws {
        guard let db else { throw OutboxError.notOpened }
        guard !id.isEmpty else { return }
        var stmt: OpaquePointer?
        guard sqlite3_prepare_v2(
            db, "INSERT OR IGNORE INTO seen (id, at) VALUES (?, ?)", -1, &stmt, nil
        ) == SQLITE_OK, let stmt
        else { throw OutboxError.execFailed(String(cString: sqlite3_errmsg(db))) }
        defer { sqlite3_finalize(stmt) }
        sqlite3_bind_text(stmt, 1, id, -1, kSQLITE_TRANSIENT)
        sqlite3_bind_text(
            stmt, 2,
            ISO8601DateFormatter.iso8601Second.string(from: Date()),
            -1, kSQLITE_TRANSIENT
        )
        guard sqlite3_step(stmt) == SQLITE_DONE else {
            throw OutboxError.execFailed(String(cString: sqlite3_errmsg(db)))
        }
    }

    /// §70 bounded local retention — trims the `delivered` archive only.
    ///
    /// Pending rows in `outbox` are **never** touched, so a transport
    /// failure can never cost local data. Keeps at most `maxEntries`
    /// delivered rows (0 = no entry cap) and drops delivered rows acked
    /// more than `retentionDays` before `now` (0 = no age cap). Returns
    /// the number of rows pruned.
    @discardableResult
    public func prune(maxEntries: Int, retentionDays: Int, now: Date = Date()) throws -> Int {
        guard let db else { throw OutboxError.notOpened }
        guard maxEntries > 0 || retentionDays > 0 else { return 0 }
        var pruned = 0
        if retentionDays > 0 {
            let cutoff = ISO8601DateFormatter.iso8601Second.string(
                from: Date(timeIntervalSince1970:
                    now.timeIntervalSince1970 - TimeInterval(retentionDays) * 86_400)
            )
            let before = try deliveredCount()
            _ = try execRaw(db, "DELETE FROM delivered WHERE acked_at <= '\(cutoff)'")
            let after = try deliveredCount()
            pruned += max(0, before - after)
        }
        if maxEntries > 0 {
            let total = try deliveredCount()
            let overflow = total - maxEntries
            if overflow > 0 {
                // Oldest-first: the newest `maxEntries` rows survive.
                _ = try execRaw(db, """
                    DELETE FROM delivered
                    WHERE seq IN (
                        SELECT seq FROM delivered
                        ORDER BY acked_at ASC, seq ASC
                        LIMIT \(overflow)
                    )
                    """)
                let after = try deliveredCount()
                pruned += max(0, total - after)
            }
        }
        return pruned
    }

    /// The persisted capture cursor: the highest chat.db `id` the engine
    /// has attempted to capture. Survives an empty outbox (unlike
    /// `maxStoreID()`, which is 0 once everything is acked), so rows that
    /// arrive while the queue is empty are never skipped. 0 = first run.
    public func cursor() throws -> Int64 {
        try scalar("SELECT value FROM meta WHERE key='cursor'")
    }

    /// Advance the capture cursor (never backwards).
    public func setCursor(_ id: Int64) throws {
        guard let db else { throw OutboxError.notOpened }
        guard id > 0 else { return }
        let sql = """
            INSERT INTO meta (key, value) VALUES ('cursor', ?)
            ON CONFLICT(key) DO UPDATE SET value = MAX(value, excluded.value)
        """
        var stmt: OpaquePointer?
        guard sqlite3_prepare_v2(db, sql, -1, &stmt, nil) == SQLITE_OK, let stmt
        else { throw OutboxError.execFailed(String(cString: sqlite3_errmsg(db))) }
        defer { sqlite3_finalize(stmt) }
        sqlite3_bind_int64(stmt, 1, id)
        guard sqlite3_step(stmt) == SQLITE_DONE else {
            throw OutboxError.execFailed(String(cString: sqlite3_errmsg(db)))
        }
    }

    private func execRaw(_ db: OpaquePointer, _ sql: String) throws -> Bool {
        var errPtr: UnsafeMutablePointer<CChar>?
        let rc = sqlite3_exec(db, sql, nil, nil, &errPtr)
        if rc != SQLITE_OK {
            var msg = "sqlite3_exec failed (rc=\(rc))"
            if let errPtr {
                msg = String(cString: errPtr)
                sqlite3_free(errPtr)
            }
            throw OutboxError.execFailed(msg)
        }
        return true
    }
}
