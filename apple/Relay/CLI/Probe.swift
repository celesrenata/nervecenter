import Foundation
import SQLite3

/// `probe` — read-only discovery of the Phone app's voicemail and the
/// Contacts stores, run under the relay's Full Disk Access.
///
/// Prints ONLY structure: paths (home shortened to `~`), file counts by
/// extension, SQLite table names with row counts, and column names. Never
/// prints a row value, a file name inside a store, or any content. Opens
/// every database `SQLITE_OPEN_READONLY`.
///
/// The voicemail store itself is under `group.com.apple.FaceTime`
/// (`com.apple.facetimemessagestored/Data Store/FaceTimeMessageStore-local.sqlitedb`);
/// the relay reads it with `VoicemailStoreReader`, and `voicemail-scan` prints
/// counts for it.
enum Probe {
    static let roots = [
        "~/Library/Application Support/CallHistoryDB",
        "~/Library/Group Containers/group.com.apple.telephonyutilities.callservicesd",
        "~/Library/Containers/com.apple.mobilephone",
        "~/Library/Application Support/AddressBook",
        "~/Library/Group Containers/group.com.apple.FaceTime",
    ]
    static let dbExtensions: Set<String> = ["db", "sqlite", "sqlitedb", "storedata", "abcddb"]

    static func short(_ path: String) -> String {
        path.replacingOccurrences(of: NSHomeDirectory(), with: "~")
    }

    /// Directories under ~/Library (depth <= 4) whose name mentions voicemail.
    static func findVoicemailDirs() {
        let fm = FileManager.default
        let lib = NSString(string: "~/Library").expandingTildeInPath
        print("== voicemail-named folders under ~/Library")
        func walk(_ dir: String, _ depth: Int) {
            guard depth <= 4, let items = try? fm.contentsOfDirectory(atPath: dir) else { return }
            for name in items {
                let full = (dir as NSString).appendingPathComponent(name)
                var isDir: ObjCBool = false
                guard fm.fileExists(atPath: full, isDirectory: &isDir), isDir.boolValue else { continue }
                if name.lowercased().contains("voicemail") {
                    var exts: [String: Int] = [:]
                    var dbs: [String] = []
                    if let e = fm.enumerator(atPath: full) {
                        while let rel = e.nextObject() as? String {
                            let x = (rel as NSString).pathExtension.lowercased()
                            exts[x.isEmpty ? "(none)" : x, default: 0] += 1
                            if dbExtensions.contains(x) { dbs.append((full as NSString).appendingPathComponent(rel)) }
                        }
                    }
                    print("   \(short(full)): \(exts.sorted { $0.key < $1.key })")
                    for db in dbs.sorted() { describe(db) }
                } else if !name.hasPrefix(".") {
                    walk(full, depth + 1)
                }
            }
        }
        walk(lib, 1)
    }

    /// Folder paths, sizes and ages of audio in the call-services container,
    /// plus the top-level keys of its plist. No file names, no values.
    static func describeCallAudio() {
        let fm = FileManager.default
        let root = NSString(string: "~/Library/Group Containers/group.com.apple.telephonyutilities.callservicesd").expandingTildeInPath
        print("== call-services audio")
        guard let e = fm.enumerator(atPath: root) else { print("   (not readable)"); return }
        var byDir: [String: (Int, Int64, Date?, Date?)] = [:]
        while let rel = e.nextObject() as? String {
            let full = (root as NSString).appendingPathComponent(rel)
            let ext = (rel as NSString).pathExtension.lowercased()
            if ext == "plist", let d = NSDictionary(contentsOfFile: full) {
                print("   plist keys: \((d.allKeys as? [String] ?? []).sorted())")
            }
            guard ["caf", "m4a", "amr", "wav"].contains(ext) else { continue }
            let dir = (rel as NSString).deletingLastPathComponent
            let attrs = (try? fm.attributesOfItem(atPath: full)) ?? [:]
            let size = (attrs[.size] as? NSNumber)?.int64Value ?? 0
            let date = attrs[.modificationDate] as? Date
            var cur = byDir[dir] ?? (0, 0, nil, nil)
            cur.0 += 1; cur.1 += size
            if let date { cur.2 = min(cur.2 ?? date, date); cur.3 = max(cur.3 ?? date, date) }
            byDir[dir] = cur
        }
        let f = ISO8601DateFormatter()
        for (dir, v) in byDir.sorted(by: { $0.key < $1.key }) {
            print("   dir '\(dir)': \(v.0) files, \(v.1 / 1024) KiB, oldest \(v.2.map(f.string) ?? "-"), newest \(v.3.map(f.string) ?? "-")")
        }
    }

