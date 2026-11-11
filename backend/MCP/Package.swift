// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "MCP",
    platforms: [.macOS(.v15)],
    products: [.library(name: "SymbioMCP", targets: ["SymbioMCP"])],
    dependencies: [
        .package(path: "../Core"),
    ],
    targets: [
        .target(name: "SymbioMCP", dependencies: [.product(name: "SymbioCore", package: "Core")]),
        .testTarget(name: "SymbioMCPTests", dependencies: ["SymbioMCP"]),
    ]
)
