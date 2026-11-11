// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "Dev",
    platforms: [.macOS(.v15)],
    products: [
        .library(name: "SymbioDev", targets: ["SymbioDev"]),
        .executable(name: "symbio-dev", targets: ["symbio-dev"]),
    ],
    dependencies: [
        .package(path: "../Core"),
    ],
    targets: [
        .target(name: "SymbioDev", dependencies: [.product(name: "SymbioCore", package: "Core")]),
        .executableTarget(name: "symbio-dev", dependencies: ["SymbioDev"]),
        // Depends on the executable so `swift test` builds it next to the test bundle.
        .testTarget(name: "SymbioDevTests", dependencies: ["SymbioDev", "symbio-dev"]),
    ]
)
