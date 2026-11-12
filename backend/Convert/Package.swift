// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "Convert",
    platforms: [.macOS(.v15)],
    products: [.library(name: "SymbioConvert", targets: ["SymbioConvert"])],
    dependencies: [
        .package(path: "../Core"),
    ],
    targets: [
        .target(name: "SymbioConvert", dependencies: [.product(name: "SymbioCore", package: "Core")]),
        .testTarget(name: "SymbioConvertTests", dependencies: ["SymbioConvert"]),
    ]
)
