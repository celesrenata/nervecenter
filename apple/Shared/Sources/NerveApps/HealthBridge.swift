import Foundation

// MARK: - Apple Health bridge: pure wire logic (NS-ADR-100)
//
// Platform-free so it is unit-testable on macOS. The iOS-only HealthKit reader
// (apple/iOS/App/Services/HealthKitReader.swift) produces `HealthSamplePayload`
// values; this file shapes, batches and sends them to
// `POST /api/health/ingest`. That route is SSO-free and authenticates with the
// device's relay pairing token (`Authorization: Bearer`) plus the paired device
// id in `X-Nerve-Device`. The token never appears in logs or descriptions.

/// Kinds the server accepts, with the unit it requires for each.
public enum HealthKind: String, CaseIterable, Codable, Sendable {
    case bodyMass = "body_mass"
    case sleep = "sleep"
    case stepCount = "step_count"
    case heartRate = "heart_rate"
    case restingHeartRate = "resting_heart_rate"
    case hrv = "hrv"
    case activeEnergy = "active_energy"

    public var unit: String {
        switch self {
        case .bodyMass: return "kg"
        case .sleep: return "min"
        case .stepCount: return "count"
        case .heartRate, .restingHeartRate: return "bpm"
        case .hrv: return "ms"
        case .activeEnergy: return "kcal"
        }
    }
}

/// One sample on the wire (snake_case keys match the server schema).
public struct HealthSamplePayload: Codable, Equatable, Sendable {
    public var kind: String
    public var startAt: Date
    public var endAt: Date
    public var value: Double
    public var unit: String
    public var source: String

    enum CodingKeys: String, CodingKey {
        case kind, value, unit, source
        case startAt = "start_at"
        case endAt = "end_at"
    }

    public init(kind: HealthKind, startAt: Date, endAt: Date, value: Double, source: String = "apple_health") {
        self.kind = kind.rawValue
        self.startAt = startAt
        self.endAt = endAt
        self.value = value
        self.unit = kind.unit
        self.source = source
    }
}

public struct HealthBatchPayload: Codable, Equatable, Sendable {
    public var deviceID: String
    public var samples: [HealthSamplePayload]

    enum CodingKeys: String, CodingKey {
        case deviceID = "device_id"
        case samples
    }

    public init(deviceID: String, samples: [HealthSamplePayload]) {
        self.deviceID = deviceID
        self.samples = samples
    }
}

/// Server reply to a batch.
public struct HealthIngestResult: Codable, Equatable, Sendable {
    public var received: Int
    public var stored: Int
    public var skipped: Int
    public var sleepRecords: Int

    enum CodingKeys: String, CodingKey {
        case received, stored, skipped
        case sleepRecords = "sleep_records"
    }

    public init(received: Int, stored: Int, skipped: Int, sleepRecords: Int) {
        self.received = received
        self.stored = stored
        self.skipped = skipped
        self.sleepRecords = sleepRecords
    }
}

/// Last-sync summary shown on the Health settings view.
public struct HealthSyncState: Codable, Equatable, Sendable {
    public var lastSyncAt: Date?
    public var sentTotal: Int
    public var storedTotal: Int
    public var lastError: String?

    public init(lastSyncAt: Date? = nil, sentTotal: Int = 0, storedTotal: Int = 0, lastError: String? = nil) {
        self.lastSyncAt = lastSyncAt
        self.sentTotal = sentTotal
        self.storedTotal = storedTotal
        self.lastError = lastError
    }

    public mutating func recordSuccess(_ result: HealthIngestResult, at date: Date) {
        lastSyncAt = date
        sentTotal += result.received
        storedTotal += result.stored
        lastError = nil
    }

    public mutating func recordFailure(_ message: String) {
        lastError = message
    }
}

public enum HealthBridgeError: Error, Equatable, Sendable {
    case notPaired
    case unauthorized
    case rejected(Int)
    case network(String)
}

public enum HealthBridge {
    /// Server cap per batch (`MAX_BATCH_SAMPLES`); stay under it.
    public static let batchSize = 400
    /// Asleep segments closer together than this belong to the same night.
    public static let sleepMergeGap: TimeInterval = 60 * 60

