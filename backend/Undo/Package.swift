// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "Undo",
    platforms: [.macOS(.v15)],
    products: [.library(name: "SymbioUndo", targets: ["SymbioUndo"])],
    dependencies: [
        .package(path: "../Core"),
    ],
    targets: [
        .target(name: "SymbioUndo", dependencies: [.product(name: "SymbioCore", package: "Core")]),
        .testTarget(name: "SymbioUndoTests", dependencies: ["SymbioUndo"]),
    ]
)
