import Foundation
import NerveRelayCore
import SQLite3

/// `nerve-personal-relay` — macOS Personal Relay CLI (prompt2 §46).
///
/// Subcommands:
///   init-config [path]   write a template config (default ~/.nervcenter/relay.conf)
///   status               open store + outbox, report schema/cursor/backlog
///   run                  loop: capture + flush every `interval_seconds`
///                        (Messages first, then each *enabled* D5' channel —
///                        email/voicemail/calendar/contacts)
///   run --once           a single capture + flush cycle (then exit)
///   install              render + load the LaunchAgent plist
///                        (`com.nervcenter.relay`, gui/$UID)
///   uninstall            bootout + remove the plist (idempotent)
///   pair                 POST /api/relay/pair → store token in the secure
///                        credential store → POST /api/relay/register
///   health               server health (GET /api/relay/health) + local
///                        diagnostics (binary, LaunchAgent, Full Disk Access)
///   import --consent     initial history import — REFUSED without the
///                        explicit --consent flag; posts stable-id batches
///                        to POST /api/relay/messages; the incremental loop
///                        (durable outbox cursor) then continues as before
///
/// All output goes to stdout/stderr so the LaunchAgent can tee it to a
/// log file (§47: observable, user-account scoped, no remote shell).

enum Exit: Int {
    case ok = 0
    case usage = 2
    case config = 3
    case refused = 4      // schema refused / store missing — fail closed
    case transport = 5    // NerveCenter unreachable (outbox keeps data)
    case pairing = 6      // pair/register/import-request failure
}

func log(_ line: String) {
    let ts = ISO8601DateFormatter.iso8601Second.string(from: Date())
    FileHandle.standardOutput.write(Data(("[\(ts)] \(line)\n".utf8)))
}

func fail(_ code: Exit, _ message: String) -> Never {
    FileHandle.standardError.write(Data(("[relay] \(message)\n".utf8)))
    exit(Int32(code.rawValue))
}

let args = CommandLine.arguments.dropFirst().map { $0 }

// -- installation + pairing constants (spec personal-ops-recovery §5) --------

let kAgentLabel = "com.nervcenter.relay"
let kPlistPath = "~/Library/LaunchAgents/com.nervcenter.relay.plist"
let kLogsDir = "~/Library/Logs/nervcenter"
let kLogsOut = "~/Library/Logs/nervcenter/relay.out.log"
let kLogsErr = "~/Library/Logs/nervcenter/relay.err.log"
let kIdentityFile = "~/Library/Application Support/NerveCenterRelay/identity.json"
let kImportCursorFile = "~/Library/Application Support/NerveCenterRelay/import-cursor.json"

// -- identity + credential helpers ---------------------------------------------
//
// The pairing token from POST /api/relay/pair is returned exactly once.
// `pair` prints it one time and stores it in the secure credential store;
// every other subcommand reports only "present"/"missing" — it is never
// echoed again, logged, or written to the config file.

struct RelayIdentity: Codable {
    var deviceID: String
    var connectionID: String
    var pairedAt: String
    var baseURL: String
}

func identityPath() -> URL {
    RelayConfig.expand(kIdentityFile)
}

func loadIdentity() throws -> RelayIdentity? {
    let path = identityPath()
    guard FileManager.default.fileExists(atPath: path.path) else { return nil }
    return try JSONDecoder().decode(RelayIdentity.self, from: Data(contentsOf: path))
}

func saveIdentity(_ id: RelayIdentity) throws {
    let path = identityPath()
    try FileManager.default.createDirectory(
        at: path.deletingLastPathComponent(), withIntermediateDirectories: true)
    try JSONEncoder().encode(id).write(to: path, options: [.atomic])
    try? FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: path.path)
}

func storeToken(_ token: String, deviceID: String) throws {
    let store = defaultCredentialStore()
    try store.storeCredential(deviceID: deviceID, value: token, expiresAt: .distantFuture)
}

func loadStoredToken(deviceID: String) -> String? {
    let store = defaultCredentialStore()
    return (try? store.retrieveCredential(deviceID: deviceID)) ?? nil
}

// -- plist template (shipped in Distribution/, resolved at run time) ----------

func plistTemplatePath() -> URL? {
    let fm = FileManager.default
    let candidates = [
        URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .appendingPathComponent("Distribution/relay_launchagent.plist.template"),
        URL(fileURLWithPath: CommandLine.arguments[0])
            .deletingLastPathComponent()
            .appendingPathComponent("com.nervcenter.relay.plist.template"),
    ]
    for url in candidates where fm.fileExists(atPath: url.path) {
        return url
    }
    return nil
}

