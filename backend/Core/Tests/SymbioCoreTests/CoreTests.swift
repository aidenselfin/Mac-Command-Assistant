import Foundation
import Testing
@testable import SymbioCore

@Test func dumpsMatchesPython() throws {
    let value: JSON = ["a": "한", "b": [1, 2.5, nil, true], "c": [:]]
    #expect(value.dumps() == #"{"a": "\ud55c", "b": [1, 2.5, null, true], "c": {}}"#)
    #expect(value.text == #"{"a": "한", "b": [1, 2.5, null, true], "c": {}}"#)
    #expect(JSON.object([("b", 1), ("a", 2)]).dumps(.init(sortKeys: true, compact: true)) == #"{"a":2,"b":1}"#)
    #expect(JSON.array([1, ["x": 2.0]]).dumps(.init(indent: 2)) == "[\n  1,\n  {\n    \"x\": 2.0\n  }\n]")
}

@Test func parseKeepsOrderAndRoundTrips() throws {
    let text = #"{"z": 1, "a": [true, null, -1.5e3, "한\n"], "m": {}}"#
    let value = try JSON.parse(text)
    #expect(value.objectValue?.map(\.0) == ["z", "a", "m"])
    #expect(value["a"]?.arrayValue?[3] == "한\n")
    #expect(try JSON.parse(value.dumps()) == value)
    #expect(throws: JSONParseError.self) { try JSON.parse("{\"a\": }") }
}

@Test func schemaSubset() {
    let schema: JSON = [
        "type": "object", "required": ["path"], "additionalProperties": false,
        "properties": ["path": ["type": "string", "minLength": 1], "n": ["type": "integer", "minimum": 1]],
    ]
    let definition = ToolDefinition(name: "t", description: "", schema: schema)
    #expect((try? validateArguments(definition, ["path": "/a"])) != nil)
    #expect(throws: ToolError.rejected("잘못된 t 인자입니다.")) { try validateArguments(definition, ["path": ""]) }
    #expect(throws: ToolError.self) { try validateArguments(definition, ["path": "/a", "x": 1]) }
    #expect(throws: ToolError.self) { try validateArguments(definition, ["path": "/a", "n": true]) }
}

@Test func boundedJoinCuts() {
    #expect(boundedJoin(["ab", "cd"], limit: 3) == ("abc", true))
    #expect(boundedJoin(["ab", "cd"], limit: 4) == ("abcd", false))
}

@Test func insideRejectsEscapesAndLinks() throws {
    let root = URL(fileURLWithPath: realPath(NSTemporaryDirectory())!).appendingPathComponent(UUID().uuidString)
    try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
    defer { try? FileManager.default.removeItem(at: root) }
    let file = root.appendingPathComponent("a.txt")
    try Data("x".utf8).write(to: file)
    let link = root.appendingPathComponent("link.txt")
    try FileManager.default.createSymbolicLink(at: link, withDestinationURL: file)
    #expect(try inside(.string(file.path), roots: [root], exists: true).path == file.path)
    #expect(try inside(.string(root.appendingPathComponent("new.txt").path), roots: [root], exists: false).path.hasSuffix("new.txt"))
    #expect(throws: ToolError.self) { try inside(.string(link.path), roots: [root], exists: true) }
    #expect(throws: ToolError.self) { try inside(.string("/etc/hosts"), roots: [root], exists: true) }
    #expect(throws: ToolError.self) { try inside(.string(" " + file.path), roots: [root], exists: true) }
    #expect(throws: ToolError.self) { try inside(.int(1), roots: [root], exists: true) }
}
