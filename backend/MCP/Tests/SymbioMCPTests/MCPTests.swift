import Foundation
import SymbioCore
import Testing
@testable import SymbioMCP

// MARK: - Fixtures (Python conftest `config` + tests/test_mcp_client.py helpers)

func makeRoot() -> String {
    let url = FileManager.default.temporaryDirectory.appendingPathComponent("symbio-mcp-\(UUID().uuidString)")
    try! FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
    try! "hello".write(to: url.appendingPathComponent("한글 메모.txt"), atomically: true, encoding: .utf8)
    return realPath(url.path)!
}

let pathSchema: JSON = [
    "type": "object", "properties": ["path": ["type": "string"]], "required": ["path"], "additionalProperties": false,
]

actor FakeSession: MCPSession {
    var calls: [(String, JSON)] = []
    var listCursors: [String?] = []
    var inFlight = 0
    var maxInFlight = 0
    let pages: [JSON]
    let behavior: @Sendable () async throws -> JSON

    init(pages: [JSON] = [], behavior: @escaping @Sendable () async throws -> JSON = { ["content": []] }) {
        self.pages = pages
        self.behavior = behavior
    }

    func listTools(cursor: String?) async throws -> JSON {
        listCursors.append(cursor)
        return pages[min(listCursors.count - 1, pages.count - 1)]
    }

    func callTool(_ name: String, _ arguments: JSON) async throws -> JSON {
        calls.append((name, arguments))
        inFlight += 1
        maxInFlight = max(maxInFlight, inFlight)
        defer { inFlight -= 1 }
        return try await behavior()
    }

    func close() async {}
}

func client(
    _ root: String, tool: String = "list_directory", schema: JSON = pathSchema, timeout: Double = 30,
    session: FakeSession = FakeSession()
) async throws -> MCPClient {
    let mcp = try MCPClient(allowedDirectories: [URL(fileURLWithPath: root)], timeoutSeconds: timeout, allowedTools: [tool])
    await mcp.install(session: session, tools: [ToolDefinition(name: tool, description: tool, schema: schema)], roots: [root])
    return mcp
}

func rejected(_ error: any Error, containing text: String = "") -> Bool {
    guard let error = error as? ToolError else { return false }
    return error.isRejected && (text.isEmpty || error.message.contains(text))
}

// MARK: - tests/test_mcp_client.py

@Test(arguments: [
    ("delete_file", [:]), ("move_file", [:]), ("write_file", [:]), ("list_directory", [:]),
    ("list_directory", ["path": 12]),
] as [(String, JSON)])
func rejectedBeforeMCP(name: String, args: JSON) async throws {
    let session = FakeSession()
    let mcp = try await client(makeRoot(), session: session)
    await #expect { try await mcp.callTool(name, args) } throws: { rejected($0) }
    #expect(await session.calls.isEmpty)
}

@Test func relativeEmptyAndWhitespaceWrappedPathsAreRejected() async throws {
    let root = makeRoot()
    let session = FakeSession()
    let mcp = try await client(root, session: session)
    for path in ["relative", "", " \(root) "] {
        await #expect { try await mcp.callTool("list_directory", ["path": .string(path)]) } throws: {
            rejected($0, containing: "절대경로")
        }
    }
    #expect(await session.calls.isEmpty)
}

@Test func symlinkEscape() async throws {
    let root = makeRoot(), outside = makeRoot()
    try FileManager.default.createSymbolicLink(atPath: root + "/escape", withDestinationPath: outside)
    let session = FakeSession()
    let mcp = try await client(root, session: session)
    await #expect { try await mcp.callTool("list_directory", ["path": .string(root + "/escape")]) } throws: {
        rejected($0, containing: "허용 경로 밖")
    }
    #expect(await session.calls.isEmpty)
}

@Test func writeNormalizationReusesSchemaAndCanonicalRootBoundary() async throws {
    let root = makeRoot(), outside = makeRoot()
    let schema: JSON = [
        "type": "object", "properties": ["path": ["type": "string"], "content": ["type": "string"]],
        "required": ["path", "content"], "additionalProperties": false,
    ]
    let session = FakeSession()
    let mcp = try await client(root, tool: "write_file", schema: schema, session: session)
    try FileManager.default.createDirectory(atPath: root + "/nested", withIntermediateDirectories: false)

    let normalized = try await mcp.normalizeToolArguments(
        "write_file", ["path": .string(root + "/nested/../new.txt"), "content": "exact"])
    #expect(normalized == ["path": .string(root + "/new.txt"), "content": "exact"])
    await #expect { try await mcp.normalizeToolArguments("write_file", ["path": .string(outside + "/o.txt"), "content": "x"]) }
        throws: { rejected($0) }
    await #expect { try await mcp.normalizeToolArguments("write_file", ["path": .string(root), "content": "x"]) }
        throws: { rejected($0, containing: "regular file") }
    await #expect {
        try await mcp.normalizeToolArguments("write_file", ["path": .string(root + "/missing/new.txt"), "content": "x"])
    } throws: { rejected($0, containing: "부모") }
    #expect(await session.calls.isEmpty)
}

