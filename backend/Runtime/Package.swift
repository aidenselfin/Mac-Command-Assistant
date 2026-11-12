// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "Runtime",
    platforms: [.macOS(.v15)],
    products: [.library(name: "SymbioRuntime", targets: ["SymbioRuntime"])],
    dependencies: [
        .package(path: "../Core"),
        .package(path: "../Planner"),
    ],
    targets: [
        .target(name: "SymbioRuntime", dependencies: [.product(name: "SymbioCore", package: "Core"), .product(name: "SymbioPlanner", package: "Planner")]),
        .testTarget(name: "SymbioRuntimeTests", dependencies: ["SymbioRuntime"]),
    ]
)