/// Inline fallback LaunchAgent template.
///
/// Key-for-key in sync with the shipped
/// `Distribution/relay_launchagent.plist.template` (enforced by
/// `tests/apple/test_relay_launchagent.py`) so a missing template file
/// cannot silently change the agent's behavior.
func inlinePlistTemplate() -> String {
    """
    <?xml version="1.0" encoding="UTF-8"?>
    <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
      "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
    <plist version="1.0">
    <dict>
        <key>Label</key>
        <string>com.nervcenter.relay</string>
        <key>ProgramArguments</key>
        <array>
            <string>{{BINARY_PATH}}</string>
            <string>run</string>
        </array>
        <key>RunAtLoad</key>
        <true/>
        <key>KeepAlive</key>
        <true/>
        <key>ProcessType</key>
        <string>Background</string>
        <key>StandardOutPath</key>
        <string>{{STANDARD_OUT}}</string>
        <key>StandardErrorPath</key>
        <string>{{STANDARD_ERROR}}</string>
        <key>StateDir</key>
        <string>{{LOGS_DIR}}</string>
        <key>ThrottleInterval</key>
        <integer>30</integer>
        <key>ExitTimeOut</key>
        <integer>15</integer>
        <key>PIDFilePath</key>
        <string>{{PID_FILE}}</string>
    </dict>
    </plist>
    """
}

func loadPlistTemplate() throws -> String {
    if let url = plistTemplatePath() {
        return try String(contentsOf: url, encoding: .utf8)
    }
    log("WARNING: shipped template not found (apple/Relay/Distribution/"
        + "relay_launchagent.plist.template) — using the built-in fallback")
    return inlinePlistTemplate()
}

func renderPlistTemplate(_ template: String, binaryPath: String) -> String {
    var plist = template
    plist = plist.replacingOccurrences(of: "{{BINARY_PATH}}", with: binaryPath)
    plist = plist.replacingOccurrences(of: "{{LOGS_DIR}}", with: RelayConfig.expand(kLogsDir).path)
    plist = plist.replacingOccurrences(of: "{{STANDARD_OUT}}", with: RelayConfig.expand(kLogsOut).path)
    plist = plist.replacingOccurrences(of: "{{STANDARD_ERROR}}", with: RelayConfig.expand(kLogsErr).path)
    plist = plist.replacingOccurrences(of: "{{PID_FILE}}", with: RelayConfig.expand(kLogsDir).path + "/relay.pid")
    return plist
}

// -- launchctl helper -----------------------------------------------------------

func runLaunchctl(_ args: [String]) -> (status: Int32, output: String) {
    let p = Process()
    p.executableURL = URL(fileURLWithPath: "/bin/launchctl")
    p.arguments = args
    let pipe = Pipe()
    p.standardOutput = pipe
    p.standardError = pipe
    do {
        try p.run()
    } catch {
        return (-1, "exec failed: \(error.localizedDescription)")
    }
    p.waitUntilExit()
    let data = pipe.fileHandleForReading.readDataToEndOfFile()
    return (p.terminationStatus, String(data: data, encoding: .utf8) ?? "")
}

func launchAgentIsLoaded() -> Bool {
    let (status, output) = runLaunchctl(["print", "gui/\(getuid())", kAgentLabel])
    return status == 0 && output.contains(kAgentLabel)
}

// -- relay HTTP helpers (server URL from env → RelayConfig; bearer token) -------

func relayBaseURL() throws -> URL {
    if let env = ProcessInfo.processInfo.environment["NERVE_RELAY_BASE_URL"], !env.isEmpty {
        guard let url = URL(string: env), url.scheme != nil else {
            throw RelayConfig.ConfigError.invalidValue("NERVE_RELAY_BASE_URL is not a valid URL")
        }
        return url
    }
    let config = try RelayConfig.load(path: nil)
    return config.baseURL
}

func relayURL(_ base: URL, _ path: String) -> URL {
    var url = base
    url.appendPathComponent("api")
    for segment in path.split(separator: "/") {
        url.appendPathComponent(String(segment))
    }
    return url
}

struct RelayHTTPError: Error, CustomStringConvertible {
    let status: Int
    let body: String
    var description: String { "HTTP \(status): \(body)" }
}

