import Foundation
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif
#if canImport(Darwin)
import Darwin
#endif

public struct ProbeFailure: Error, CustomStringConvertible, Sendable {
    public let description: String
    public init(_ description: String) { self.description = description }
}

public struct HostObservation: Codable, Sendable {
    public let system: String
    public let hypervisorAPIAvailable: Bool?
    public let attestation: String
    public let hostConfidentiality: String
    public let confidentialEligibility: Bool
    public let explanation: String

    public static func collect() -> HostObservation {
        #if canImport(Darwin)
        var value: Int32 = 0
        var size = MemoryLayout<Int32>.size
        let status = sysctlbyname("kern.hv_support", &value, &size, nil, 0)
        return HostObservation(system: "macOS", hypervisorAPIAvailable: status == 0 ? value == 1 : nil,
            attestation: "not_performed", hostConfidentiality: "not_established", confidentialEligibility: false,
            explanation: "kern.hv_support reports ordinary Hypervisor API availability. No VM or protected boundary was created or verified.")
        #else
        return HostObservation(system: "other", hypervisorAPIAvailable: nil,
            attestation: "not_performed", hostConfidentiality: "not_established", confidentialEligibility: false,
            explanation: "The native Hypervisor availability observation is implemented only on macOS.")
        #endif
    }
}

public func validatedBaseURL(_ raw: String) throws -> URL {
    guard let parts = URLComponents(string: raw), let scheme = parts.scheme?.lowercased(),
          ["http", "https"].contains(scheme), let host = parts.host?.lowercased(), !host.isEmpty,
          parts.user == nil, parts.password == nil, parts.query == nil, parts.fragment == nil,
          parts.path.isEmpty || parts.path == "/", let url = parts.url else {
        throw ProbeFailure("Use an HTTP(S) origin without credentials, path, query, or fragment.")
    }
    let normalized = host.trimmingCharacters(in: CharacterSet(charactersIn: "[]"))
    let chunks = normalized.split(separator: ".")
    let loopbackV4 = chunks.count == 4 && chunks.first == "127" && chunks.allSatisfy { Int($0).map { 0...255 ~= $0 } ?? false }
    if scheme == "http" && normalized != "localhost" && normalized != "::1" && !loopbackV4 {
        throw ProbeFailure("Plain HTTP is allowed only for a loopback development marketplace; use HTTPS remotely.")
    }
    return url
}

private struct IgnoredObject: Decodable {
    private struct Key: CodingKey { let stringValue: String; init?(stringValue: String) { self.stringValue = stringValue }; let intValue: Int? = nil; init?(intValue: Int) { return nil } }
    init(from decoder: Decoder) throws { _ = try decoder.container(keyedBy: Key.self) }
}

private struct WireSnapshot: Decodable {
    struct Credits: Decodable { let reserved: Int; let settled: Int }
    let workers: [IgnoredObject]
    let tasks: [IgnoredObject]
    let attempts: [IgnoredObject]
    let receipts: [IgnoredObject]
    let credits: Credits
}

public struct SnapshotSummary: Codable, Equatable, Sendable {
    public let workerCount: Int
    public let taskCount: Int
    public let attemptCount: Int
    public let receiptCount: Int
    public let reservedCredits: Int
    public let settledCredits: Int

    public static func decode(_ data: Data) throws -> SnapshotSummary {
        guard data.count <= 1_048_576 else { throw ProbeFailure("Snapshot exceeds the 1 MiB read limit.") }
        let wire = try JSONDecoder().decode(WireSnapshot.self, from: data)
        guard wire.credits.reserved >= 0, wire.credits.settled >= 0 else { throw ProbeFailure("Invalid credit counters.") }
        return SnapshotSummary(workerCount: wire.workers.count, taskCount: wire.tasks.count,
            attemptCount: wire.attempts.count, receiptCount: wire.receipts.count,
            reservedCredits: wire.credits.reserved, settledCredits: wire.credits.settled)
    }
}

private final class NoRedirect: NSObject, URLSessionTaskDelegate, Sendable {
    func urlSession(_ session: URLSession, task: URLSessionTask,
        willPerformHTTPRedirection response: HTTPURLResponse, newRequest request: URLRequest,
        completionHandler: @escaping @Sendable (URLRequest?) -> Void) { completionHandler(nil) }
}

public struct NetworkObservation: Codable, Sendable {
    public let healthHTTPReachable: Bool
    public let snapshot: SnapshotSummary
    public let explanation: String
}

public struct MarketplaceClient: Sendable {
    public init() {}

    private func fetch(_ url: URL) async throws -> Data {
        let config = URLSessionConfiguration.ephemeral
        config.timeoutIntervalForRequest = 5
        config.timeoutIntervalForResource = 8
        config.httpCookieStorage = nil
        config.urlCredentialStorage = nil
        let session = URLSession(configuration: config, delegate: NoRedirect(), delegateQueue: nil)
        defer { session.invalidateAndCancel() }
        var request = URLRequest(url: url)
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        // Stream with a strict read limit rather than buffering an unlimited response.
        let (bytes, response) = try await session.bytes(for: request)
        guard let http = response as? HTTPURLResponse, http.statusCode == 200 else {
            throw ProbeFailure("Marketplace endpoint did not return HTTP 200; redirects are refused.")
        }
        var data = Data()
        for try await byte in bytes {
            guard data.count < 1_048_576 else { throw ProbeFailure("Marketplace response exceeds 1 MiB.") }
            data.append(byte)
        }
        return data
    }

    public func observe(baseURL: URL) async throws -> NetworkObservation {
        let safeBase = try validatedBaseURL(baseURL.absoluteString)
        async let healthData = fetch(safeBase.appendingPathComponent("health"))
        async let snapshotData = fetch(safeBase.appendingPathComponent("v1/snapshot"))
        let health = try await healthData
        guard (try JSONSerialization.jsonObject(with: health)) is [String: Any] else {
            throw ProbeFailure("Health endpoint did not return a JSON object.")
        }
        return NetworkObservation(healthHTTPReachable: true, snapshot: try SnapshotSummary.decode(await snapshotData),
            explanation: "Public marketplace metadata only. Connectivity is not worker enrollment, authorization, or attestation.")
    }
}