    /// Merge asleep segments into one sample per night (minutes asleep, spanning
    /// the first segment start to the last segment end).
    public static func mergeSleep(
        segments: [(start: Date, end: Date)],
        maxGap: TimeInterval = sleepMergeGap
    ) -> [HealthSamplePayload] {
        let sorted = segments.filter { $0.end > $0.start }.sorted { $0.start < $1.start }
        var nights: [(start: Date, end: Date, asleep: TimeInterval)] = []
        for seg in sorted {
            let length = seg.end.timeIntervalSince(seg.start)
            if let last = nights.last, seg.start.timeIntervalSince(last.end) <= maxGap {
                nights[nights.count - 1] = (last.start, max(last.end, seg.end), last.asleep + length)
            } else {
                nights.append((seg.start, seg.end, length))
            }
        }
        return nights.compactMap { night in
            let minutes = (night.asleep / 60).rounded()
            guard minutes >= 1, minutes <= 1440 else { return nil }
            return HealthSamplePayload(kind: .sleep, startAt: night.start, endAt: night.end, value: minutes)
        }
    }

    public static func chunk(_ samples: [HealthSamplePayload], size: Int = batchSize) -> [[HealthSamplePayload]] {
        guard size > 0, !samples.isEmpty else { return [] }
        return stride(from: 0, to: samples.count, by: size).map {
            Array(samples[$0 ..< min($0 + size, samples.count)])
        }
    }

    public static func makeRequest(
        baseURL: URL,
        deviceID: String,
        token: String,
        batch: HealthBatchPayload
    ) throws -> URLRequest {
        var request = URLRequest(url: baseURL.appendingPathComponent("health/ingest"))
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        request.setValue(deviceID, forHTTPHeaderField: "X-Nerve-Device")
        let encoder = JSONEncoder()
        encoder.dateEncodingStrategy = .iso8601
        request.httpBody = try encoder.encode(batch)
        return request
    }

    /// Map an HTTP status and body to a result or a typed error.
    public static func interpret(status: Int, body: Data) -> Result<HealthIngestResult, HealthBridgeError> {
        switch status {
        case 200:
            if let result = try? JSONDecoder().decode(HealthIngestResult.self, from: body) {
                return .success(result)
            }
            return .failure(.rejected(status))
        case 401, 403: return .failure(.unauthorized)
        default: return .failure(.rejected(status))
        }
    }
}

/// Sends batches through an injected HTTP function (URLSession in the app, a
/// stub in tests). `apiBase` is the same base the app already uses (ends in /api).
public struct HealthSyncClient: Sendable {
    public typealias HTTP = @Sendable (URLRequest) async throws -> (Int, Data)

    private let apiBase: URL
    private let deviceID: String?
    private let token: String?
    private let http: HTTP

    public init(apiBase: URL, deviceID: String?, token: String?, http: @escaping HTTP) {
        self.apiBase = apiBase
        self.deviceID = deviceID
        self.token = token
        self.http = http
    }

    /// Send all samples in batches; stops at the first failed batch.
    public func send(_ samples: [HealthSamplePayload]) async -> Result<HealthIngestResult, HealthBridgeError> {
        guard let deviceID, !deviceID.isEmpty, let token, !token.isEmpty else {
            return .failure(.notPaired)
        }
        var total = HealthIngestResult(received: 0, stored: 0, skipped: 0, sleepRecords: 0)
        for part in HealthBridge.chunk(samples) {
            do {
                let request = try HealthBridge.makeRequest(
                    baseURL: apiBase,
                    deviceID: deviceID,
                    token: token,
                    batch: HealthBatchPayload(deviceID: deviceID, samples: part)
                )
                let (status, body) = try await http(request)
                switch HealthBridge.interpret(status: status, body: body) {
                case .success(let r):
                    total.received += r.received
                    total.stored += r.stored
                    total.skipped += r.skipped
                    total.sleepRecords += r.sleepRecords
                case .failure(let error):
                    return .failure(error)
                }
            } catch {
                return .failure(.network("request failed"))
            }
        }
        return .success(total)
    }
}