    static func run() {
        let fm = FileManager.default
        for root in roots {
            let path = NSString(string: root).expandingTildeInPath
            print("== \(root)")
            guard let walker = fm.enumerator(atPath: path) else {
                print("   (not readable or missing)")
                continue
            }
            var extCounts: [String: Int] = [:]
            var databases: [String] = []
            var dirs: Set<String> = []
            while let rel = walker.nextObject() as? String {
                let full = (path as NSString).appendingPathComponent(rel)
                var isDir: ObjCBool = false
                fm.fileExists(atPath: full, isDirectory: &isDir)
                if isDir.boolValue {
                    if rel.split(separator: "/").count <= 3 { dirs.insert(rel) }
                    continue
                }
                let ext = (rel as NSString).pathExtension.lowercased()
                extCounts[ext.isEmpty ? "(none)" : ext, default: 0] += 1
                if dbExtensions.contains(ext) { databases.append(full) }
            }
            print("   files by extension: \(extCounts.sorted { $0.key < $1.key })")
            let voiceDirs = dirs.filter { $0.lowercased().contains("voice") }.sorted()
            if !voiceDirs.isEmpty { print("   voice-named dirs: \(voiceDirs)") }
            for db in databases.sorted() { describe(db) }
        }
    }

    static func describe(_ path: String) {
        var handle: OpaquePointer?
        let uri = "file:\(path)?mode=ro"
        guard sqlite3_open_v2(uri, &handle, SQLITE_OPEN_READONLY | SQLITE_OPEN_URI, nil) == SQLITE_OK,
              let db = handle else {
            print("   db \(short(path)): open failed")
            sqlite3_close(handle)
            return
        }
        defer { sqlite3_close(db) }
        let tables = strings(db, "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        guard let tables else {
            print("   db \(short(path)): unreadable (\(String(cString: sqlite3_errmsg(db))))")
            return
        }
        print("   db \(short(path)): \(tables.count) tables")
        for table in tables where !table.hasPrefix("sqlite_") {
            let quoted = "\"" + table.replacingOccurrences(of: "\"", with: "\"\"") + "\""
            let count = strings(db, "SELECT count(*) FROM \(quoted)")?.first ?? "?"
            let interesting = ["voice", "transcri", "message", "asset", "record"]
            let columns = strings(db, "SELECT name FROM pragma_table_info('\(table.replacingOccurrences(of: "'", with: "''"))')") ?? []
            let show = interesting.contains { table.lowercased().contains($0) }
            print("     \(table): \(count) rows" + (show ? " cols=\(columns)" : ""))
        }
        if tables.contains("ZSTOREDMESSAGE") {
            // Counts per (message type, file type): transcripts present, deleted, unread.
            // Type 1 is voicemail (.amr); type 0 holds video/audio messages.
            let sql = "SELECT ZMESSAGETYPE || '/' || ZFILETYPE || ': ' || count(*) || ' msgs, ' || sum(ZTRANSCRIPT IS NOT NULL) || ' transcript, ' || sum(ZTRANSCRIPTDATA IS NOT NULL) || ' transcriptdata, ' || sum(ZDATEDELETED IS NOT NULL) || ' deleted, ' || sum(ZISREAD = 0) || ' unread, ' || sum(ZFROM IS NOT NULL) || ' with-from' FROM ZSTOREDMESSAGE GROUP BY ZMESSAGETYPE, ZFILETYPE"
            for line in strings(db, sql) ?? ["(query failed)"] { print("       type/file \(line)") }
        }
    }

    /// First-column strings of a query, or nil when it fails.
    static func strings(_ db: OpaquePointer, _ sql: String) -> [String]? {
        var stmt: OpaquePointer?
        guard sqlite3_prepare_v2(db, sql, -1, &stmt, nil) == SQLITE_OK else { return nil }
        defer { sqlite3_finalize(stmt) }
        var out: [String] = []
        while sqlite3_step(stmt) == SQLITE_ROW {
            if let c = sqlite3_column_text(stmt, 0) { out.append(String(cString: c)) }
            else { out.append(String(sqlite3_column_int64(stmt, 0))) }
        }
        return out
    }
}