@Test func resultNormalization() async throws {
    let root = makeRoot()
    let session = FakeSession {
        ["content": [["type": "text", "text": "abc"], ["type": "text", "text": "def"]], "isError": true,
         "structuredContent": ["error": "denied"]]
    }
    let mcp = try await client(root, session: session)
    let result = try await mcp.callTool("list_directory", ["path": .string(root)])
    #expect(!result.ok)
    #expect(result.text == "abc\ndef")
    #expect(result.structured == ["error": "denied"])
    #expect(result.error == "abc\ndef")
    #expect(!result.truncated)
}

@Test func timeoutNoRetry() async throws {
    let root = makeRoot()
    let session = FakeSession { try await Task.sleep(for: .seconds(1)); return [:] }
    let mcp = try await client(root, timeout: 0.01, session: session)
    await #expect { try await mcp.callTool("list_directory", ["path": .string(root)]) } throws: {
        ($0 as? ToolError)?.message.contains("시간 초과") == true
    }
    #expect(await session.calls.count == 1)
    await #expect { try await mcp.callTool("list_directory", ["path": .string(root)]) } throws: {
        if case .connection = $0 as? ToolError { true } else { false }
    }
    #expect(await session.calls.count == 1)
}

@Test(arguments: [("abc", 3, "abc"), ("abcd", 3, "abc\n[TRUNCATED]"), ("한글😀이름", 3, "한글😀\n[TRUNCATED]")])
func textTruncation(text: String, limit: Int, expected: String) {
    let result = normalizeResult(["content": [["type": "text", "text": .string(text)]]], limit: limit)
    #expect(result.text == expected)
    #expect(result.truncated == (text.count > limit))
}

@Test func structuredPayloadCannotBypassLimit() {
    let secret = String(repeating: "secret", count: 1000)
    let result = normalizeResult(
        ["content": [["type": "text", "text": "ok"]], "structuredContent": ["data": .string(secret)]], limit: 20)
    #expect(result.structured == nil)
    #expect(result.text == "ok\n[TRUNCATED]")
    #expect(result.truncated)
}

@Test func structuredOnlyAndNonText() {
    var result = normalizeResult(["content": [], "structuredContent": ["number": 2]], limit: 100)
    #expect(result.text == #"{"number": 2}"#)
    #expect(result.structured == ["number": 2])
    result = normalizeResult(["content": [["type": "image", "data": "not-for-planner"]]], limit: 100)
    #expect(result.text == "[UNSUPPORTED:image]")
}

@Test func dynamicPaginatedDiscovery() async throws {
    let schema: JSON = ["type": "object", "properties": ["path": ["type": "string"]], "required": ["path"]]
    func tool(_ name: String) -> JSON { ["name": .string(name), "description": "runtime description", "inputSchema": schema] }
    let session = FakeSession(pages: [
        ["tools": [tool("move_file"), tool("list_directory")], "nextCursor": "next"],
        ["tools": [tool("read_text_file"), tool("unknown_new_tool")], "nextCursor": nil],
    ])
    let root = makeRoot()
    let mcp = try MCPClient(allowedDirectories: [URL(fileURLWithPath: root)], allowedTools: ["list_directory", "read_text_file"])
    let tools = try await mcp.discover(session)
    await mcp.install(session: session, tools: tools, roots: [root])
    let snapshot = try await mcp.listTools()
    #expect(Set(snapshot.map(\.name)) == ["list_directory", "read_text_file"])
    #expect(snapshot[0].description == "runtime description")
    #expect(await session.listCursors == [nil, "next"])
}

@Test(arguments: ["missing", "duplicate", "schema", "pagination"])
func invalidDiscoveryFailsClosed(failure: String) async throws {
    var tool: JSON = ["name": "list_directory", "inputSchema": pathSchema]
    var page: JSON = ["tools": [tool], "nextCursor": nil]
    switch failure {
    case "missing": page.set("tools", [])
    case "duplicate": page.set("tools", [tool, tool])
    case "schema": tool.set("inputSchema", ["type": "object"]); page.set("tools", [tool])
    default: page = ["tools": [], "nextCursor": "same"]
    }
    let mcp = try MCPClient(allowedDirectories: [URL(fileURLWithPath: makeRoot())])
    await #expect { try await mcp.discover(FakeSession(pages: [page])) } throws: {
        if case .startup = $0 as? ToolError { true } else { false }
    }
}

