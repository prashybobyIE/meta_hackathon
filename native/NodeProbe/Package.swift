// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "NodeProbe",
    platforms: [.macOS(.v13)],
    products: [.executable(name: "node-probe", targets: ["NodeProbe"])],
    targets: [
        .target(name: "NodeProbeCore"),
        .executableTarget(name: "NodeProbe", dependencies: ["NodeProbeCore"]),
        .testTarget(name: "NodeProbeCoreTests", dependencies: ["NodeProbeCore"]),
    ]
)
