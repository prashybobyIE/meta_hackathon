import Foundation
import NodeProbeCore

struct Output: Encodable {
    let schemaVersion = 1
    let host: HostObservation
    let network: NetworkObservation?
}

@main
struct NodeProbe {
    static func main() async {
        do {
            let args = Array(CommandLine.arguments.dropFirst())
            if args == ["--help"] || args == ["-h"] {
                print("Usage: node-probe [--market http://127.0.0.1:PORT]\nRead-only host observations and public marketplace counts. Never establishes confidential eligibility.")
                return
            }
            let market: URL?
            if args.isEmpty { market = nil }
            else if args.count == 2 && args[0] == "--market" { market = try validatedBaseURL(args[1]) }
            else { throw ProbeFailure("Usage: node-probe [--market http://127.0.0.1:PORT]") }
            let network: NetworkObservation?
            if let market {
                network = try await MarketplaceClient().observe(baseURL: market)
            } else {
                network = nil
            }
            let encoder = JSONEncoder()
            encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
            let data = try encoder.encode(Output(host: .collect(), network: network))
            print(String(decoding: data, as: UTF8.self))
        } catch {
            // Avoid echoing server bodies, URLs, or credentials in errors.
            FileHandle.standardError.write(Data("node-probe: request or argument validation failed; use --help.\n".utf8))
            exit(1)
        }
    }
}
