import Foundation
import SymbioCore

/// Python `voice_action/mcp_client.py`: session-scoped Filesystem MCP transport and execution boundary.

/// Python `planner.ToolGateway`: what Guard and Planner need from the MCP layer.
public protocol ToolGateway: Sendable {
    func listTools() async throws -> [ToolDefinition]
    func callTool(_ name: String, _ arguments: JSON) async throws -> ToolResult
    func validateResultPath(_ rawPath: String) async throws -> URL
    func normalizeToolArguments(_ name: String, _ arguments: JSON) async throws -> JSON
}

/// A client MCPManager can own (Python `_ManagedMCPClient`). `connect`/`close` replace `async with`.
public protocol ManagedMCPClient: ToolGateway {
    func connect() async throws
    func close() async
}

/// Repo root is five levels above this file (backend/MCP/Sources/SymbioMCP/MCP.swift).
public let serverEntrypoint = URL(fileURLWithPath: #filePath)
    .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
    .deletingLastPathComponent().deletingLastPathComponent()
    .appendingPathComponent("node_modules/@modelcontextprotocol/server-filesystem/dist/index.js")
public let supportedTools: Set<String> = ["list_directory", "search_files", "read_text_file", "write_file"]
public let defaultAllowlist: Set<String> = ["list_directory"]

/// A Dock-launched app has a minimal PATH, so also look where node is usually installed.
public func findNode(path: String? = ProcessInfo.processInfo.environment["PATH"]) -> String {
    for dir in ["/opt/homebrew/bin", "/usr/local/bin"] + (path ?? "").split(separator: ":").map(String.init) {
        let candidate = dir + "/node"
        var isDir: ObjCBool = false
        if FileManager.default.fileExists(atPath: candidate, isDirectory: &isDir), !isDir.boolValue,
           FileManager.default.isExecutableFile(atPath: candidate) { return candidate }
    }
    return "node"
}

/// Python `normalize_result`: bounded text, structured payload cannot bypass the limit.
public func normalizeResult(_ result: JSON, limit: Int) -> ToolResult {
    var parts: [String] = []
    for (index, item) in (result["content"]?.arrayValue ?? []).enumerated() {
        if index > 0 { parts.append("\n") }
        let type = item["type"]?.stringValue ?? ""
        parts.append(type == "text" ? item["text"]?.stringValue ?? "" : "[UNSUPPORTED:\(type)]")
    }
    var (text, truncated) = boundedJoin(parts, limit: limit)
    var structured = result["structuredContent"].flatMap { $0.isNull ? nil : $0 }
    if let payload = structured {
        let (encoded, oversized) = boundedJoin([payload.text], limit: limit - text.count)
        if text.isEmpty { text = encoded }
        if oversized { structured = nil; truncated = true }
    }
    if truncated { text += truncationMarker }
    let ok = result["isError"]?.boolValue != true
    return ToolResult(
        ok: ok, text: text, structured: structured,
        error: ok ? nil : (text.isEmpty ? "MCP 도구가 오류를 반환했습니다." : text), truncated: truncated
    )
}

// MARK: - Transport

/// JSON-RPC error response (Python `McpError`).
struct RPCError: Error { let code: Int; let message: String }
struct TimeoutMarker: Error {}

/// The two requests MCPClient makes after startup; a seam so tests can stand in for the server.
protocol MCPSession: Sendable {
    func listTools(cursor: String?) async throws -> JSON
    func callTool(_ name: String, _ arguments: JSON) async throws -> JSON
    func close() async
}

/// Runs `op` with a deadline; the losing child is cancelled (Python `asyncio.timeout`).
func withTimeout<T: Sendable>(_ seconds: Double, _ op: @escaping @Sendable () async throws -> T) async throws -> T {
    try await withThrowingTaskGroup(of: T.self) { group in
        group.addTask { try await op() }
        group.addTask { try await Task.sleep(for: .seconds(seconds)); throw TimeoutMarker() }
        defer { group.cancelAll() }
        return try await group.next()!
    }
}

/// Line-delimited JSON-RPC over the server's stdin/stdout.
actor StdioSession: MCPSession {
    private let process = Process()
    private let input: FileHandle
    private let output: FileHandle
    private var pending: [Int: CheckedContinuation<JSON, any Error>] = [:]
    private var nextID = 0
    private var closed = false
    private var reader: Task<Void, Never>?

    init(arguments: [String]) throws {
        signal(SIGPIPE, SIG_IGN)  // a dead server must fail the write, not kill the app
        let node = findNode()
        process.executableURL = URL(fileURLWithPath: node.hasPrefix("/") ? node : "/usr/bin/env")
        process.arguments = (node.hasPrefix("/") ? [] : [node]) + arguments
        let stdin = Pipe(), stdout = Pipe()
        process.standardInput = stdin
        process.standardOutput = stdout
        input = stdin.fileHandleForWriting
        output = stdout.fileHandleForReading
        try process.run()
    }

    func initialize() async throws {
        startReading()
        _ = try await request("initialize", [
            "protocolVersion": "2025-06-18",
            "capabilities": [:],
            "clientInfo": ["name": "symbio", "version": "1.0"],
        ])
        try send(["jsonrpc": "2.0", "method": "notifications/initialized"])
    }

    func listTools(cursor: String?) async throws -> JSON {
        try await request("tools/list", cursor.map { ["cursor": .string($0)] } ?? [:])
    }

    func callTool(_ name: String, _ arguments: JSON) async throws -> JSON {
        try await request("tools/call", ["name": .string(name), "arguments": arguments])
    }

    func close() async {
        try? input.close()
        for _ in 0..<400 where process.isRunning { try? await Task.sleep(for: .milliseconds(5)) }
        if process.isRunning { process.terminate() }
        reader?.cancel()
        finish()
    }

    private func startReading() {
        reader = Task { [output] in
            do { for try await line in output.bytes.lines { self.receive(line) } } catch {}
            self.finish()
        }
    }

    private func request(_ method: String, _ params: JSON) async throws -> JSON {
        if closed { throw ToolError.connection("MCP 서버 연결이 끊겼습니다.") }
        nextID += 1
        let id = nextID
        return try await withTaskCancellationHandler {
            try await withCheckedThrowingContinuation { continuation in
                if Task.isCancelled { return continuation.resume(throwing: CancellationError()) }
                pending[id] = continuation
                do {
                    try send(["jsonrpc": "2.0", "id": .int(id), "method": .string(method), "params": params])
                } catch {
                    pending.removeValue(forKey: id)?.resume(throwing: error)
                }
            }
        } onCancel: {
            Task { await self.fail(id, CancellationError()) }
        }
    }

    private func fail(_ id: Int, _ error: any Error) { pending.removeValue(forKey: id)?.resume(throwing: error) }

    private func send(_ message: JSON) throws {
        try input.write(contentsOf: Data((message.dumps(JSONDumpOptions(compact: true)) + "\n").utf8))
    }

    private func receive(_ line: String) {
        guard let message = try? JSON.parse(line) else { return }
        if let method = message["method"]?.stringValue {
            // Server-initiated request: answer ping, refuse the rest (we declare no capabilities).
            guard let id = message["id"], !id.isNull else { return }
            try? send(method == "ping"
                ? ["jsonrpc": "2.0", "id": id, "result": [:]]
                : ["jsonrpc": "2.0", "id": id, "error": ["code": -32601, "message": "Method not found"]])
            return
        }
        guard let id = message["id"]?.intValue, let continuation = pending.removeValue(forKey: id) else { return }
        if let error = message["error"], !error.isNull {
            continuation.resume(throwing: RPCError(
                code: error["code"]?.intValue ?? 0, message: error["message"]?.stringValue ?? ""))
        } else {
            continuation.resume(returning: message["result"] ?? [:])
        }
    }

    private func finish() {
        closed = true
        for continuation in pending.values { continuation.resume(throwing: ToolError.connection("MCP 서버 연결이 끊겼습니다.")) }
        pending = [:]
    }
}

/// Python `asyncio.Lock`: actors are reentrant across `await`, so calls need an explicit queue.
actor AsyncLock {
    private var busy = false
    private var waiters: [CheckedContinuation<Void, Never>] = []

    func acquire() async {
        if busy { await withCheckedContinuation { waiters.append($0) } } else { busy = true }
    }

    func release() {
        if waiters.isEmpty { busy = false } else { waiters.removeFirst().resume() }
    }
}

// MARK: - Client

public actor MCPClient: ManagedMCPClient {
    private let allowedDirectories: [URL]
    private let timeoutSeconds: Double
    private let maxResultChars: Int
    private let allowlist: Set<String>
    private let entrypoint: URL
    private(set) var tools: [ToolDefinition] = []
    private var session: (any MCPSession)?
    private var connecting = false
    private var failed = false
    private var roots: [String] = []
    private let lock = AsyncLock()

    public init(
        allowedDirectories: [URL], timeoutSeconds: Double = 30, maxResultChars: Int = 50000,
        allowedTools: Set<String> = defaultAllowlist, entrypoint: URL = serverEntrypoint
    ) throws {
        if allowedTools.isEmpty || !allowedTools.isSubset(of: supportedTools) {
            throw ToolError.rejected("지원하지 않는 application allowlist입니다.")
        }
        self.allowedDirectories = allowedDirectories
        self.timeoutSeconds = timeoutSeconds
        self.maxResultChars = maxResultChars
        self.allowlist = allowedTools
        self.entrypoint = entrypoint
    }

    /// Test seam: what a successful `connect()` leaves behind.
    func install(session: any MCPSession, tools: [ToolDefinition], roots: [String]) {
        self.session = session
        self.tools = tools
        self.roots = roots
        failed = false
    }

    private func resolveRoots() throws(ToolError) -> [String] {
        let roots = allowedDirectories.map { realPath(($0.path as NSString).expandingTildeInPath) }
        guard !roots.isEmpty, roots.allSatisfy({ $0.map(isDirectory) ?? false }) else {
            throw .startup("유효한 allowed directory가 필요합니다.")
        }
        return roots.compactMap { $0 }
    }

    func discover(_ session: any MCPSession) async throws -> [ToolDefinition] {
        var found: [ToolDefinition] = []
        var cursor: String?
        var seen = Set<String>()
        while true {
            let page = try await session.listTools(cursor: cursor)
            for tool in page["tools"]?.arrayValue ?? [] {
                guard let name = tool["name"]?.stringValue, allowlist.contains(name) else { continue }
                if found.contains(where: { $0.name == name }) {
                    throw ToolError.startup("MCP 도구 정의가 중복되었습니다.")
                }
                // ponytail: structural check only, no draft 2020-12 meta-schema validation.
                let schema = tool["inputSchema"] ?? .null
                guard schema["type"]?.stringValue == "object",
                      schema["properties"]?["path"]?["type"]?.stringValue == "string",
                      schema["required"]?.arrayValue?.contains("path") == true
                else { throw ToolError.startup("지원하지 않는 filesystem 도구 schema입니다.") }
                found.append(ToolDefinition(name: name, description: tool["description"]?.stringValue ?? "", schema: schema))
            }
            guard let next = page["nextCursor"]?.stringValue, !next.isEmpty else { break }
            if !seen.insert(next).inserted { throw ToolError.startup("MCP 도구 페이지가 반복됩니다.") }
            cursor = next
        }
        if Set(found.map(\.name)) != allowlist { throw ToolError.startup("허용 목록의 필수 MCP capability가 없습니다.") }
        return found
    }

    /// Return only the allowlisted startup snapshot; no per-command discovery.
    public func listTools() async throws -> [ToolDefinition] {
        guard session != nil, !failed else { throw ToolError.connection("MCP가 연결되어 있지 않습니다.") }
        return tools
    }

    public func connect() async throws {
        if connecting { throw ToolError.connection("MCP가 이미 연결 중이거나 연결되어 있습니다.") }
        connecting = true
        var started: StdioSession?
        do {
            let roots = try resolveRoots()
            guard isRegularFile(entrypoint.path) else {
                throw ToolError.startup("Filesystem MCP가 설치되어 있지 않습니다. npm ci를 실행하세요.")
            }
            let stdio = try StdioSession(arguments: [entrypoint.path] + roots)
            started = stdio
            let tools = try await withTimeout(timeoutSeconds) {
                try await stdio.initialize()
                return try await self.discover(stdio)
            }
            install(session: stdio, tools: tools, roots: roots)
        } catch {
            await started?.close()
            reset()
            if let error = error as? ToolError, case .startup = error { throw error }
            throw ToolError.startup("MCP 시작/초기화/도구 발견 실패")
        }
    }

    public func close() async {
        await session?.close()
        reset()
    }

    private func reset() {
        session = nil
        tools = []
        roots = []
        connecting = false
    }

    private func validatePath(_ raw: JSON?, creating: Bool) throws(ToolError) -> String {
        guard let raw = raw?.stringValue, raw.hasPrefix("/") else { throw .rejected("도구 경로는 절대경로여야 합니다.") }
        guard !raw.contains("\0"), let path = creating ? resolveLoose(raw) : realPath(raw) else {
            throw .rejected("도구 경로를 해석할 수 없습니다.")
        }
        guard roots.contains(where: { isRelative(path, to: $0) }) else { throw .rejected("허용 경로 밖 접근입니다.") }
        return path
    }

    func normalizeArguments(_ name: String, _ arguments: JSON) throws(ToolError) -> JSON {
        guard allowlist.contains(name), let tool = tools.first(where: { $0.name == name }) else {
            throw .notFound("허용되지 않은 도구입니다.")
        }
        guard session != nil, !failed else { throw .connection("MCP 연결이 종료되었거나 사용할 수 없습니다.") }
        var args = arguments
        if name == "read_text_file", case let .object(pairs) = args {
            args = .object(pairs.filter { !(($0.0 == "head" || $0.0 == "tail") && $0.1.isNull) })
        }
        guard JSONSchema.isValid(args, schema: tool.schema) else { throw .rejected("잘못된 도구 인자입니다.") }

        let creating = name == "write_file"
        let path = try validatePath(args["path"], creating: creating)
        if creating {
            guard args["content"]?.stringValue != nil else { throw .rejected("write_file content는 문자열이어야 합니다.") }
            var info = stat()
            if stat(path, &info) == 0 {
                guard isRegularFile(path) else { throw .rejected("쓰기 대상은 regular file이어야 합니다.") }
            } else {
                guard let parent = realPath((path as NSString).deletingLastPathComponent) else {
                    throw .rejected("쓰기 대상의 부모 디렉터리가 존재하지 않습니다.")
                }
                guard isDirectory(parent), roots.contains(where: { isRelative(parent, to: $0) }) else {
                    throw .rejected("쓰기 대상의 부모가 허용 디렉터리가 아닙니다.")
                }
            }
        }
        args.set("path", .string(path))
        return args
    }

    /// Validate and canonicalize arguments without executing the MCP tool.
    public func normalizeToolArguments(_ name: String, _ arguments: JSON) async throws -> JSON {
        try normalizeArguments(name, arguments)
    }

    /// Revalidate a server-returned candidate as a file inside an allowed root.
    public func validateResultPath(_ rawPath: String) async throws -> URL {
        guard session != nil, !failed else { throw ToolError.connection("MCP가 연결되어 있지 않습니다.") }
        let path = try validatePath(.string(rawPath), creating: false)
        guard isRegularFile(path) else { throw ToolError.rejected("검색 후보가 읽을 수 있는 파일이 아닙니다.") }
        return URL(fileURLWithPath: path)
    }

    public func callTool(_ name: String, _ arguments: JSON) async throws -> ToolResult {
        await lock.acquire()
        do {
            let result = try await callLocked(name, arguments)
            await lock.release()
            return result
        } catch {
            await lock.release()
            throw error
        }
    }

    private func callLocked(_ name: String, _ arguments: JSON) async throws -> ToolResult {
        let normalized = try normalizeArguments(name, arguments)
        guard let session else { throw ToolError.connection("MCP 연결이 종료되었거나 사용할 수 없습니다.") }
        let raw: JSON
        do {
            raw = try await withTimeout(timeoutSeconds) { try await session.callTool(name, normalized) }
        } catch {
            failed = true
            let timeout = ToolError.timeout("MCP 도구 실행 시간 초과; 자동 재실행하지 않습니다.")
            switch error {
            case is TimeoutMarker: throw timeout
            case is CancellationError: throw error
            case let rpc as RPCError: throw rpc.code == 408 ? timeout : ToolError.connection("MCP protocol 요청 실패")
            default: throw ToolError.connection("MCP 도구 연결 또는 실행 실패")
            }
        }
        return normalizeResult(raw, limit: maxResultChars)
    }
}

// MARK: - Path helpers

/// Python `Path.resolve(strict=False)`: follow existing components, keep the missing tail lexical.
/// Stricter than Python: an existing but unresolvable component (dangling link) fails closed.
func resolveLoose(_ raw: String) -> String? {
    var current = "/"
    for part in raw.split(separator: "/") where part != "." {
        if part == ".." { current = (current as NSString).deletingLastPathComponent; continue }
        let candidate = current == "/" ? "/\(part)" : "\(current)/\(part)"
        if let real = realPath(candidate) { current = real; continue }
        var info = stat()
        if lstat(candidate, &info) == 0 { return nil }
        current = candidate
    }
    return current
}

func isRegularFile(_ path: String) -> Bool {
    var info = stat()
    return stat(path, &info) == 0 && (info.st_mode & S_IFMT) == S_IFREG
}

func isDirectory(_ path: String) -> Bool {
    var info = stat()
    return stat(path, &info) == 0 && (info.st_mode & S_IFMT) == S_IFDIR
}