func relayPost(_ url: URL, token: String, body: [String: Any]) async throws -> [String: Any] {
    var request = URLRequest(url: url)
    request.httpMethod = "POST"
    request.setValue("application/json", forHTTPHeaderField: "Content-Type")
    if !token.isEmpty {
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
    }
    request.httpBody = try JSONSerialization.data(withJSONObject: body)
    request.timeoutInterval = 15
    let (data, response) = try await URLSession.shared.data(for: request)
    let status = (response as? HTTPURLResponse)?.statusCode ?? 0
    guard (200...299).contains(status) else {
        throw RelayHTTPError(status: status, body: String(data: data, encoding: .utf8) ?? "(no body)")
    }
    guard let obj = try JSONSerialization.jsonObject(with: data) as? [String: Any] else {
        throw RelayHTTPError(status: status, body: "unparseable JSON response")
    }
    return obj
}

func relayGet(_ url: URL, token: String) async throws -> (status: Int, body: Data) {
    var request = URLRequest(url: url)
    request.timeoutInterval = 10
    if !token.isEmpty {
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
    }
    let (data, response) = try await URLSession.shared.data(for: request)
    return ((response as? HTTPURLResponse)?.statusCode ?? 0, data)
}

// -- durable import cursor (resumable; separate from the outbox capture cursor,
// which is itself durable — persisted in the outbox SQLite `meta` table) -------

func saveImportCursor(_ cursor: Int64, at url: URL) throws {
    let fm = FileManager.default
    try fm.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
    let data = try JSONSerialization.data(withJSONObject: ["cursor": cursor])
    try data.write(to: url, options: [.atomic])
    try? fm.setAttributes([.posixPermissions: 0o600], ofItemAtPath: url.path)
}

func loadImportCursor(_ url: URL) throws -> Int64 {
    let obj = try JSONSerialization.jsonObject(with: Data(contentsOf: url)) as? [String: Any]
    let value = (obj?["cursor"] as? NSNumber)?.int64Value ?? 0
    return value
}

func usage() -> Never {
    let text = """
    nerve-personal-relay — local Messages relay to NerveCenter (§46/§47)

    USAGE
      nerve-personal-relay init-config [path]
      nerve-personal-relay status
      nerve-personal-relay run [--once]
      nerve-personal-relay install
      nerve-personal-relay uninstall
      nerve-personal-relay pair
      nerve-personal-relay health
      nerve-personal-relay import --consent [--window-hours N]

    CHANNELS (Phase 4 D5')
      email / voicemail / calendar / contacts capture planes. Each is
      disabled until opted in (`<channel>_enabled = true` in the config,
      or NERVE_RELAY_<CHANNEL>_ENABLED). Each keeps its own durable
      outbox, retention bounds, and local capture dir
      (<source_dir>/<channel>.json — an array of JSON records). Local
      history is never deleted on send failure (§70).

    ENV OVERRIDES
      NERVE_RELAY_BASE_URL, NERVE_RELAY_TOKEN, NERVE_RELAY_STORE,
      NERVE_RELAY_OUTBOX, NERVE_RELAY_<CHANNEL>_ENABLED,
      NERVE_RELAY_MAIL_TOKEN (mailbox credential for the email plane —
      env only, never in the config file)
    """
    FileHandle.standardError.write(Data(text.utf8))
    exit(Int32(Exit.usage.rawValue))
}

guard let sub = args.first else { usage() }

switch sub {
case "init-config":
    let path = args.count > 1
        ? RelayConfig.expand(args[1])
        : RelayConfig.expand(RelayConfig.defaultConfigPath)
    try? FileManager.default.createDirectory(
        at: path.deletingLastPathComponent(), withIntermediateDirectories: true)
    try RelayConfig.template().write(to: path, atomically: true, encoding: .utf8)
    try? FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: path.path)
    log("wrote config template to \(path.path)")
    log("set a real access token (or NERVE_RELAY_TOKEN), then: nerve-personal-relay run --once")

