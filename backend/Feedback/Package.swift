// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "Feedback",
    platforms: [.macOS(.v15)],
    products: [.library(name: "SymbioFeedback", targets: ["SymbioFeedback"])],
    dependencies: [
        .package(path: "../Core"),
        .package(path: "../Planner"),
        .package(path: "../Undo"),
    ],
    targets: [
        .target(name: "SymbioFeedback", dependencies: [.product(name: "SymbioCore", package: "Core"), .product(name: "SymbioPlanner", package: "Planner"), .product(name: "SymbioUndo", package: "Undo")]),
        .testTarget(name: "SymbioFeedbackTests", dependencies: ["SymbioFeedback"]),
    ]
)
