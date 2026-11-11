// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "Core",
    platforms: [.macOS(.v15)],
    products: [.library(name: "SymbioCore", targets: ["SymbioCore"])],
    targets: [
        .target(name: "SymbioCore"),
        .testTarget(name: "SymbioCoreTests", dependencies: ["SymbioCore"]),
    ]
)
