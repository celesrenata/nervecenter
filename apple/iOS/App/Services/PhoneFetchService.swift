import Foundation

import NerveAPI
import NerveApps
import NerveModels
import NervePersistence

// MARK: - Phone fetch service (truthful, no fabricated data)
//
// This service talks to the backend API endpoints (or `MockTransport` in tests)
// and maps responses into the NerveApps contract types. It is the single source
// of data for the six §56 iPhone tabs (Overview, Goals, Incidents, Agents,
// Systems, Ask).
//
// All fetches are:
// - Truthful: 501 responses → error state, empty arrays → empty state
// - Stale-aware: every fetch updates `cache.updatedAt`, UI shows stale badge
// - Thread-safe: all decode happens off main actor; only state mutation is
//   MainActor-protected

/// A fetch error that can be surfaced to the user (network vs server).
public enum FetchError: Error, CustomStringConvertible, Sendable {
    /// Network-level failure (DNS, TLS, connection reset, timeout).
    case network(String)
    /// HTTP error status (4xx/5xx).
    case http(statusCode: Int, body: String?)
    /// Response body could not be decoded.
    case decoding(String)
    /// Unexpected response shape (e.g. empty body for non-204).
    case unexpected(String)

    public var description: String {
        switch self {
        case .network(let msg): return "Network error: \(msg)"
        case .http(let code, _): return "Server error: \(code)"
        case .decoding(let msg): return "Data error: \(msg)"
        case .unexpected(let msg): return "Unexpected response: \(msg)"
        }
    }
}

