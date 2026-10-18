import Foundation

import Combine
import NerveAPI
import NerveApps
import NerveAuth
import NerveModels
import NervePersistence
import NerveRealtime

// MARK: - §70/§71/§72 iPhone app environment

/// App Group container identifier shared by the iPhone app and the Watch/
/// Widget extension targets (spec §71/§72/§69).
public enum NerveAppGroup {
    public static let identifier = "group.com.celestium.nervecenter"
}

/// The App Group container directory shared across Phone / Watch / Widgets.
public enum AppGroupContainer {
    /// Resolve the shared container directory: the App Group container when
    /// the entitlement is present, otherwise an Application Support fallback
    /// (host / unit tests / pre-configuration).
    public static var directory: URL {
        let fm = FileManager.default
        if let group = fm.containerURL(
            forSecurityApplicationGroupIdentifier: NerveAppGroup.identifier
        ) {
            return group
        }
        let base = fm.urls(for: .applicationSupportDirectory, in: .userDomainMask).first ?? fm.temporaryDirectory
        return base.appendingPathComponent("NerveCenter", isDirectory: true)
    }

    /// Ensure a named subdirectory exists and return its URL.
    public static func subdirectory(_ name: String) -> URL {
        let url = directory.appendingPathComponent(name, isDirectory: true)
        try? FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
        return url
    }
}

/// The live `NerveCenter` server the phone is configured to talk to (spec §70:
/// manual server configuration is required; Bonjour is optional).
public struct ServerConfig: Codable, Hashable, Sendable {
    public var host: String
    public var port: Int
    public var scheme: String

    public init(host: String, port: Int = 443, scheme: String = "https") {
        self.host = host
        self.port = port
        self.scheme = scheme
    }

    public var apiBaseURL: URL? {
        URL(string: "\(scheme)://\(host):\(port)/api")
    }

    public var webSocketURL: URL? {
        let wsScheme = (scheme == "https") ? "wss" : "ws"
        return URL(string: "\(wsScheme)://\(host):\(port)/ws/events")
    }

    /// Parse a `--nerve-server=<scheme://host:port>` override value (C-3 /
    /// §70 manual server configuration). Accepts optional scheme and port;
    /// defaults to `https` + 443. Returns `nil` when unparseable.
    public static func parse(_ raw: String) -> ServerConfig? {
        var value = raw
        var scheme = "https"
        var host = value
        var port = 443
        if let schemeRange = value.range(of: "://") {
            scheme = String(value[..<schemeRange.lowerBound])
            host = String(value[schemeRange.upperBound...])
        }
        if let slash = host.firstIndex(of: ":") {
            port = Int(host[host.index(after: slash)...]) ?? port
            host = String(host[..<slash])
        }
        guard !host.isEmpty, scheme == "https" || scheme == "http" else { return nil }
        return ServerConfig(host: host, port: port, scheme: scheme)
    }
}

/// The iPhone app's service container: wires the existing `NerveShared`
/// modules (`NerveAPI` · `NerveAuth` · `NervePersistence` · `NerveRealtime`
/// · `NerveDesign`) so the six §56 tabs share one real configuration.
///
/// This object *is* the app-level wiring the tests exercise (pairing,
/// biometric, cache round-trip, shared-state publication).
public final class PhoneEnvironment: ObservableObject {
    /// The configured server (§70).
    @Published public var server: ServerConfig
    /// Whether a device credential has been stored (pairing complete).
    @Published public var isPaired: Bool

    /// The §73 local cache actor.
    public let cache: LocalCache
    /// The Keychain-backed credential store (§71).
    public let credentialStore: any CredentialStore
    /// The shared cross-target state (App Group) publisher for
    /// Watch/Widgets (§69/§62).
    public let sharedStore: SharedStateStore
    /// Biometric gate for §72 approvals.
    public let biometric: BiometricApprovalGate

    /// Current §73 snapshot, kept up-to-date by the environment itself so
    /// the UI never has to cross the actor boundary.
    @Published public private(set) var cacheState: CachedState

    public let deviceID: String

    private var lastRealtimeClient: (any RealtimeClient)?

    public init(
        server: ServerConfig,
        store: any CredentialStore = InMemoryCredentialStore(),
        biometric: any BiometricAuthenticator = InMemoryBiometricAuthenticator(result: true),
        sharedStore: SharedStateStore? = nil,
        deviceID: String = "iphone"
    ) {
        self.server = server
        self.credentialStore = store
        self.deviceID = deviceID
        self.biometric = BiometricApprovalGate(authenticator: biometric)
        self.cache = LocalCache(directory: AppGroupContainer.subdirectory("nerve-cache"))
        self.sharedStore = sharedStore
            ?? SharedStateStore(directory: AppGroupContainer.subdirectory("published"))
        self.cacheState = CachedState()
        self.isPaired = false
    }
}

// MARK: - Lifecycle (async setup — called from the App init on MainActor)

extension PhoneEnvironment {
    /// Load the persisted credential (if any) and hydrate the cache snapshot
    /// into the observable state. Called from `NervePhoneApp.init()` on the
    /// main actor.
    @MainActor
    public func bootstrap() async {
        let existing: Credential? = try? await credentialStore.retrieveCredential(forDevice: deviceID)
        isPaired = (existing != nil)
        cacheState = await cache.snapshot
    }

