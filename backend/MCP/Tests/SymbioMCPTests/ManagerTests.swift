import Foundation
import SymbioCore
import Testing
@testable import SymbioMCP

// tests/test_mcp_manager.py

actor FakeMCPClient: ManagedMCPClient {
    var tools: [ToolDefinition]
    var calls: [(String, JSON)] = []
    var validatedPaths: [String] = []
    var normalizedCalls: [(String, JSON)] = []
    var connected = false
    var closed = false
    let failConnect: Bool

    init(_ tools: [ToolDefinition], failConnect: Bool = false) {
        self.tools = tools
        self.failConnect = failConnect
    }

    func add(_ tool: ToolDefinition) { tools.append(tool) }

    func connect() async throws {
        if failConnect { throw ToolError.startup("fake startup failed") }
        connected = true
    }

    func close() async {
        connected = false
        closed = true
    }

    func listTools() async throws -> [ToolDefinition] { tools }

    func callTool(_ name: String, _ arguments: JSON) async throws -> ToolResult {
        calls.append((name, arguments))
        return ToolResult(ok: true, text: "\(name) completed")
    }

    func validateResultPath(_ rawPath: String) async throws -> URL {
        validatedPaths.append(rawPath)
        return URL(fileURLWithPath: rawPath)
    }

    func normalizeToolArguments(_ name: String, _ arguments: JSON) async throws -> JSON {
        normalizedCalls.append((name, arguments))
        var out = arguments
        out.set("normalized", true)
        return out
    }
}

func tool(_ name: String, _ description: String) -> ToolDefinition {
    ToolDefinition(name: name, description: description, schema: [
        "type": "object", "properties": ["path": ["type": "string"]], "required": ["path"],
    ])
}

func fakes() -> (FakeMCPClient, FakeMCPClient) {
    (FakeMCPClient([tool("list_directory", "List a directory")]), FakeMCPClient([tool("example_tool", "Example tool")]))
}

struct CommandFailed: Error {}

@Test(.enabled(if: serverInstalled)) func realFilesystemAndFakeClientShareManager() async throws {
    let root = makeRoot()
    let filesystem = try MCPClient(allowedDirectories: [URL(fileURLWithPath: root)])
    let fake = FakeMCPClient([tool("example_tool", "Example tool")])
    let manager = try MCPManager([("filesystem", filesystem), ("fake", fake)])
    try await manager.withConnection { manager in
        #expect(Set(try await manager.listTools().map(\.name)) == ["filesystem__list_directory", "fake__example_tool"])
        let listing = try await manager.callTool("filesystem__list_directory", ["path": .string(root)])
        let example = try await manager.callTool("fake__example_tool", ["path": "/example"])
        #expect(listing.ok && listing.text.contains("한글 메모.txt"))
        #expect(example.ok)
    }
    await #expect { try await filesystem.listTools() } throws: { _ in true }
    #expect(await fake.closed)
}

@Test func namespacedToolsDispatchAndLifecycle() async throws {
    let (filesystem, fake) = fakes()
    let manager = try MCPManager()
    try await manager.register("filesystem", filesystem)
    try await manager.register("fake", fake)
    try await manager.withConnection { manager in
        let tools = try await manager.listTools()
        #expect(tools.map(\.name) == ["filesystem__list_directory", "fake__example_tool"])
        #expect(tools[0].description == "List a directory")
        #expect(tools[0].schema == (await filesystem.tools)[0].schema)
        #expect(try await manager.callTool("filesystem__list_directory", ["path": "/allowed"]).ok)
        #expect(try await manager.callTool("fake__example_tool", ["path": "/example"]).ok)
        #expect(await filesystem.calls.map(\.0) == ["list_directory"])
        #expect(await filesystem.calls.map(\.1) == [["path": "/allowed"]])
        #expect(await fake.calls.map(\.0) == ["example_tool"])
        #expect(await filesystem.connected)
        #expect(await fake.connected)
    }
    #expect(await filesystem.closed)
    #expect(await fake.closed)
    #expect(await manager.tools.isEmpty)
}

@Test func managerClosesAllClientsOnException() async throws {
    let (filesystem, fake) = fakes()
    let manager = try MCPManager([("filesystem", filesystem), ("fake", fake)])
    await #expect(throws: CommandFailed.self) { try await manager.withConnection { _ in throw CommandFailed() } }
    #expect(await filesystem.closed)
    #expect(await fake.closed)
}

@Test func managerDelegatesCandidateValidationToFilesystem() async throws {
    let (filesystem, fake) = fakes()
    let manager = try MCPManager([("filesystem", filesystem), ("fake", fake)])
    let result = try await manager.withConnection { try await $0.validateResultPath("/allowed/candidate.md") }
    #expect(result.path == "/allowed/candidate.md")
    #expect(await filesystem.validatedPaths == ["/allowed/candidate.md"])
    #expect(await fake.validatedPaths.isEmpty)
}

@Test func managerDelegatesNamespacedArgumentNormalization() async throws {
    let (filesystem, fake) = fakes()
    let manager = try MCPManager([("filesystem", filesystem), ("fake", fake)])
    let result = try await manager.withConnection {
        try await $0.normalizeToolArguments("filesystem__list_directory", ["path": "/allowed"])
    }
    #expect(result == ["path": "/allowed", "normalized": true])
    #expect(await filesystem.normalizedCalls.map(\.0) == ["list_directory"])
    #expect(await fake.normalizedCalls.isEmpty)
}

@Test func secondClientStartupFailureClosesFirstClient() async throws {
    let (filesystem, _) = fakes()
    let failing = FakeMCPClient([tool("example_tool", "Example tool")], failConnect: true)
    let manager = try MCPManager([("filesystem", filesystem), ("web", failing)])
    await #expect { try await manager.withConnection { _ in Issue.record("must not yield") } } throws: {
        ($0 as? ToolError)?.message.contains("startup failed") == true
    }
    #expect(await filesystem.closed)
}

@Test func namespacedToolCollisionFailsStartupAndClosesClients() async throws {
    let (filesystem, fake) = fakes()
    await filesystem.add(tool("list_directory", "Duplicate"))
    let manager = try MCPManager([("filesystem", filesystem), ("fake", fake)])
    await #expect { try await manager.withConnection { _ in Issue.record("must not yield") } } throws: {
        ($0 as? ToolError)?.message.contains("충돌") == true
    }
    #expect(await filesystem.closed)
    #expect(await fake.closed)
}

@Test(arguments: ["list_directory", "unknown__list_directory", "filesystem__missing_tool"])
func invalidNamespacedToolIsRejectedBeforeClientCall(name: String) async throws {
    let (filesystem, fake) = fakes()
    let manager = try MCPManager([("filesystem", filesystem), ("fake", fake)])
    try await manager.withConnection { manager in
        await #expect { try await manager.callTool(name, ["path": "/allowed"]) } throws: {
            if case .notFound = $0 as? ToolError { true } else { false }
        }
    }
    #expect(await filesystem.calls.isEmpty)
    #expect(await fake.calls.isEmpty)
}

@Test func registerRejectsBadAndDuplicateNames() async throws {
    let (filesystem, fake) = fakes()
    #expect(throws: ToolError.self) { try MCPManager([("a__b", filesystem)]) }
    #expect(throws: ToolError.self) { try MCPManager([("", filesystem)]) }
    #expect(throws: ToolError.self) { try MCPManager([("x", filesystem), ("x", fake)]) }
}