case "status":
    do {
        let config = try RelayConfig.load(path: nil)
        let engine = RelayEngine(config: config)
        let (schema, backlog) = try engine.open()
        let cursor = try engine.outbox.maxStoreID()
        let maxID = try engine.store.maxID()
        log("store       : \(config.storePath.path)")
        log("schema      : recognizable (user_version=\(schema)) — read-only")
        log("outbox      : \(config.outboxPath.path)")
        log("queued      : \(backlog) pending, cursor=\(cursor), store max_id=\(maxID)")
        log("target      : \(config.baseURL.absoluteString)")
        // D5' channel planes — local-only state (status makes no network
        // calls). Disabled planes are shown so the opt-in surface is
        // visible at a glance.
        for channel in RelayChannel.allCases {
            let cc = config.channels[channel]
            let pad = String(repeating: " ", count: max(0, 12 - channel.rawValue.count))
            if !cc.enabled {
                log("\(channel.rawValue)\(pad): disabled (set \(channel.rawValue)_enabled = true to opt in)")
                continue
            }
            let path = config.channelOutboxPath(channel)
            do {
                let outbox = Outbox(url: path)
                let queued = try outbox.open()
                log("\(channel.rawValue)\(pad): queued=\(queued) delivered=\(try outbox.deliveredCount()) outbox=\(path.path) source=\(cc.sourceDir.path)")
            } catch {
                log("\(channel.rawValue)\(pad): enabled, outbox not opened: \(error)")
            }
        }
    } catch let e as RelayConfig.ConfigError {
        fail(.config, "config: \(e)")
    } catch let e as StoreError {
        switch e {
        case .missing(let p):
            fail(.refused, "store not found at \(p) — is Full Disk Access granted and Messages synced?")
        case .openFailed:
            fail(.refused, "cannot open store read-only — grant this process Full Disk Access (System Settings → Privacy & Security)")
        case .schemaIncompatible(let why):
            fail(.refused, "schema refused (fail-closed): \(why)")
        default:
            fail(.refused, "store: \(e)")
        }
    } catch let e as OutboxError {
        fail(.config, "outbox: \(e)")
    } catch {
        fail(.config, "\(error)")
    }

case "run":
    let once = args.contains("--once")
    do {
        let config = try RelayConfig.load(path: nil)
        let engine = RelayEngine(config: config)
        let (schema, _) = try engine.open()
        // D5' channel planes — one engine per *enabled* channel. A channel
        // whose outbox cannot be opened is skipped (and logged) without
        // stopping the rest: each plane owns its durable outbox (§70).
        var channelEngines: [ChannelEngine] = []
        for channel in config.channels.enabledChannels {
            let cc = config.channels[channel]
            let source: ChannelCaptureSource
            let sourceName: String
            if channel == .email, MailConnectorPlane.mailToken() != nil {
                // Mailbox credential present → the mail-connector plane
                // polls the mailbox host-side; the registry
                // (GET /api/mail-connectors) records which mailbox answers.
                // The token itself is env-only and never logged.
                source = MailConnectorPlane(config: MailConnectorPlane.Config())
                sourceName = "mail-connector (NERVE_RELAY_MAIL_TOKEN env)"
            } else {
                // Operator-supplied capture file: <source_dir>/<channel>.json.
                source = FolderChannelSource(channel: channel, sourceDir: cc.sourceDir)
                sourceName = "capture file \(cc.sourceDir.path)/\(channel.rawValue).json"
            }
            let plane = ChannelEngine(channel: channel, config: config, source: source)
            do {
                _ = try plane.open()
                channelEngines.append(plane)
                log("channel \(channel.rawValue): enabled — outbox=\(config.channelOutboxPath(channel).path) source=\(sourceName)")
            } catch {
                log("channel \(channel.rawValue): outbox unavailable (\(error)) — channel skipped this start")
            }
        }
        log("relay starting: store=\(config.storePath.path) schema=v\(schema) target=\(config.baseURL.absoluteString) interval=\(config.interval)s channels=\(channelEngines.count) \(once ? "mode=once" : "mode=loop")")
        while true {
            do {
                let cycle = try await engine.runOnce()
                if cycle.captured > 0 || cycle.sent > 0 || cycle.failed > 0 || cycle.skipped > 0 {
                    log("captured=\(cycle.captured) sent=\(cycle.sent) replayed=\(cycle.replayed) skipped=\(cycle.skipped) failed=\(cycle.failed) backlog=\(cycle.backlog) cursor=\(cycle.maxStoreID)\(cycle.note.isEmpty ? "" : " note=\(cycle.note)")")
                }
                // Channel planes run after Messages so the primary path
                // always executes first; each channel error is caught and
                // logged — its outbox keeps the data queued (§70).
                for plane in channelEngines {
                    do {
                        let c = try await plane.runOnce()
                        if c.captured > 0 || c.sent > 0 || c.failed > 0 || c.skipped > 0 {
                            log("[\(plane.channel.rawValue)] captured=\(c.captured) sent=\(c.sent) replayed=\(c.replayed) skipped=\(c.skipped) failed=\(c.failed) backlog=\(c.backlog) delivered=\(c.delivered) pruned=\(c.pruned)\(c.note.isEmpty ? "" : " note=\(c.note)")")
                        }
                    } catch {
                        log("[\(plane.channel.rawValue)] cycle error: \(error) — data stays queued (§70)")
                    }
                }
                if once { break }
            } catch let e as StoreError {
                // Store disappeared / permission revoked mid-run: stop
                // rather than spinning. Data in the outbox is safe.
                log("store error: \(e) — stopping (outbox preserves queued events)")
                exit(Int32(Exit.refused.rawValue))
            } catch let e as OutboxError {
                log("outbox error: \(e) — stopping")
                exit(Int32(Exit.config.rawValue))
            } catch {
                // Engine-level (should be rare; transport errors are
                // handled inside flush and stay in the outbox).
                log("cycle error: \(error) — retrying next interval")
            }
            if once { break }
            try? await Task.sleep(nanoseconds: UInt64(config.interval) * 1_000_000_000)
        }
    } catch let e as RelayConfig.ConfigError {
        fail(.config, "config: \(e)")
    } catch {
        fail(.config, "\(error)")
    }


