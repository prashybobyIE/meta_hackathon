import Foundation
import XCTest
@testable import NodeProbeCore

final class ProbeTests: XCTestCase {
    func testHostObservationNeverGrantsTrust() {
        let host = HostObservation.collect()
        XCTAssertFalse(host.confidentialEligibility)
        XCTAssertEqual(host.attestation, "not_performed")
        XCTAssertEqual(host.hostConfidentiality, "not_established")
    }

    func testOriginValidation() throws {
        for allowed in ["http://127.0.0.1:8000", "http://localhost:8000", "http://[::1]:8000", "https://example.com"] {
            XCTAssertNoThrow(try validatedBaseURL(allowed))
        }
        for denied in ["http://example.com", "http://127.evil.test", "http://user:secret@localhost", "http://localhost/?token=secret", "http://localhost/path", "file:///etc/passwd"] {
            XCTAssertThrowsError(try validatedBaseURL(denied))
        }
    }

    func testSnapshotCountersAndInvalidBodies() throws {
        let data = Data(#"{"workers":[{},{}],"tasks":[{}],"attempts":[{}],"receipts":[],"credits":{"reserved":10,"settled":0}}"#.utf8)
        let summary = try SnapshotSummary.decode(data)
        XCTAssertEqual(summary.workerCount, 2)
        XCTAssertEqual(summary.reservedCredits, 10)
        XCTAssertThrowsError(try SnapshotSummary.decode(Data("[]".utf8)))
        XCTAssertThrowsError(try SnapshotSummary.decode(Data(#"{"workers":[],"tasks":[],"attempts":[],"receipts":[],"credits":{"reserved":-1,"settled":0}}"#.utf8)))
    }
}
