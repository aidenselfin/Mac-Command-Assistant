// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "Finder",
    platforms: [.macOS(.v15)],
    products: [.library(name: "SymbioFinder", targets: ["SymbioFinder"])],
    dependencies: [
        .package(path: "../Core"),
    ],
    targets: [
        .target(name: "SymbioFinder", dependencies: [.product(name: "SymbioCore", package: "Core")]),
        .testTarget(name: "SymbioFinderTests", dependencies: ["SymbioFinder"]),
    ]
)