// ============================================================================
// Guided install / pairing / health / import (spec personal-ops-recovery §5).
// These subcommands sit AROUND the existing relay engine: capture,
// normalize, and flush behavior (`run`/`status`) are unchanged.
// ============================================================================

case "install":
    // Refuse to install if the binary is missing/unbuilt (actionable).
    let fm = FileManager.default
    let binaryPath = CommandLine.arguments[0]
    var isDir: ObjCBool = false
    guard fm.fileExists(atPath: binaryPath, isDirectory: &isDir), !isDir.boolValue,
          fm.isExecutableFile(atPath: binaryPath) else {
        fail(.refused,
             "relay binary not found or not executable at \(binaryPath). "
             + "Build it first: cd apple && swift build (or ./build_and_test.sh), "
             + "then re-run `nerve-personal-relay install`.")
    }

    let plist: String
    do {
        let template = try loadPlistTemplate()
        plist = renderPlistTemplate(template, binaryPath: binaryPath)
    } catch {
        fail(.config, "install failed: \(error.localizedDescription)")
    }

    let plistPath = RelayConfig.expand(kPlistPath)
    let logsDir = RelayConfig.expand(kLogsDir)
    do {
        try fm.createDirectory(at: logsDir, withIntermediateDirectories: true)
        try fm.createDirectory(at: plistPath.deletingLastPathComponent(),
                               withIntermediateDirectories: true)
        try plist.write(to: plistPath, atomically: true, encoding: .utf8)
        log("wrote LaunchAgent plist: \(plistPath.path)")
    } catch {
        fail(.config, "install failed (plist write): \(error.localizedDescription)")
    }

    // The agent runs `run`, which needs the config (server URL + store path).
    let configPath = RelayConfig.expand(RelayConfig.defaultConfigPath)
    if !fm.fileExists(atPath: configPath.path) {
        fail(.config, "no relay config at \(configPath.path) — run "
            + "`nerve-personal-relay init-config` first, then re-run install.")
    }

    // Current-correct invocation (macOS 13+): `launchctl bootstrap gui/$UID`.
    // `launchctl load` is retained as a legacy fallback for pre-13 macOS.
    // bootout first so re-install is idempotent (ignore "not loaded").
    let domain = "gui/\(getuid())"
    _ = runLaunchctl(["bootout", domain, plistPath.path])
    let (status, output) = runLaunchctl(["bootstrap", domain, plistPath.path])
    if status == 0 {
        log("LaunchAgent loaded: launchctl bootstrap \(domain) \(kAgentLabel)")
    } else {
        let (legacy, legacyOut) = runLaunchctl(["load", plistPath.path])
        if legacy == 0 {
            log("LaunchAgent loaded (legacy: launchctl load \(plistPath.path))")
        } else {
            let trimmed = output.trimmingCharacters(in: .whitespacesAndNewlines)
            let trimmedLegacy = legacyOut.trimmingCharacters(in: .whitespacesAndNewlines)
            fail(.config, "launchctl bootstrap failed: \(trimmed)\n"
                + "legacy launchctl load failed: \(trimmedLegacy)\n"
                + "Check the plist at \(plistPath.path) and re-run install.")
        }
    }
    log("verify: nerve-personal-relay health")

case "uninstall":
    do {
        let fm = FileManager.default
        let plistPath = RelayConfig.expand(kPlistPath)
        // Idempotent: bootout + legacy unload are both no-ops when unloaded.
        let domain = "gui/\(getuid())"
        _ = runLaunchctl(["bootout", domain, plistPath.path])
        _ = runLaunchctl(["unload", plistPath.path])
        if fm.fileExists(atPath: plistPath.path) {
            try fm.removeItem(at: plistPath)
            log("removed \(plistPath.path)")
        } else {
            log("plist not present — nothing to remove (idempotent no-op)")
        }
        log("uninstalled. Logs remain under \(RelayConfig.expand(kLogsDir).path); "
            + "the credential-store token was NOT removed (re-run `pair` to re-pair).")
    } catch {
        fail(.config, "uninstall failed: \(error.localizedDescription)")
    }

