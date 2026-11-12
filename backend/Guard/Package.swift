// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "Guard",
    platforms: [.macOS(.v15)],
    products: [.library(name: "SymbioGuard", targets: ["SymbioGuard"])],
    dependencies: [
        .package(path: "../Core"),
        .package(path: "../Undo"),
        .package(path: "../Search"),
        .package(path: "../FileOps"),
        .package(path: "../Delete"),
        .package(path: "../Convert"),
        .package(path: "../Finder"),
        .package(path: "../MCP"),
    ],
    targets: [
        .target(name: "SymbioGuard", dependencies: [.product(name: "SymbioCore", package: "Core"), .product(name: "SymbioUndo", package: "Undo"), .product(name: "SymbioSearch", package: "Search"), .product(name: "SymbioFileOps", package: "FileOps"), .product(name: "SymbioDelete", package: "Delete"), .product(name: "SymbioConvert", package: "Convert"), .product(name: "SymbioFinder", package: "Finder"), .product(name: "SymbioMCP", package: "MCP")]),
        .testTarget(name: "SymbioGuardTests", dependencies: ["SymbioGuard"]),
    ]
)
