import Foundation

import Combine
import NerveApps
import NerveModels
import NervePersistence

// MARK: - iPhone tab data store (§65–§66)

/// Load / empty / error / loaded state for a single surface.
public enum LoadState: Equatable {
    case loading
    case empty
    case error(String)
    case loaded
}

/// One renderable row per tab (value types so the UI stays diffable).
public struct AgentRow: Identifiable, Hashable {
    public let id: String
    public let name: String
    public let status: String
    public let currentTask: String?
    public init(id: String, name: String, status: String, currentTask: String? = nil) {
        self.id = id; self.name = name; self.status = status; self.currentTask = currentTask
    }
}

public struct SystemRow: Identifiable, Hashable {
    public let id: String
    public let name: String
    public let health: String
    public init(id: String, name: String, health: String) {
        self.id = id; self.name = name; self.health = health
    }
}

/// A `@MainActor`-safe, test-free observable store that drives the six §56
/// tabs. All tab rows and the compact counts come from the §73 cache, so the
/// app is truthful (no fabricated data) and works offline per §73.
public final class PhoneDataStore: ObservableObject {
    // Compact counts (always present from §73 cache).
    @Published public private(set) var systemHealth = "unknown"
    @Published public private(set) var activeIncidents = 0
    @Published public private(set) var runningAgents = 0
    @Published public private(set) var pendingApprovals = 0
    @Published public private(set) var primaryGoalProgress: Double?
    @Published public private(set) var updatedAt = Date.distantPast

    // Rich rows.
    @Published public var goals: [NerveModels.Goal] = []
    @Published public var incidents: [IncidentDetail] = []
    @Published public var agents: [AgentRow] = []
    @Published public var systems: [SystemRow] = []
    @Published public var askAnswers: [AskAnswer] = []

    // Per-surface load states (driven by the fetch result).
    @Published public var goalsState: LoadState = .empty
    @Published public var incidentsState: LoadState = .empty
    @Published public var agentsState: LoadState = .empty
    @Published public var systemsState: LoadState = .empty
    @Published public var askState: LoadState = .empty

    public init() {}
}

extension PhoneDataStore {
    /// Reconcile the store with a §73 cache snapshot. This is the single place
    /// the tab data is derived from shared state, so a stale cache still
    /// renders correctly (the screens additionally apply ``StaleState``).
   public func apply(_ cache: CachedState) {
       systemHealth = cache.systemHealth
       activeIncidents = cache.activeIncidents
       runningAgents = cache.runningAgents
       pendingApprovals = cache.pendingApprovals
       primaryGoalProgress = cache.primaryGoalProgress
       updatedAt = cache.updatedAt
   }

   // MARK: - Public setters for wire mapping

   /// Replace compact counts from wire state. Called from fetch service.
   public func applyWireCompact(
       systemHealth: String,
       activeIncidents: Int,
       runningAgents: Int,
       pendingApprovals: Int
   ) {
       self.systemHealth = systemHealth
       self.activeIncidents = activeIncidents
       self.runningAgents = runningAgents
       self.pendingApprovals = pendingApprovals
   }

   /// Mark the store as freshly hydrated (call after a successful live
   /// fetch so the §73 freshness/staleness badge reflects "just now").
   public func touchUpdatedAt(_ date: Date = .now) {
       updatedAt = date
   }

    /// True when the last cache update is older than `policy.staleAfter`.
    public func isStale(policy: StalenessPolicy = .default) -> Bool {
        StaleState.evaluate(updatedAt: updatedAt, now: Date(), policy: policy).isStale
    }

    public func staleAgeLabel(policy: StalenessPolicy = .default) -> String {
        let report = StaleState.evaluate(updatedAt: updatedAt, now: Date(), policy: policy)
        return StaleState.asOfLabel(report)
    }
}
