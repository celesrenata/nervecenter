import SwiftUI

import NerveDesign

// MARK: - §70 manual server configuration (C-3 / gaps-apple1)

/// UserDefaults-backed persistence for the configured server (§70: "manual
/// server configuration is required; Bonjour is optional"). `ServerConfig` is
/// not a credential (the Keychain holds pairing secrets), so plain
/// `UserDefaults` is the honest store. Absent → the app default.
enum ServerConfigStore {
    private static let key = "nerve.serverConfig.v1"
    static let defaultConfig = ServerConfig(host: "nerv.celestium.life")

    static func load() -> ServerConfig {
        guard
            let data = UserDefaults.standard.data(forKey: key),
            let cfg = try? JSONDecoder().decode(ServerConfig.self, from: data)
        else {
            return defaultConfig
        }
        return cfg
    }

    static func save(_ config: ServerConfig) {
        if let data = try? JSONEncoder().encode(config) {
            UserDefaults.standard.set(data, forKey: key)
        }
    }
}

/// A presented sheet for choosing the NerveCenter server (scheme / host /
/// port) with an honest live connection test against `/api/health` before
/// saving. Closes the dead header button from the visual-lift audit (C-3):
/// tapping the host on the Overview tab used to do nothing.
struct ServerConfigSheet: View {
    @EnvironmentObject var env: PhoneEnvironment
    @Environment(\.dismiss) private var dismiss
    let store: PhoneDataStore

    @State private var scheme: String = "https"
    @State private var host: String = ""
    @State private var port: String = "443"
    @State private var testResult: TestResult?
    @State private var testing = false
    @State private var saving = false

    enum TestResult: Equatable {
        case ok(String)
        case failure(String)
    }

    private var parsed: ServerConfig {
        ServerConfig(
            host: host.trimmed,
            port: Int(port) ?? 443,
            scheme: scheme
        )
    }

    var body: some View {
        NavigationStack {
            Form {
                Section("Server") {
                    TextField("Host", text: $host)
                        .autocorrectionDisabled()
                        .modifyServerConfigHostField()
                        .accessibilityIdentifier("serverConfig.host")
                    HStack {
                        TextField("Port", text: $port, prompt: Text("443"))
                            .modifyServerConfigPortField()
                        Picker("Scheme", selection: $scheme) {
                            ForEach(["https", "http"], id: \.self) { Text($0) }
                        }
                        .accessibilityIdentifier("serverConfig.scheme")
                    }
                }

                Section {
                    Button {
                        Task { await runConnectionTest() }
                    } label: {
                        if testing {
                            HStack(spacing: Spacing.sm) {
                                ProgressView()
                                Text("Testing…")
                            }
                        } else {
                            Label("Test connection", systemImage: "antenna.radiowaves.left.and.right")
                        }
                    }
                    .disabled(testing || host.trimmed.isEmpty)
                    .accessibilityIdentifier("serverConfig.test")

                    if let result = testResult {
                        Group {
                            switch result {
                            case .ok(let detail):
                                Label(detail, systemImage: "checkmark.seal")
                                    .foregroundStyle(.green)
                            case .failure(let detail):
                                Label(detail, systemImage: "xmark.seal")
                                    .foregroundStyle(.red)
                            }
                        }
                        .font(.footnote)
                    }
                } footer: {
                    Text("The test performs a single GET \(scheme)://\(host.isEmpty ? "host" : host):\(port)/api/health. The server is only changed when you press Save.")
                }

                Section {
                    Button {
                        Task { await save() }
                    } label: {
                        if saving {
                            HStack(spacing: Spacing.sm) {
                                ProgressView()
                                Text("Saving…")
                            }
                        } else {
                            Text("Save & refresh")
                        }
                    }
                    .disabled(saving || testResult == nil)
                    .accessibilityIdentifier("serverConfig.save")
                } footer: {
                    Text("Saving re-fetches the engineering surfaces from the new server and publishes a fresh §73 snapshot to the Watch / Widgets. You must test the connection before saving.")
                }
            }
            .navigationTitle("Server")
            .inlineNavigationTitle()
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") { dismiss() }
                        .accessibilityIdentifier("serverConfig.cancel")
                }
            }
            .onAppear {
                scheme = env.server.scheme
                host = env.server.host
                port = String(env.server.port)
            }
        }
        .accessibilityIdentifier("serverConfig.sheet")
    }

    @MainActor
    private func runConnectionTest() async {
        testResult = nil
        testing = true
        defer { testing = false }
        guard let url = parsed.healthURL else {
            testResult = .failure("No valid server URL (check scheme and host).")
            return
        }
        let request = URLRequest(url: url, timeoutInterval: 5)
        do {
            let (data, response) = try await URLSession.shared.data(for: request)
            let status = (response as? HTTPURLResponse)?.statusCode ?? -1
            var state = ""
            if
                let root = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                let s = root["state"] as? String
            {
                state = s
            }
            switch status {
            case 200:
                testResult = state.isEmpty
                    ? .ok("Reachable (HTTP 200).")
                    : .ok("Reachable (HTTP 200, state: \(state)).")
            case 401, 403:
                testResult = .ok("Reachable (HTTP \(status) — auth required, host is up).")
            default:
                testResult = .failure("HTTP \(status) — server answered but the health endpoint is not healthy.")
            }
        } catch {
            testResult = .failure("Unreachable: \(error.localizedDescription)")
        }
    }

    @MainActor
    private func save() async {
        guard parsed.apiBaseURL != nil else {
            testResult = .failure("No valid server URL (check scheme and host).")
            return
        }
        saving = true
        defer { saving = false }
        env.server = parsed
        ServerConfigStore.save(parsed)
        // Re-fetch the engineering surfaces from the new server and publish a
        // fresh §73 snapshot so Watch / Widgets converge to the new target.
        await env.fetchInto(store)
        dismiss()
    }
}

private extension String {
    var trimmed: String { trimmingCharacters(in: .whitespaces) }
}

// MARK: - Cross-platform field shims (macOS host build)
//
// The package cross-compiles the iPhone target for macOS (SPM `swift build`
// on the host), so iOS-only UIKit-bridge modifiers must be gated. These
// shims keep the call sites platform-neutral (see CrossPlatformToolbar.swift).

extension View {
    /// URL-typed host field on iOS; a plain field elsewhere.
    @ViewBuilder
    func modifyServerConfigHostField() -> some View {
        #if os(iOS)
        self
            .keyboardType(.URL)
            .textInputAutocapitalization(.never)
        #else
        self
        #endif
    }

    /// Number-pad port field on iOS; a plain field elsewhere.
    @ViewBuilder
    func modifyServerConfigPortField() -> some View {
        #if os(iOS)
        self.keyboardType(.numberPad)
        #else
        self
        #endif
    }
}

#if DEBUG
/// Unit-test seam: exercise the persistence round-trip without a server.
struct ServerConfigSheet_Previews: View {
    @StateObject private var env = PhoneEnvironment(
        server: ServerConfig(host: "example.test")
    )
    var body: some View {
        ServerConfigSheet(store: PhoneDataStore())
            .environmentObject(env)
    }
}
#endif