case "pair":
    do {
        // Server URL from env (NERVE_RELAY_BASE_URL) or the relay config.
        let base = try relayBaseURL()
        log("pairing against \(base.absoluteString)")

        // 1) POST /api/relay/pair — the token is returned ONCE in this body.
        let deviceName = ProcessInfo.processInfo.environment["NERVE_RELAY_DEVICE_NAME"]
            ?? "Apple Messages relay"
        let pair = try await relayPost(relayURL(base, "relay/pair"), token: "", body: [
            "device_name": deviceName,
        ])
        guard let deviceID = pair["device_id"] as? String,
              let token = pair["token"] as? String,
              let connectionID = pair["connection_id"] as? String else {
            fail(.pairing, "pair response missing device_id/token/connection_id "
                + "from \(base.absoluteString) — is this a NerveCenter server with "
                + "durable storage configured?")
        }

        // Show the token EXACTLY ONCE. No other subcommand prints it;
        // `health` reports only "present"/"missing".
        print("one-time relay token (shown ONCE — now stored in the secure credential store):")
        print("  \(token)")

        // 2) Store it in the secure credential store (keychain-backed;
        //    0600 file fallback when the keychain is unavailable).
        do {
            try storeToken(token, deviceID: deviceID)
        } catch {
            fail(.pairing, "pair failed: could not store token in the secure "
                + "credential store: \(error.localizedDescription)")
        }

        // 3) POST /api/relay/register to complete registration.
        do {
            let register = try await relayPost(relayURL(base, "relay/register"), token: token, body: [
                "device_id": deviceID,
                "token": token,
            ])
            if register["ok"] as? Bool == false {
                fail(.pairing, "register reported ok=false: \(register)")
            }
        } catch let e as RelayHTTPError {
            fail(.pairing, "register failed: \(e) — the token was stored; re-run "
                + "`nerve-personal-relay pair` to mint a fresh one")
        }

        // 4) Persist identity so health/import can find this device.
        do {
            try saveIdentity(RelayIdentity(
                deviceID: deviceID,
                connectionID: connectionID,
                pairedAt: ISO8601DateFormatter.iso8601Second.string(from: Date()),
                baseURL: base.absoluteString
            ))
        } catch {
            fail(.pairing, "pair failed: could not persist identity: "
                + error.localizedDescription)
        }
        log("identity written to \(identityPath().path)")

        // The freeze: pairing success does NOT make the relay operational —
        // operational_status stays never_synced until the first message is
        // synced through POST /api/relay/messages.
        print("Relay registered — operational status will become connected after the first synced message")
    } catch let e as RelayHTTPError {
        fail(.pairing, "pair failed: \(e)")
    } catch let e as RelayConfig.ConfigError {
        fail(.config, "pair failed (config): \(e) — set NERVE_RELAY_BASE_URL or "
            + "run `nerve-personal-relay init-config` first")
    } catch {
        fail(.pairing, "pair failed: \(error.localizedDescription)")
    }

