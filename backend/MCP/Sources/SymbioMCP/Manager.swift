import Foundation
import SymbioCore

/// Python `voice_action/mcp_manager.py`: lifecycle and `서버__도구` namespaced dispatch for several clients.
public actor MCPManager: ToolGateway {
    private var clients: [(String, any ManagedMCPClient)] = []
    private var connected: [(String, any ManagedMCPClient)] = []
    private var routes: [String: (any ManagedMCPClient, String)] = [:]
    private var connecting = false
    private let lock = AsyncLock()
    public private(set) var tools: [ToolDefinition] = []

    public init(_ clients: [(String, any ManagedMCPClient)] = []) throws {
        for (name, client) in clients {
            try Self.check(name, self.clients)
            self.clients.append((name, client))
        }
    }

    public func register(_ name: String, _ client: any ManagedMCPClient) throws {
        if connecting { throw ToolError.connection("연결 중에는 MCP를 등록할 수 없습니다.") }
        try Self.check(name, clients)
        clients.append((name, client))
    }

    private static func check(_ name: String, _ clients: [(String, any ManagedMCPClient)]) throws {
        if name.isEmpty || name.contains("__") {
            throw ToolError.other("MCP 이름은 비어 있지 않고 '__'를 포함하지 않아야 합니다.")
        }
        if clients.contains(where: { $0.0 == name }) { throw ToolError.other("MCP 이름이 중복되었습니다: \(name)") }
    }

    /// Connect every client in order; any failure closes the ones already up.
    public func connect() async throws {
        if connecting { throw ToolError.connection("MCP manager가 이미 연결 중이거나 연결되어 있습니다.") }
        connecting = true
        var up: [(String, any ManagedMCPClient)] = []
        do {
            for (name, client) in clients {
                try await client.connect()
                up.append((name, client))
            }
            var tools: [ToolDefinition] = []
            var routes: [String: (any ManagedMCPClient, String)] = [:]
            for (namespace, client) in up {
                for tool in try await client.listTools() {
                    let name = "\(namespace)__\(tool.name)"
                    if routes[name] != nil { throw ToolError.startup("namespaced MCP 도구 이름이 충돌했습니다: \(name)") }
                    routes[name] = (client, tool.name)
                    tools.append(ToolDefinition(name: name, description: tool.description, schema: tool.schema))
                }
            }
            connected = up
            self.routes = routes
            self.tools = tools
        } catch {
            connected = up
            await close()
            throw error
        }
    }

    public func close() async {
        for (_, client) in connected.reversed() { await client.close() }
        connected = []
        routes = [:]
        tools = []
        connecting = false
    }

    /// Python `async with manager.connect():` — always closes, even when `body` throws.
    @discardableResult
    public nonisolated func withConnection<T: Sendable>(_ body: @Sendable (MCPManager) async throws -> T) async throws -> T {
        try await connect()
        do {
            let result = try await body(self)
            await close()
            return result
        } catch {
            await close()
            throw error
        }
    }

    public func listTools() async throws -> [ToolDefinition] {
        if connected.isEmpty { throw ToolError.connection("MCP manager가 연결되어 있지 않습니다.") }
        return tools
    }

    /// Delegate search-candidate validation to the Filesystem MCP boundary.
    public func validateResultPath(_ rawPath: String) async throws -> URL {
        if connected.isEmpty { throw ToolError.connection("MCP manager가 연결되어 있지 않습니다.") }
        guard let filesystem = connected.first(where: { $0.0 == "filesystem" })?.1 else {
            throw ToolError.connection("filesystem MCP가 경로 재검증을 지원하지 않습니다.")
        }
        return try await filesystem.validateResultPath(rawPath)
    }

    /// Delegate non-executing argument validation to the routed MCP client.
    public func normalizeToolArguments(_ name: String, _ arguments: JSON) async throws -> JSON {
        if connected.isEmpty { throw ToolError.connection("MCP manager가 연결되어 있지 않습니다.") }
        guard let (client, original) = routes[name] else {
            throw ToolError.notFound("MCP namespace에 존재하지 않는 도구입니다: \(name)")
        }
        return try await client.normalizeToolArguments(original, arguments)
    }

    public func callTool(_ name: String, _ arguments: JSON) async throws -> ToolResult {
        await lock.acquire()
        do {
            let result = try await route(name, arguments)
            await lock.release()
            return result
        } catch {
            await lock.release()
            throw error
        }
    }

    private func route(_ name: String, _ arguments: JSON) async throws -> ToolResult {
        if connected.isEmpty { throw ToolError.connection("MCP manager가 연결되어 있지 않습니다.") }
        guard let range = name.range(of: "__"), range.lowerBound != name.startIndex, range.upperBound != name.endIndex
        else { throw ToolError.notFound("MCP namespace가 없는 도구 이름입니다.") }
        let namespace = String(name[..<range.lowerBound])
        if !connected.contains(where: { $0.0 == namespace }) {
            throw ToolError.notFound("등록되지 않은 MCP namespace입니다: \(namespace)")
        }
        guard let (client, original) = routes[name] else {
            throw ToolError.notFound("MCP namespace에 존재하지 않는 도구입니다: \(name)")
        }
        return try await client.callTool(original, arguments)
    }
}
