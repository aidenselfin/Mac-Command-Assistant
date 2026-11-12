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
        .package(path: "../MCP"),
        .package(path: "../Undo"),
        .package(path: "../Planner"),
        .package(path: "../Feedback"),
        .package(path: "../Runtime"),
        .package(path: "../Finder"),
    ],
    targets: [
        .target(name: "SymbioDev", dependencies: [
            .product(name: "SymbioCore", package: "Core"), .product(name: "SymbioMCP", package: "MCP"),
            .product(name: "SymbioUndo", package: "Undo"), .product(name: "SymbioPlanner", package: "Planner"),
            .product(name: "SymbioFeedback", package: "Feedback"), .product(name: "SymbioRuntime", package: "Runtime"),
            .product(name: "SymbioFinder", package: "Finder"),
        ]),
        .executableTarget(name: "symbio-dev", dependencies: ["SymbioDev"]),
        // Depends on the executable so `swift test` builds it next to the test bundle.
        .testTarget(name: "SymbioDevTests", dependencies: [
            "SymbioDev", "symbio-dev", .product(name: "SymbioCore", package: "Core"),
            .product(name: "SymbioPlanner", package: "Planner"),
        ]),
    ]
)