    /// Fetch live data from the configured server into `store`, then publish
    /// a fresh §73 snapshot into the shared App Group so the Watch / Widgets
    /// render real data (spec §69/§73). Truthful: a missing/unreachable
    /// server leaves the store in its honest empty/error state and publishes
    /// nothing new.
    @MainActor
    public func fetchInto(_ store: PhoneDataStore) async {
        guard let baseURL = server.apiBaseURL else { return }
        let client = APIClient(baseURL: baseURL, transport: URLSessionAPIClient())
        let service = PhoneFetchService(client: client, store: store)
        await service.fetchAll()

        // Reflect the live compact state into the observable cache snapshot so
        // the store's re-apply + the shared App Group both see fresh data.
        let snapshot = CachedState(
            systemHealth: store.systemHealth,
            activeIncidents: store.activeIncidents,
            runningAgents: store.runningAgents,
            pendingApprovals: store.pendingApprovals,
            primaryGoalProgress: store.primaryGoalProgress ?? 0,
            updatedAt: store.updatedAt,
            recentIncidentIDs: store.incidents.map(\.id),
            recentGoalIDs: store.goals.map(\.id)
        )
        cacheState = snapshot
        publishToAppGroup(snapshot)
    }

    /// Write the §73 snapshot to the App Group container the Watch reads
    /// (`group.nerv.center` / `cached-state.json`). The phone target carries
    /// both the `group.nerv.center` and `group.com.celestium.nervecenter`
    /// entitlements, so this shares one container across Phone / Watch /
    /// Widgets. Falls back to a local path (host / unit tests) when the group
    /// container is unavailable.
    public func publishToAppGroup(_ snapshot: CachedState) {
        let fm = FileManager.default
        let container: URL
        if let group = fm.containerURL(
            forSecurityApplicationGroupIdentifier: "group.nerv.center"
        ) {
            container = group
        } else {
            let base = fm.urls(for: .applicationSupportDirectory, in: .userDomainMask).first
                ?? fm.temporaryDirectory
            container = base.appendingPathComponent("NerveCenter", isDirectory: true)
        }
        try? fm.createDirectory(at: container, withIntermediateDirectories: true)
        let url = container.appendingPathComponent("cached-state.json")
        // `NerveJSON` uses ISO-8601 dates — the same strategy the Watch's
        // `WatchSnapshotSource.readSnapshot()` decodes with.
        if let data = NerveJSON.encode(snapshot) {
            try? data.write(to: url)
        }
    }
}

// MARK: - Pairing (§71)

extension PhoneEnvironment {
    /// Mark the phone as paired after a successful `PairingFlow` result.
    /// (§71: iPhone → challenge → server → credential → Keychain.) The
    /// ``PairingFlow`` already wrote the credential into the
    /// ``NerveAuth/CredentialStore``; this env adopts the state.
    @MainActor
    public func adoptPairing(_ outcome: PairingOutcome) async {
        isPaired = (outcome.credential != nil)
        publishSharedState()
    }

    /// Unpair: drop the stored credential and any published shared state.
    @MainActor
    public func unpair() async {
        try? await credentialStore.deleteCredential(forDevice: deviceID)
        sharedStore.clear()
        isPaired = false
        publishSharedState()
    }
}

// MARK: - Realtime (§70 local connectivity)

extension PhoneEnvironment {
    /// Open the concrete WebSocket realtime stream (spec §70: manual server
    /// configuration — no Bonjour required).
    public func connectRealtime(session: URLSession = .shared) {
        guard let url = server.webSocketURL else { return }
        let client = WebSocketRealtimeClient(url: url, session: session)
        client.connect()
        lastRealtimeClient = client
    }

    public func disconnectRealtime() {
        lastRealtimeClient?.disconnect()
        lastRealtimeClient = nil
    }

    /// Fold a realtime event into the §73 cache and republish shared state.
    ///
    /// Only the fields the event explicitly carries are modified, so a
    /// partial event (e.g. a goal progress update) never clobbers unrelated
    /// counts.
    @MainActor
    public func applyRealtimeEvent(_ event: Event) async {
        var snapshot = cacheState
        var changed = false

        if let v = event.payload["active_incidents"]?.value.intValue, v >= 0 {
            snapshot.activeIncidents = v
            changed = true
        }
        if let v = event.payload["running_agents"]?.value.intValue, v >= 0 {
            snapshot.runningAgents = v
            changed = true
        }
        if let v = event.payload["pending_approvals"]?.value.intValue, v >= 0 {
            snapshot.pendingApprovals = v
            changed = true
        }
        if let health = event.payload["system_health"]?.value.stringValue {
            snapshot.systemHealth = health
            changed = true
        }
        snapshot.updatedAt = event.occurredAt

        let next = snapshot
        if changed {
            await cache.update { state in
                state = next
            }
            cacheState = next
            publishSharedState()
        }
    }
}

// MARK: - §73 cache snapshot + §69/§62 publication

extension PhoneEnvironment {
    /// Publish the current §73 cache state as §69/§62 shared state for
    /// Watch and Widgets.
    @MainActor
    public func publishSharedState() {
        let published = PublishedNerveState.make(
            from: cacheState,
            primaryGoalLabel: nil,
            incidentsSummary: nil,
            agentActivitySummary: nil,
            computeRows: []
        )
        try? sharedStore.write(published)
    }
}
