// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "FileOps",
    platforms: [.macOS(.v15)],
    products: [.library(name: "SymbioFileOps", targets: ["SymbioFileOps"])],
    dependencies: [
        .package(path: "../Core"),
        .package(path: "../Undo"),
    ],
    targets: [
        .target(name: "SymbioFileOps", dependencies: [.product(name: "SymbioCore", package: "Core"), .product(name: "SymbioUndo", package: "Undo")]),
        .testTarget(name: "SymbioFileOpsTests", dependencies: ["SymbioFileOps"]),
    ]
)