@Test func unsupportedAllowlist() {
    #expect { try MCPClient(allowedDirectories: [], allowedTools: ["move_file"]) } throws: { rejected($0) }
}

@Test func sdkTimeoutIsNotRetried() async throws {
    let root = makeRoot()
    let session = FakeSession { throw RPCError(code: 408, message: "timeout") }
    let mcp = try await client(root, session: session)
    await #expect { try await mcp.callTool("list_directory", ["path": .string(root)]) } throws: {
        if case .timeout = $0 as? ToolError { true } else { false }
    }
    await #expect { try await mcp.callTool("list_directory", ["path": .string(root)]) } throws: {
        if case .connection = $0 as? ToolError { true } else { false }
    }
    #expect(await session.calls.count == 1)
}

@Test func toolCallsAreSerialized() async throws {
    let root = makeRoot()
    let session = FakeSession { try await Task.sleep(for: .milliseconds(10)); return ["content": []] }
    let mcp = try await client(root, session: session)
    try await withThrowingTaskGroup(of: ToolResult.self) { group in
        for _ in 0..<3 { group.addTask { try await mcp.callTool("list_directory", ["path": .string(root)]) } }
        try await group.waitForAll()
    }
    #expect(await session.calls.count == 3)
    #expect(await session.maxInFlight == 1)
}

@Test func readOptionalNullsAreOmittedWithoutRelaxingValidation() async throws {
    let root = makeRoot()
    let schema: JSON = [
        "type": "object",
        "properties": ["path": ["type": "string"], "head": ["type": "integer", "minimum": 0],
                       "tail": ["type": "integer", "minimum": 0]],
        "required": ["path"], "additionalProperties": false,
    ]
    let session = FakeSession()
    let mcp = try await client(root, tool: "read_text_file", schema: schema, session: session)
    let path = root + "/source.txt"
    try "first\nsecond\n".write(toFile: path, atomically: true, encoding: .utf8)
    for (options, expected) in [([], []), ([("head", .null), ("tail", .null)], []),
                                ([("head", 1), ("tail", .null)], [("head", 1)])] as [([(String, JSON)], [(String, JSON)])] {
        let args = JSON.object([("path", .string(path))] + options)
        #expect(try await mcp.normalizeToolArguments("read_text_file", args) == .object([("path", .string(path))] + expected))
    }
    for invalid in [("head", "1"), ("tail", -1), ("unknown", .null), ("path", .null)] as [(String, JSON)] {
        var args: JSON = ["path": .string(path)]
        args.set(invalid.0, invalid.1)
        await #expect { try await mcp.normalizeToolArguments("read_text_file", args) } throws: { rejected($0) }
    }
    #expect(await session.calls.isEmpty)
}

@Test func findNodeWorksWithMinimalPath() {
    #expect(findNode(path: "/usr/bin:/bin").hasSuffix("node"))
}

@Test func resultPathMustBeFileInsideRoot() async throws {
    let root = makeRoot()
    let mcp = try await client(root)
    #expect(try await mcp.validateResultPath(root + "/한글 메모.txt").path == root + "/한글 메모.txt")
    await #expect { try await mcp.validateResultPath(root) } throws: { rejected($0, containing: "파일") }
}

// MARK: - Real Node filesystem server (skipped when node_modules is missing)

let serverInstalled = FileManager.default.fileExists(atPath: serverEntrypoint.path)

@Test(.enabled(if: serverInstalled)) func realServerConnectListAndClose() async throws {
    let root = makeRoot()
    let mcp = try MCPClient(allowedDirectories: [URL(fileURLWithPath: root)], allowedTools: ["list_directory", "read_text_file"])
    try await mcp.connect()
    #expect(Set(try await mcp.listTools().map(\.name)) == ["list_directory", "read_text_file"])
    let listing = try await mcp.callTool("list_directory", ["path": .string(root)])
    #expect(listing.ok && listing.text.contains("한글 메모.txt"))
    let read = try await mcp.callTool("read_text_file", ["path": .string(root + "/한글 메모.txt"), "head": nil])
    #expect(read.ok && read.text.contains("hello"))
    await mcp.close()
    await #expect { try await mcp.listTools() } throws: { _ in true }
}
