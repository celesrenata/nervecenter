#if os(iOS)
import Foundation
import HealthKit
import NerveApps

// MARK: - HealthKit reader (read-only, NS-ADR-100)
//
// Reads weight, sleep, steps, heart rate, resting HR, HRV and active energy.
// Each type keeps an HKQueryAnchor in UserDefaults, so every sync fetches only
// what is new. The app never writes to Health. Background delivery wakes the
// app when new samples arrive; if iOS denies it the foreground sync still works.

final class HealthKitReader {
    private let store = HKHealthStore()
    private let defaults: UserDefaults
    /// Cap one first-sync pull per type so a long history cannot stall the app.
    private let firstSyncWindow: TimeInterval = 30 * 24 * 3600

    init(defaults: UserDefaults = .standard) {
        self.defaults = defaults
    }

    static var isAvailable: Bool { HKHealthStore.isHealthDataAvailable() }

    private struct Spec {
        let kind: HealthKind
        let type: HKSampleType
        let unit: HKUnit?
        let isSum: Bool
    }

    private var specs: [Spec] {
        func q(_ id: HKQuantityTypeIdentifier) -> HKSampleType { HKQuantityType(id) }
        return [
            Spec(kind: .bodyMass, type: q(.bodyMass), unit: .gramUnit(with: .kilo), isSum: false),
            Spec(kind: .stepCount, type: q(.stepCount), unit: .count(), isSum: true),
            Spec(kind: .heartRate, type: q(.heartRate), unit: HKUnit.count().unitDivided(by: .minute()), isSum: false),
            Spec(kind: .restingHeartRate, type: q(.restingHeartRate), unit: HKUnit.count().unitDivided(by: .minute()), isSum: false),
            Spec(kind: .hrv, type: q(.heartRateVariabilitySDNN), unit: .secondUnit(with: .milli), isSum: false),
            Spec(kind: .activeEnergy, type: q(.activeEnergyBurned), unit: .kilocalorie(), isSum: true),
            Spec(kind: .sleep, type: HKCategoryType(.sleepAnalysis), unit: nil, isSum: false),
        ]
    }

    /// Shows the system permission sheet (read-only: nothing is shared for writing).
    func requestAuthorization() async throws {
        guard Self.isAvailable else { return }
        try await store.requestAuthorization(toShare: [], read: Set(specs.map(\.type)))
    }

    /// Ask iOS to wake the app when new samples land. Best effort.
    func enableBackgroundDelivery() async {
        for spec in specs {
            try? await store.enableBackgroundDelivery(for: spec.type, frequency: .hourly)
        }
    }

    /// Fetch everything new since the stored anchors. Anchors are only
    /// committed by `commit()` once the server accepted the samples.
    func readNew() async -> (samples: [HealthSamplePayload], commit: () -> Void) {
        var all: [HealthSamplePayload] = []
        var pending: [(String, HKQueryAnchor)] = []
        for spec in specs {
            let key = "nerve.health.anchor.\(spec.kind.rawValue)"
            let anchor = loadAnchor(key)
            guard let (samples, newAnchor) = try? await fetch(spec: spec, anchor: anchor) else { continue }
            all.append(contentsOf: samples)
            if let newAnchor { pending.append((key, newAnchor)) }
        }
        return (all, { [defaults] in
            for (key, anchor) in pending {
                if let data = try? NSKeyedArchiver.archivedData(withRootObject: anchor, requiringSecureCoding: true) {
                    defaults.set(data, forKey: key)
                }
            }
        })
    }

    private func loadAnchor(_ key: String) -> HKQueryAnchor? {
        guard let data = defaults.data(forKey: key) else { return nil }
        return try? NSKeyedUnarchiver.unarchivedObject(ofClass: HKQueryAnchor.self, from: data)
    }

    private func fetch(spec: Spec, anchor: HKQueryAnchor?) async throws -> ([HealthSamplePayload], HKQueryAnchor?) {
        let predicate: NSPredicate? = anchor == nil
            ? HKQuery.predicateForSamples(withStart: Date().addingTimeInterval(-firstSyncWindow), end: nil)
            : nil
        return try await withCheckedThrowingContinuation { continuation in
            let query = HKAnchoredObjectQuery(
                type: spec.type, predicate: predicate, anchor: anchor, limit: HKObjectQueryNoLimit
            ) { _, added, _, newAnchor, error in
                if let error { continuation.resume(throwing: error); return }
                let payloads = Self.convert(added ?? [], spec: spec)
                continuation.resume(returning: (payloads, newAnchor))
            }
            store.execute(query)
        }
    }

    private static func convert(_ samples: [HKSample], spec: Spec) -> [HealthSamplePayload] {
        if spec.kind == .sleep {
            let asleep: Set<Int> = [
                HKCategoryValueSleepAnalysis.asleepUnspecified.rawValue,
                HKCategoryValueSleepAnalysis.asleepCore.rawValue,
                HKCategoryValueSleepAnalysis.asleepDeep.rawValue,
                HKCategoryValueSleepAnalysis.asleepREM.rawValue,
            ]
            let segments = samples.compactMap { s -> (start: Date, end: Date)? in
                guard let c = s as? HKCategorySample, asleep.contains(c.value) else { return nil }
                return (c.startDate, c.endDate)
            }
            return HealthBridge.mergeSleep(segments: segments)
        }
        guard let unit = spec.unit else { return [] }
        return samples.compactMap { s -> HealthSamplePayload? in
            guard let q = s as? HKQuantitySample else { return nil }
            let value = q.quantity.doubleValue(for: unit)
            guard value.isFinite else { return nil }
            return HealthSamplePayload(kind: spec.kind, startAt: q.startDate, endAt: q.endDate, value: value)
        }
    }
}
#endif
