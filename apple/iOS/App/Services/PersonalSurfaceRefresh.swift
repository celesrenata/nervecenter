import Foundation

import NerveAPI
import NerveApps

// MARK: - Personal-surface refresh helper (§66)
//
// A small helper that each personal view calls to trigger a fetch.
// Mirrors how `PhoneEnvironment.fetchInto` creates a fresh `APIClient`
// per fetch: the personal plane builds its own client from the
// environment's `server` config and drives `PersonalFetchService`.
//
// This keeps `PhoneEnvironment` untouched (the engineering data-lift
// already landed) and avoids a cross-store dependency. Each call is
// independent: a failing personal fetch does not affect the
// engineering tabs.
@MainActor
public enum PersonalSurfaceRefresh {
    /// Create an `APIClient` from the environment's server config.
    /// Returns `nil` when the server has no valid base URL.
    public static func makeClient(_ server: ServerConfig) -> APIClient? {
        guard let baseURL = server.apiBaseURL else { return nil }
        return APIClient(baseURL: baseURL, transport: URLSessionAPIClient())
    }

    /// Create a `PersonalFetchService` from the environment's server.
    /// Returns `nil` when the server has no valid base URL.
    public static func makeService(
        server: ServerConfig,
        store: PersonalDataStore
    ) -> PersonalFetchService? {
        guard let client = makeClient(server) else { return nil }
        return PersonalFetchService(client: client, store: store)
    }
}
