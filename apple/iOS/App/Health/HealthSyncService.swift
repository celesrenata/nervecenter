#if os(iOS)
import Foundation
import Security
import NerveApps

// MARK: - Pairing secret in the Keychain

/// Stores the relay device id and token for the Health bridge. The token never
/// leaves the Keychain except as the Bearer header of the ingest request.
enum HealthKeychain {
    private static let service = "com.celesrenata.NervePhone.health"

    static func save(account: String, value: String) {
        let base: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
        ]
        SecItemDelete(base as CFDictionary)
        guard !value.isEmpty else { return }
        var add = base
        add[kSecValueData as String] = Data(value.utf8)
        add[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
        SecItemAdd(add as CFDictionary, nil)
    }

    static func load(account: String) -> String? {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
            kSecReturnData as String: true,
            kSecMatchLimit as String: kSecMatchLimitOne,
        ]
        var out: CFTypeRef?
        guard SecItemCopyMatching(query as CFDictionary, &out) == errSecSuccess,
              let data = out as? Data else { return nil }
        return String(data: data, encoding: .utf8)
    }
}

// MARK: - Sync service

@MainActor
final class HealthSyncService: ObservableObject {
    @Published private(set) var state: HealthSyncState
    @Published private(set) var syncing = false
    @Published private(set) var isPaired: Bool
    @Published private(set) var authorized: Bool

    private let reader = HealthKitReader()
    private let stateKey = "nerve.health.syncState.v1"
    private let authKey = "nerve.health.authorized.v1"

    init() {
        if let data = UserDefaults.standard.data(forKey: stateKey),
           let saved = try? JSONDecoder().decode(HealthSyncState.self, from: data) {
            state = saved
        } else {
            state = HealthSyncState()
        }
        isPaired = HealthKeychain.load(account: "device") != nil && HealthKeychain.load(account: "token") != nil
        authorized = UserDefaults.standard.bool(forKey: authKey)
    }

    var healthAvailable: Bool { HealthKitReader.isAvailable }
    var deviceID: String { HealthKeychain.load(account: "device") ?? "" }

    func savePairing(deviceID: String, token: String) {
        HealthKeychain.save(account: "device", value: deviceID.trimmingCharacters(in: .whitespacesAndNewlines))
        HealthKeychain.save(account: "token", value: token.trimmingCharacters(in: .whitespacesAndNewlines))
        isPaired = !deviceID.isEmpty && !token.isEmpty
    }

    func requestAccess() async {
        do {
            try await reader.requestAuthorization()
            authorized = true
            UserDefaults.standard.set(true, forKey: authKey)
            await reader.enableBackgroundDelivery()
        } catch {
            state.recordFailure("Health access was not granted")
            persist()
        }
    }

    func syncNow(apiBase: URL?) async {
        guard !syncing else { return }
        guard let apiBase else { state.recordFailure("Server address is not set"); persist(); return }
        syncing = true
        defer { syncing = false }
        let (samples, commit) = await reader.readNew()
        let client = HealthSyncClient(
            apiBase: apiBase,
            deviceID: HealthKeychain.load(account: "device"),
            token: HealthKeychain.load(account: "token")
        ) { request in
            let (data, response) = try await URLSession.shared.data(for: request)
            return ((response as? HTTPURLResponse)?.statusCode ?? 0, data)
        }
        if samples.isEmpty {
            state.recordSuccess(HealthIngestResult(received: 0, stored: 0, skipped: 0, sleepRecords: 0), at: Date())
            persist()
            commit()
            return
        }
        switch await client.send(samples) {
        case .success(let result):
            commit()
            state.recordSuccess(result, at: Date())
        case .failure(.notPaired):
            state.recordFailure("Enter the device id and token first")
        case .failure(.unauthorized):
            state.recordFailure("The server rejected this device token")
        case .failure(.rejected(let code)):
            state.recordFailure("The server answered \(code)")
        case .failure(.network):
            state.recordFailure("Could not reach the server")
        }
        persist()
    }

    private func persist() {
        if let data = try? JSONEncoder().encode(state) {
            UserDefaults.standard.set(data, forKey: stateKey)
        }
    }
}
#endif
