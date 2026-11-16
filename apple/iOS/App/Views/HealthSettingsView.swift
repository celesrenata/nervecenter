import SwiftUI
import NerveApps

#if os(iOS)

/// Settings > Health: pair this phone, grant Health read access, sync, and see
/// the last sync time and counts.
struct HealthSettingsView: View {
    @EnvironmentObject private var env: PhoneEnvironment
    @Environment(\.dismiss) private var dismiss
    @StateObject private var service = HealthSyncService()
    @State private var deviceID = ""
    @State private var token = ""

    var body: some View {
        NavigationStack {
            Form {
                Section("Pairing") {
                    TextField("Device id (relay-…)", text: $deviceID)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        .accessibilityIdentifier("health.deviceId")
                    SecureField("Device token", text: $token)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        .accessibilityIdentifier("health.token")
                    Button("Save pairing") {
                        service.savePairing(deviceID: deviceID, token: token)
                        token = ""
                    }
                    .disabled(deviceID.isEmpty || token.isEmpty)
                    Text(service.isPaired ? "Paired. The token is stored in the Keychain." : "Not paired yet.")
                        .font(.footnote).foregroundStyle(.secondary)
                }
                Section("Apple Health") {
                    if !service.healthAvailable {
                        Text("Health data is not available on this device.")
                    } else {
                        Button(service.authorized ? "Review Health access" : "Allow Health access") {
                            Task { await service.requestAccess() }
                        }
                        Button(service.syncing ? "Syncing…" : "Sync now") {
                            Task { await service.syncNow(apiBase: env.server.apiBaseURL) }
                        }
                        .disabled(service.syncing || !service.isPaired || !service.authorized)
                        .accessibilityIdentifier("health.syncNow")
                    }
                }
                Section("Last sync") {
                    LabeledContent("When", value: service.state.lastSyncAt.map {
                        $0.formatted(date: .abbreviated, time: .shortened)
                    } ?? "Never")
                    LabeledContent("Samples sent", value: "\(service.state.sentTotal)")
                    LabeledContent("New on server", value: "\(service.state.storedTotal)")
                    if let error = service.state.lastError {
                        Text(error).foregroundStyle(.red).font(.footnote)
                    }
                }
            }
            .navigationTitle("Health")
            .toolbar { ToolbarItem(placement: .confirmationAction) { Button("Done") { dismiss() } } }
            .onAppear { deviceID = service.deviceID }
        }
    }
}
#else
/// HealthKit is iOS-only; the package also compiles this target on macOS.
struct HealthSettingsView: View {
    var body: some View { EmptyView() }
}
#endif
