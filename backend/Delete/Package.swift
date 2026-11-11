// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "Delete",
    platforms: [.macOS(.v15)],
    products: [.library(name: "SymbioDelete", targets: ["SymbioDelete"])],
    dependencies: [
        .package(path: "../Core"),
        .package(path: "../Undo"),
    ],
    targets: [
        .target(name: "SymbioDelete", dependencies: [.product(name: "SymbioCore", package: "Core"), .product(name: "SymbioUndo", package: "Undo")]),
        .testTarget(name: "SymbioDeleteTests", dependencies: ["SymbioDelete"]),
    ]
)