case "health":
    do {
        // ---- LOCAL diagnostics (repair flow: each failure → exact fix) ----
        print("local:")
        let fm = FileManager.default
        let binaryPath = CommandLine.arguments[0]
        if fm.isExecutableFile(atPath: binaryPath) {
            print("  binary: OK (\(binaryPath))")
        } else {
            print("  binary: MISSING/NOT EXECUTABLE (\(binaryPath))")
            print("    fix: cd apple && swift build   (or run ./build_and_test.sh)")
        }

        let plistPath = RelayConfig.expand(kPlistPath)
        let plistExists = fm.fileExists(atPath: plistPath.path)
        let agentLoaded = launchAgentIsLoaded()
        if plistExists && agentLoaded {
            print("  launchagent: LOADED (\(plistPath.path))")
        } else if plistExists {
            print("  launchagent: plist present but NOT loaded")
            print("    fix: launchctl bootstrap gui/\(getuid()) \(plistPath.path)")
        } else {
            print("  launchagent: NOT INSTALLED")
            print("    fix: nerve-personal-relay install")
        }

        // Full Disk Access: detect by opening the Messages store read-only.
        // A failed open with a permission error means FDA is not granted.
        let storePath = (try? RelayConfig.load(path: nil))?.storePath
            ?? RelayConfig.expand(RelayConfig.defaultStore)
        var db: OpaquePointer?
        let rc = sqlite3_open_v2(storePath.path, &db, SQLITE_OPEN_READONLY, nil)
        let fdaOK = db != nil && rc == SQLITE_OK
        if let handle = db { sqlite3_close(handle) }
        if fdaOK {
            print("  full disk access: OK (opened \(storePath.path) read-only)")
        } else {
            print("  full disk access: NOT GRANTED (store open failed, sqlite rc=\(rc))")
            print("    fix: System Settings → Privacy & Security → Full Disk Access")
            print("         → click + and add the app that launches the relay")
            print("           (Terminal.app for a manual run, or the relay binary")
            print("           itself when running under the LaunchAgent), then re-run")
            print("           this command.")
        }

        let identity = try loadIdentity()
        if let id = identity {
            let tokenPresent = loadStoredToken(deviceID: id.deviceID) != nil
            if tokenPresent {
                print("  token: present in secure credential store (value hidden)")
            } else {
                print("  token: MISSING — re-run `nerve-personal-relay pair`")
            }
            print("  device: \(id.deviceID)")
        } else {
            print("  identity: NOT PAIRED — run `nerve-personal-relay pair` first")
        }

        // ---- SERVER health (GET /api/relay/health, this device) ----
        if let id = identity {
            print("server:")
            if let base = URL(string: id.baseURL) {
                let token = loadStoredToken(deviceID: id.deviceID) ?? ""
                do {
                    let (status, body) = try await relayGet(relayURL(base, "relay/health"), token: token)
                    if status == 200,
                       let root = try JSONSerialization.jsonObject(with: body) as? [String: Any],
                       let rows = root["data"] as? [[String: Any]] {
                        if let row = rows.first(where: { ($0["device_id"] as? String) == id.deviceID }) {
                            print("  auth status: \(row["auth_status"] ?? "?")")
                            print("  operational: \(row["operational_status"] ?? "?") "
                                + "(becomes healthy after the first synced message)")
                            print("  state: \(row["state"] ?? "?")")
                            print("  last seen: \(row["last_seen_at"] as? String ?? "never")")
                            print("  last message received: \(row["last_message_at"] as? String ?? "never")")
                            print("  pending backlog: \((row["pending_backlog"] as? Int) ?? 0)")
                        } else {
                            print("  device \(id.deviceID) not listed on the server — "
                                + "re-run `nerve-personal-relay pair`")
                        }
                    } else {
                        print("  unreachable / non-200 (HTTP \(status)) — check "
                            + "NERVE_RELAY_BASE_URL and server availability")
                    }
                } catch let e as RelayHTTPError {
                    print("  unreachable (HTTP \(e.status)) — \(e.body)")
                } catch {
                    print("  unreachable: \(error.localizedDescription)")
                }
            } else {
                print("  invalid base URL: \(id.baseURL)")
            }
        } else {
            print("server: (not paired — run `nerve-personal-relay pair`)")
        }
    } catch {
        fail(.config, "health failed: \(error.localizedDescription)")
    }

