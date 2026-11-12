// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "App",
    platforms: [.macOS(.v15)],
    products: [
        .library(name: "SymbioApp", targets: ["SymbioApp"]),
        .executable(name: "symbio-bridge", targets: ["symbio-bridge"]),
        .executable(name: "symbio", targets: ["symbio"]),
    ],
    dependencies: [
        .package(path: "../Core"),
        .package(path: "../MCP"),
        .package(path: "../Undo"),
        .package(path: "../Delete"),
        .package(path: "../FileOps"),
        .package(path: "../Search"),
        .package(path: "../Planner"),
        .package(path: "../Feedback"),
        .package(path: "../Runtime"),
    ],
    targets: [
        .target(name: "SymbioApp", dependencies: [
            .product(name: "SymbioCore", package: "Core"), .product(name: "SymbioMCP", package: "MCP"),
            .product(name: "SymbioUndo", package: "Undo"), .product(name: "SymbioDelete", package: "Delete"),
            .product(name: "SymbioFileOps", package: "FileOps"), .product(name: "SymbioSearch", package: "Search"),
            .product(name: "SymbioPlanner", package: "Planner"), .product(name: "SymbioFeedback", package: "Feedback"),
            .product(name: "SymbioRuntime", package: "Runtime"),
        ]),
        .executableTarget(name: "symbio-bridge", dependencies: ["SymbioApp"]),
        .executableTarget(name: "symbio", dependencies: [
            "SymbioApp", .product(name: "SymbioFeedback", package: "Feedback"),
        ]),
        .testTarget(name: "SymbioAppTests", dependencies: [
            "SymbioApp", .product(name: "SymbioCore", package: "Core"), .product(name: "SymbioMCP", package: "MCP"),
            .product(name: "SymbioUndo", package: "Undo"),
            .product(name: "SymbioPlanner", package: "Planner"), .product(name: "SymbioFeedback", package: "Feedback"),
            .product(name: "SymbioRuntime", package: "Runtime"),
        ]),
    ]
)
