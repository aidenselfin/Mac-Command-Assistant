// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "Runtime",
    platforms: [.macOS(.v15)],
    products: [.library(name: "SymbioRuntime", targets: ["SymbioRuntime"])],
    dependencies: [
        .package(path: "../Core"),
        .package(path: "../Planner"),
        .package(path: "../MCP"),  // tests only: ToolGateway for the fake gateway
        .package(path: "../Undo"),  // tests only: the real planner's undo journal
    ],
    targets: [
        .target(name: "SymbioRuntime", dependencies: [.product(name: "SymbioCore", package: "Core"), .product(name: "SymbioPlanner", package: "Planner")]),
        .testTarget(name: "SymbioRuntimeTests", dependencies: [
            "SymbioRuntime", .product(name: "SymbioCore", package: "Core"), .product(name: "SymbioPlanner", package: "Planner"),
            .product(name: "SymbioMCP", package: "MCP"), .product(name: "SymbioUndo", package: "Undo"),
        ]),
    ]
)