case "import":
    // Explicit consent is MANDATORY (spec §5 hard rule: initial history
    // import only with explicit consent). Refuse without --consent.
    guard args.contains("--consent") else {
        print("REFUSED: `import` performs an initial history import and requires EXPLICIT consent.")
        print("Run:  nerve-personal-relay import --consent [--window-hours N]")
        print("       (the local Messages store is read-only either way)")
        exit(Int32(Exit.refused.rawValue))
    }
    do {
        guard let id = try loadIdentity() else {
            fail(.config, "not paired — run `nerve-personal-relay pair` first")
        }
        guard let storedToken = loadStoredToken(deviceID: id.deviceID) else {
            fail(.pairing, "no stored token for \(id.deviceID) — re-run `nerve-personal-relay pair`")
        }
        let base = try relayBaseURL()

        // Optional bounded window: --window-hours N (default: all history).
        var windowHours: Int? = nil
        if let idx = args.firstIndex(of: "--window-hours"), idx + 1 < args.count {
            windowHours = Int(args[idx + 1])
        }

        // 1) POST /api/relay/import-request (consent: true — the server
        //    422s with the stable code consent_required otherwise).
        let job = try await relayPost(relayURL(base, "relay/import-request"),
                                      token: storedToken,
                                      body: ["device_id": id.deviceID, "consent": true])
        log("import job accepted by server: \(job["import_job_id"] ?? "ok")")

        // 2) Open the local store (schema-guarded, read-only, fail closed).
        let storePath = (try? RelayConfig.load(path: nil))?.storePath
            ?? RelayConfig.expand(RelayConfig.defaultStore)
        let store = MessageStore(url: storePath)
        _ = try store.open()
        log("store opened: \(storePath.path)")

        // 3) Durable import cursor (resumable). The incremental `run` loop's
        //    capture cursor is ALREADY durable — persisted in the outbox
        //    SQLite meta table (Outbox.cursor()/setCursor), seeded at the
        //    store's max id on first run — so import keeps its own cursor
        //    file and hands off cleanly without disturbing that position.
        let cursorURL = RelayConfig.expand(kImportCursorFile)
        var lastID: Int64 = 0
        if FileManager.default.fileExists(atPath: cursorURL.path) {
            lastID = (try? loadImportCursor(cursorURL)) ?? 0
            if lastID > 0 { log("resuming import from cursor \(lastID)") }
        }

        // 4) Tail the historical window in batches and POST each batch to
        //    POST /api/relay/messages with stable message ids (the store's
        //    item_message GUID) so B's (device_id, message_id) dedup makes
        //    re-sends no-ops.
        let batchSize = (try? RelayConfig.load(path: nil))?.batchSize ?? 200
        let normalizer = MessageNormalizer()
        let url = relayURL(base, "relay/messages")
        var totalStored = 0
        var totalDedup = 0
        var totalRejected = 0

        while true {
            let rows = try store.tail(afterCursor: lastID, limit: batchSize)
            if rows.isEmpty { break }
            var messages: [[String: Any]] = []
            for raw in rows {
                do {
                    let norm = try normalizer.normalize(raw)
                    if let hours = windowHours {
                        if let ts = ISO8601DateFormatter.iso8601Second.date(from: norm.timestampISO8601),
                           ts.timeIntervalSinceNow < -Double(hours) * 3600 {
                            continue  // outside the requested window
                        }
                    }
                    messages.append([
                        "message_id": norm.messageID,
                        "channel": norm.channel,
                        "direction": norm.direction.lowercased(),
                        "peer_identifier": norm.peerIdentifier,
                        "peer_name": (norm.peerName as Any?) ?? NSNull(),
                        "body": norm.body,
                        "received_at": norm.timestampISO8601,
                        "source": norm.source,
                    ])
                } catch {
                    totalRejected += 1  // fail closed per row (unparsable)
                }
            }
            lastID = rows.map(\.id).max() ?? lastID
            if !messages.isEmpty {
                do {
                    let result = try await relayPost(url, token: storedToken, body: [
                        "device_id": id.deviceID,
                        "messages": messages,
                    ])
                    totalStored += Int(result["stored"] as? Int ?? 0)
                    totalDedup += Int(result["skipped"] as? Int ?? 0)
                    log("batch: posted \(messages.count) (stored "
                        + "\(Int(result["stored"] as? Int ?? 0)), dedup-skipped "
                        + "\(Int(result["skipped"] as? Int ?? 0)))")
                } catch {
                    // Transport failure: the durable cursor is persisted
                    // BEFORE this point, so re-running resumes here; the
                    // server dedups any re-sent ids.
                    log("batch send failed: \(error.localizedDescription) — "
                        + "re-run `nerve-personal-relay import --consent` to resume")
                    break
                }
            }
            try saveImportCursor(lastID, at: cursorURL)
        }

        log("import complete: stored=\(totalStored) dedup-skipped=\(totalDedup) "
            + "rejected=\(totalRejected) cursor=\(lastID)")
        print("Import complete (\(totalStored) messages stored). Incremental sync continues "
            + "via the normal relay loop — check with `nerve-personal-relay health`.")
    } catch let e as RelayHTTPError {
        fail(.transport, "import failed: \(e)")
    } catch let e as RelayConfig.ConfigError {
        fail(.config, "import failed (config): \(e)")
    } catch let e as StoreError {
        switch e {
        case .missing(let p):
            fail(.refused, "import failed: store not found at \(p) — grant Full Disk Access")
        case .openFailed:
            fail(.refused, "import failed: cannot open the Messages store read-only — "
                + "grant Full Disk Access (System Settings → Privacy & Security → Full Disk Access)")
        case .schemaIncompatible(let why):
            fail(.refused, "import failed: schema refused (fail-closed): \(why)")
        default:
            fail(.refused, "import failed (store): \(e)")
        }
    } catch {
        fail(.transport, "import failed: \(error.localizedDescription)")
    }
default:
    usage()
}