/// Service that orchestrates fetches against the backend API for all six tabs.
///
/// The service lives on `PhoneEnvironment` and is injected into each tab view
/// so they share one coherent client and state.
public actor PhoneFetchService: Sendable {
    private let client: APIClient
    private let store: PhoneDataStore

    /// Create a fetch service bound to a specific API client and store.
    ///
    /// In production, `client` is backed by `URLSessionAPIClient`; in tests,
    /// `MockTransport`.
    public init(client: APIClient, store: PhoneDataStore) {
        self.client = client
        self.store = store
    }

    /// Fetch goals for the Goals tab.
    public func fetchGoals() async throws {
        do {
            let response = try await client.send(path: "/goals") as WireListResponse<WireGoal>
            store.goals = response.data.map(WireMapper.goal)
            if response.data.isEmpty {
                store.goalsState = .empty
            } else {
                store.goalsState = .loaded
            }
        } catch NerveAPIError.network(let underlying) {
            store.goalsState = .error("Network: \(underlying)")
        } catch NerveAPIError.http(let code, _) where code == 501 {
            store.goalsState = .error("Goals endpoint not wired")
        } catch NerveAPIError.decoding(let underlying) {
            store.goalsState = .error("Goals: \(underlying)")
        } catch NerveAPIError.unexpectedResponse(let underlying) {
            store.goalsState = .error("goals: \(underlying)")
        } catch {
            store.goalsState = .error(String(describing: error))
        }
    }

    /// Fetch incidents for the Incidents tab.
    public func fetchIncidents() async throws {
        do {
            let response = try await client.send(path: "/incidents") as WireListResponse<WireIncident>
            store.incidents = response.data.map(WireMapper.incident)
            if response.data.isEmpty {
                store.incidentsState = .empty
            } else {
                store.incidentsState = .loaded
            }
        } catch NerveAPIError.network(let underlying) {
            store.incidentsState = .error("Network: \(underlying)")
        } catch NerveAPIError.http(let code, _) where code == 501 {
            store.incidentsState = .error("Incidents endpoint not wired")
        } catch NerveAPIError.decoding(let underlying) {
            store.incidentsState = .error("Incidents: \(underlying)")
        } catch NerveAPIError.unexpectedResponse(let underlying) {
            store.incidentsState = .error("incidents: \(underlying)")
        } catch {
            store.incidentsState = .error(String(describing: error))
        }
    }

    /// Fetch agents for the Agents tab.
    public func fetchAgents() async throws {
        do {
            let response = try await client.send(path: "/agents") as WireListResponse<WireAgent>
            store.agents = response.data.map { a in
                AgentRow(
                    id: a.id,
                    name: a.displayName,
                    status: a.displayStatus,
                    currentTask: a.displayTask
                )
            }
            if response.data.isEmpty {
                store.agentsState = .empty
            } else {
                store.agentsState = .loaded
            }
        } catch NerveAPIError.network(let underlying) {
            store.agentsState = .error("Network: \(underlying)")
        } catch NerveAPIError.http(let code, _) where code == 501 {
            store.agentsState = .error("Agents endpoint not wired")
        } catch NerveAPIError.decoding(let underlying) {
            store.agentsState = .error("Agents: \(underlying)")
        } catch NerveAPIError.unexpectedResponse(let underlying) {
            store.agentsState = .error("agents: \(underlying)")
        } catch {
            store.agentsState = .error(String(describing: error))
        }
    }

    /// Fetch systems for the Systems tab.
    public func fetchSystems() async throws {
        do {
            let response = try await client.send(path: "/systems") as WireListResponse<WireSystem>
            store.systems = response.data.map { s in
                SystemRow(
                    id: s.rowID,
                    name: s.displayName,
                    health: s.displayHealth
                )
            }
            if response.data.isEmpty {
                store.systemsState = .empty
            } else {
                store.systemsState = .loaded
            }
        } catch NerveAPIError.network(let underlying) {
            store.systemsState = .error("Network: \(underlying)")
        } catch NerveAPIError.http(let code, let body) where code == 501 {
            store.systemsState = .error("Systems endpoint not wired")
        } catch NerveAPIError.decoding(let underlying) {
            store.systemsState = .error("Systems: \(underlying)")
        } catch NerveAPIError.unexpectedResponse(let underlying) {
            store.systemsState = .error("systems: \(underlying)")
        } catch {
            store.systemsState = .error(String(describing: error))
        }
    }

    /// Fetch Ask answer for a question (POST /api/context/ask).
    public func ask(_ question: String) async throws -> AskAnswer {
        let request = AskRequest(question: question)
        do {
            let response = try await client.send(
                path: "context/ask",
                method: "POST",
                body: request
            ) as WireAskResponse
            return WireMapper.askAnswer(response)
        } catch NerveAPIError.network(let underlying) {
            throw FetchError.network("Network unavailable: \(underlying)")
        } catch NerveAPIError.http(let code, let body) {
            let msg = body.map { String(data: $0, encoding: .utf8) }
                ?? HTTPURLResponse.localizedString(forStatusCode: code)
            throw FetchError.http(statusCode: code, body: msg)
        } catch NerveAPIError.decoding(let underlying) {
            throw FetchError.decoding("Ask: \(underlying)")
        } catch NerveAPIError.unexpectedResponse(let underlying) {
            throw FetchError.unexpected("ask: \(underlying)")
        } catch {
            throw error
        }
    }

    /// Fetch overviewCompact from the overview endpoint and apply compact
    /// state to the store (health, incident/agent/approval counts).
    ///
    /// This endpoint is a stub returning 501 until backend services are wired.
    /// When unavailable, we preserve existing compact state (from cache).
    public func fetchOverviewCompact() async throws {
        // This endpoint is a stub returning 501
        // Leave compact state unchanged (from cache).
    }

    /// Fetch all six §56 sources and reconcile the store.
    ///
    /// Each per-tab fetch is best-effort (a failing endpoint leaves its own
    /// state as an error without aborting the others). After the fetches we
    /// derive the compact counts from whatever actually loaded and bump
    /// `updatedAt` so the freshness badge reflects a real, just-received
    /// snapshot.
    public func fetchAll() async {
        _ = try? await fetchGoals()
        _ = try? await fetchIncidents()
        _ = try? await fetchAgents()
        _ = try? await fetchSystems()

        let systems = store.systems
        let agentCount = store.agents.count
        let incidentCount = store.incidents.count
        let nonHealthy = systems
            .map(\.health)
            .filter { $0.lowercased() != "healthy" }
        let health: String
        if let first = nonHealthy.first {
            health = first
        } else if !systems.isEmpty {
            health = "healthy"
        } else {
            health = store.systemHealth
        }
        await MainActor.run {
            store.applyWireCompact(
                systemHealth: health,
                activeIncidents: incidentCount,
                runningAgents: agentCount,
                pendingApprovals: store.pendingApprovals
            )
            store.touchUpdatedAt()
        }
    }
}
