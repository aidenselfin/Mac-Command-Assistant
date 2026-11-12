// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "Planner",
    platforms: [.macOS(.v15)],
    products: [.library(name: "SymbioPlanner", targets: ["SymbioPlanner"])],
    dependencies: [
        .package(path: "../Core"),
        .package(path: "../Undo"),
        .package(path: "../Search"),
        .package(path: "../FileOps"),
        .package(path: "../Delete"),
        .package(path: "../Convert"),
        .package(path: "../Finder"),
        .package(path: "../MCP"),
        .package(path: "../Guard"),
    ],
    targets: [
        .target(name: "SymbioPlanner", dependencies: [.product(name: "SymbioCore", package: "Core"), .product(name: "SymbioUndo", package: "Undo"), .product(name: "SymbioSearch", package: "Search"), .product(name: "SymbioFileOps", package: "FileOps"), .product(name: "SymbioDelete", package: "Delete"), .product(name: "SymbioConvert", package: "Convert"), .product(name: "SymbioFinder", package: "Finder"), .product(name: "SymbioMCP", package: "MCP"), .product(name: "SymbioGuard", package: "Guard")]),
        .testTarget(name: "SymbioPlannerTests", dependencies: ["SymbioPlanner"]),
    ]
)
