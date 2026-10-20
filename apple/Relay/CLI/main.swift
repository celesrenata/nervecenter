import Foundation
import NerveRelayCore

/// `nerve-personal-relay` — macOS Personal Relay CLI (prompt2 §46).
///
/// Subcommands:
///   init-config [path]   write a template config (default ~/.nervcenter/relay.conf)
///   status               open store + outbox, report schema/cursor/backlog
///   run                  loop: capture + flush every `interval_seconds`
///                        (Messages first, then each *enabled* D5' channel —
///                        email/voicemail/calendar/contacts)
///   run --once           a single capture + flush cycle (then exit)
///
/// All output goes to stdout/stderr so the LaunchAgent can tee it to a
/// log file (§47: observable, user-account scoped, no remote shell).

enum Exit: Int {
    case ok = 0
    case usage = 2
    case config = 3
    case refused = 4      // schema refused / store missing — fail closed
    case transport = 5    // NerveCenter unreachable (outbox keeps data)
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

func usage() -> Never {
    let text = """
    nerve-personal-relay — local Messages relay to NerveCenter (§46/§47)

    USAGE
      nerve-personal-relay init-config [path]
      nerve-personal-relay status
      nerve-personal-relay run [--once]

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

default:
    usage()
}
