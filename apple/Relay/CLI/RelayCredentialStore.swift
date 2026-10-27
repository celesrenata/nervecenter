import Foundation
import Security

/// Durable home for the relay pairing token.
///
/// Contract: the pairing token from `POST /api/relay/pair` is returned
/// exactly once. This store writes it **once** (keychain first, 0600 file
/// fallback) and never echoes it: `pair` displays it a single time and
/// `health`/diagnostics report `token: present/missing` at most
/// (spec `personal-ops-recovery` §5 hard rule; `relay.py` "created ONCE,
/// never in subsequent responses/logs").
///
/// `apple/Shared/Sources/NerveAuth/CredentialStore.swift` defines the
/// canonical `CredentialStore` protocol (read-only for this workstream).
/// This file provides the *production* keychain-backed implementation of
/// that same API for the CLI target (the CLI only links `NerveRelayCore`;
/// the Shared package cannot be modified here). The protocol shapes are
/// mirrored 1:1 and guarded by `tests/apple/test_relay_pair_flow.py`
/// (API drift check) so the two never diverge silently.
public protocol RelayCredentialStoreAPI: Sendable {
    func storeCredential(deviceID: String, value: String, expiresAt: Date) throws
    func retrieveCredential(deviceID: String) throws -> String?
    func deleteCredential(deviceID: String) throws
}

/// Keychain-backed implementation of `RelayCredentialStoreAPI`.
///
/// Generic-password item: service `com.nervcenter.relay`, account
/// `relay-token`, accessible after first unlock. On keychain failure
/// (headless session, ACL denied, no GUI) the same value is persisted to
/// `~/Library/Application Support/NerveCenterRelay/credentials.json`
/// (0600, atomic) so pairing can still complete offline. The value is
/// never logged.
public final class RelayKeychainCredentialStore: RelayCredentialStoreAPI {
    public static let service = "com.nervcenter.relay"
    public static let tokenAccount = "relay-token"

    /// Durable fallback file, next to the relay identity/cursor data.
    /// (Uses the shared `FileManager.default` singleton — a stored
    /// `FileManager` instance would not be `Sendable`.)
    public var fallbackPath: URL {
        FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Application Support")
            .appendingPathComponent("NerveCenterRelay")
            .appendingPathComponent("credentials.json")
    }

    public init() {}

    // -- keychain primitives -------------------------------------------------

    private func baseQuery() -> [String: Any] {
        [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: Self.service,
            kSecAttrAccount as String: Self.tokenAccount,
        ]
    }

    @discardableResult
    private func keychainStore(_ value: String) throws -> Bool {
        let data = value.data(using: .utf8)!
        var deleteQuery = baseQuery()
        deleteQuery[kSecValueData as String] = data
        SecItemDelete(deleteQuery as CFDictionary)
        var addQuery = baseQuery()
        addQuery[kSecValueData as String] = data
        addQuery[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlock
        let status = SecItemAdd(addQuery as CFDictionary, nil)
        switch status {
        case errSecSuccess:
            return true
        case errSecDuplicateItem:
            return keychainRead() != nil
        default:
            throw NSError(
                domain: "NerveRelayCredentialStore",
                code: Int(status),
                userInfo: [NSLocalizedDescriptionKey: "SecItemAdd failed (status \(status))"]
            )
        }
    }

    private func keychainRead() -> String? {
        var query = baseQuery()
        query[kSecReturnData as String] = true
        query[kSecMatchLimit as String] = kSecMatchLimitOne
        var result: AnyObject?
        let status = SecItemCopyMatching(query as CFDictionary, &result)
        guard status == errSecSuccess, let data = result as? Data else { return nil }
        return String(data: data, encoding: .utf8)
    }

    private func keychainDelete() {
        SecItemDelete(baseQuery() as CFDictionary)
    }

    // -- 0600 file fallback ----------------------------------------------------

    private func fallbackStore(_ value: String) throws {
        try FileManager.default.createDirectory(
            at: fallbackPath.deletingLastPathComponent(),
            withIntermediateDirectories: true
        )
        let data = try JSONEncoder().encode(["token": value])
        try data.write(to: fallbackPath, options: [.atomic])
        try? FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: fallbackPath.path)
    }

    private func fallbackRead() throws -> String? {
        guard FileManager.default.fileExists(atPath: fallbackPath.path) else { return nil }
        let data = try Data(contentsOf: fallbackPath)
        let payload = try JSONDecoder().decode([String: String].self, from: data)
        return payload["token"]
    }

    // -- API -------------------------------------------------------------------

    public func storeCredential(deviceID: String, value: String, expiresAt: Date) throws {
        do {
            try keychainStore(value)
        } catch {
            try fallbackStore(value)
        }
    }

    public func retrieveCredential(deviceID: String) throws -> String? {
        if let value = keychainRead() {
            try? fallbackStore(value)  // keep both copies in agreement (read-only cost)
            return value
        }
        return try fallbackRead()
    }

    public func deleteCredential(deviceID: String) throws {
        keychainDelete()
        try? FileManager.default.removeItem(at: fallbackPath)
    }
}

/// The store used by every CLI subcommand (keychain-backed production
/// implementation; swap in `InMemoryCredentialStore` for tests).
public func defaultCredentialStore() -> RelayKeychainCredentialStore {
    RelayKeychainCredentialStore()
}
