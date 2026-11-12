import Foundation
import Testing

/// tests/test_native_protocol.py: the Swift bridge, the app's receiver and docs/native-protocol.md
/// must list the same messages.

private func source(_ path: String) throws -> String {
    try String(contentsOf: repoRoot.appendingPathComponent(path), encoding: .utf8)
}

private func matches(_ pattern: String, in text: String) -> [String] {
    let regex = try! NSRegularExpression(pattern: pattern)
    return regex.matches(in: text, range: NSRange(text.startIndex..., in: text)).map {
        String(text[Range($0.range(at: 1), in: text)!])
    }
}

private let bridgePath = "backend/App/Sources/SymbioApp/Bridge.swift"
private let receiverPath = "native-preview/Sources/FinderChatPreview/Backend.swift"
private let docPath = "docs/native-protocol.md"

@Test func everyEventTheBridgeSendsIsHandledBySwiftAndDocumented() throws {
    let bridge = try source(bridgePath), swift = try source(receiverPath), doc = try source(docPath)
    let sent = Set(matches(#""type": "([a-z_]+)""#, in: bridge))
    let handled = Set(matches(#"case ((?:"[a-z_]+"(?:, )?)+):"#, in: swift).flatMap { matches(#""([a-z_]+)""#, in: $0) })
    #expect(!sent.isEmpty, "no events found: the pattern needs updating")
    #expect(sent.isSubset(of: handled), "Swift ignores: \(sent.subtracting(handled).sorted())")
    #expect(sent.allSatisfy { doc.contains("`\($0)`") }, "docs/native-protocol.md misses an event")
}

@Test func everyCommandTheBridgeAcceptsIsDocumented() throws {
    let bridge = try source(bridgePath), doc = try source(docPath)
    let body = String(bridge[bridge.range(of: "public func command(")!.lowerBound...])
    let handler = String(body[..<body.range(of: "func transcribeClip(")!.lowerBound])
    let actions = Set(matches(#"action == "([a-z_]+)""#, in: handler) + matches(#"case "([a-z_]+)":"#, in: handler))
    let known: Set = ["submit", "approve", "reject", "cancel", "undo", "attach", "detach", "shutdown", "voice", "reset",
                      "quick_list"]
    #expect(known.isSubset(of: actions), "missing: \(known.subtracting(actions).sorted())")
    #expect(actions.allSatisfy { doc.contains("`\($0)`") }, "docs/native-protocol.md misses a command")
}

@Test func everyApprovalFieldTheBridgeSendsIsDecodedBySwiftAndDocumented() throws {
    let bridge = try source(bridgePath), swift = try source(receiverPath), doc = try source(docPath)
    var block = String(bridge[bridge.range(of: #""type": "approval""#)!.lowerBound...])
    block = String(block[..<block.range(of: "])")!.lowerBound])
    let fields = Set(matches(#""([a-z_]+)":"#, in: block)).subtracting(["type"])
    var structure = String(swift[swift.range(of: "struct Approval")!.lowerBound...])
    structure = String(structure[..<structure.range(of: "}")!.lowerBound])
    let decoded = Set(matches(#"let ([a-z_]+):"#, in: structure))
    #expect(fields.contains("previews"))
    #expect(fields.isSubset(of: decoded), "Swift Approval misses: \(fields.subtracting(decoded).sorted())")
    let row = try #require(doc.split(separator: "\n").first { $0.hasPrefix("| `approval`") })
    #expect(fields.allSatisfy { row.contains("`\($0)`") }, "approval row misses a field")
}
