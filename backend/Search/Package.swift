// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "Search",
    platforms: [.macOS(.v15)],
    products: [.library(name: "SymbioSearch", targets: ["SymbioSearch"])],
    dependencies: [
        .package(path: "../Core"),
    ],
    targets: [
        .target(name: "SymbioSearch", dependencies: [.product(name: "SymbioCore", package: "Core")]),
        .testTarget(name: "SymbioSearchTests", dependencies: ["SymbioSearch"]),
    ]
)
