import Foundation

/// Voicemail plane source backed by the Mac's Phone/FaceTime message store.
///
/// - Sends metadata plus Apple's transcript only (no audio).
/// - A voicemail is yielded only once its transcript decodes to non-empty
///   text; rows without one are withheld and picked up on a later cycle.
/// - A row that is already deleted when first seen is never sent.
/// - A voicemail that was yielded and later gets `ZDATEDELETED`, or vanishes
///   from the store, produces one retraction record (`deleted: true`). The
///   ids yielded so far live in a small state file (ids only, no content).
///
/// Re-yielding the same record is safe: `ChannelEngine` dedupes on the
/// stable id. Nothing here is logged.
public struct VoicemailStoreSource: ChannelCaptureSource {
    public static let sourceLabel = "mac-phone-store"

    private let reader: VoicemailStoreReader
    private let resolver: VoicemailCallerResolver
    private let stateURL: URL

    public static let defaultStatePath = "~/.nervcenter/relay/voicemail-store-state.json"

    public init(
        reader: VoicemailStoreReader,
        resolver: VoicemailCallerResolver = VoicemailCallerResolver(),
        stateURL: URL = RelayConfig.expand(VoicemailStoreSource.defaultStatePath)
    ) {
        self.reader = reader
        self.resolver = resolver
        self.stateURL = stateURL
    }

    struct State: Codable, Equatable {
        var sent: Set<String> = []
        var retracted: Set<String> = []
    }

    public func fetch() async throws -> [ChannelRecord] {
        let rows = try reader.readAll()
        var state = loadState()
        let original = state
        var out: [ChannelRecord] = []
        var present = Set<String>()

        for row in rows {
            present.insert(row.id)
            if row.isDeleted {
                if state.sent.contains(row.id), !state.retracted.contains(row.id) {
                    out.append(retraction(id: row.id, phone: row.phone, at: row.receivedAtUnix))
                    state.retracted.insert(row.id)
                }
                continue
            }
            guard let transcript = row.transcript, !transcript.isEmpty else { continue }
            if state.retracted.contains(row.id) { continue }
            out.append(.voicemail(VoicemailCapture(
                voicemailID: row.id,
                callerPhone: row.phone,
                callerName: resolver.name(forPhone: row.phone, receivedAtUnix: row.receivedAtUnix),
                transcript: transcript,
                durationSeconds: row.durationSeconds,
                receivedAtUnix: row.receivedAtUnix,
                source: Self.sourceLabel,
                read: row.isRead
            )))
            state.sent.insert(row.id)
        }

        // Sent earlier, now gone from the store: retract once.
        for id in state.sent.subtracting(present).subtracting(state.retracted).sorted() {
            out.append(retraction(id: id, phone: "unknown", at: 0))
            state.retracted.insert(id)
        }

        if state != original { try saveState(state) }
        return out
    }

    private func retraction(id: String, phone: String, at: TimeInterval) -> ChannelRecord {
        .voicemail(VoicemailCapture(
            voicemailID: id, callerPhone: phone, callerName: nil, transcript: "",
            durationSeconds: 0, receivedAtUnix: at, source: Self.sourceLabel,
            read: true, deleted: true
        ))
    }

    // -- state (ids only) ------------------------------------------------------

    func loadState() -> State {
        guard let data = try? Data(contentsOf: stateURL),
              let s = try? JSONDecoder().decode(State.self, from: data) else { return State() }
        return s
    }

    private func saveState(_ state: State) throws {
        let dir = stateURL.deletingLastPathComponent()
        try FileManager.default.createDirectory(
            at: dir, withIntermediateDirectories: true,
            attributes: [.posixPermissions: 0o700])
        let enc = JSONEncoder()
        enc.outputFormatting = [.sortedKeys]
        try enc.encode(state).write(to: stateURL, options: .atomic)
        try? FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: stateURL.path)
    }
}
