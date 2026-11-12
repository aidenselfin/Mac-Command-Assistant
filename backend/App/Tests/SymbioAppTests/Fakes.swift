import Foundation
import SymbioCore
import SymbioMCP
import SymbioRuntime
import SymbioUndo
import Testing
@testable import SymbioApp
@testable import SymbioPlanner

/// Test doubles: Python `Mock()` workers, collected events, and (copied from the Runtime package
/// tests) the fake model/gateway a real Planner runs on to make a real approval.

struct Boom: Error, Equatable { let message: String }
struct ScriptExhausted: Error {}

let repoRoot = URL(fileURLWithPath: #filePath)
    .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
    .deletingLastPathComponent().deletingLastPathComponent()

/// Python `Mock()` as a worker: records every call and accepts it.
final class FakeWorker: BridgeWorker, @unchecked Sendable {  // fields behind `lock`
    private let lock = NSLock()
    private var log: [String] = []
    var accepts = true

    var calls: [String] { lock.withLock { log } }
    func count(_ name: String) -> Int { calls.filter { $0 == name || $0.hasPrefix(name + ":") }.count }
    private func add(_ call: String) -> Bool { lock.withLock { log.append(call); return accepts } }

    func start() { _ = add("start") }
    func submit(_ text: String) -> Bool { add("submit:\(text)") }
    func approvePending() -> Bool { add("approve") }
    func rejectPending() -> Bool { add("reject") }
    func undoLastWrite() -> Bool { add("undo") }
    func attach(_ path: String) -> Bool { add("attach:\(path)") }
    func detach() { _ = add("detach") }
    func resetConversation() -> Bool { add("reset") }
    func cancelActive() -> Bool { add("cancel") }
    func shutdown() async { _ = add("shutdown") }
}

final class Events: @unchecked Sendable {  // `items` behind `lock`
    private let lock = NSLock()
    private var items: [JSON] = []
    func append(_ event: JSON) { lock.withLock { items.append(event) } }
    func clear() { lock.withLock { items = [] } }
    var all: [JSON] { lock.withLock { items } }
    var last: JSON { all.last ?? .null }
    var types: [String] { all.map { $0["type"]?.stringValue ?? "" } }
}

func makeBridge(voiceKey: String? = nil, roots: [URL] = [],
                transcriber: (@Sendable (URL, String) async throws -> String)? = nil)
    -> (NativeBridge, FakeWorker, Events)
{
    let worker = FakeWorker(), events = Events()
    let bridge = transcriber.map {
        NativeBridge(worker, emit: events.append, voiceKey: voiceKey, roots: roots, transcriber: $0)
    } ?? NativeBridge(worker, emit: events.append, voiceKey: voiceKey, roots: roots)
    bridge.message(.state(.idle))
    return (bridge, worker, events)
}

func completed(_ text: String, changed: [String] = [], found: [String] = []) -> UIMessage {
    .outcome(.outcome(.completed(Completed(text, changedPaths: changed, foundPaths: found))))
}

func temporaryFolder() throws -> URL {
    let folder = URL(fileURLWithPath: realPath(NSTemporaryDirectory())!)
        .appendingPathComponent("app-\(UUID().uuidString)")
    try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
    return folder
}

/// Python `config` fixture: a fresh allowed root.
func makeConfig() throws -> Config {
    let root = try temporaryFolder().appendingPathComponent("voice-action-test")
    try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
    return Config(provider: "openai", model: "gpt-4o-mini", apiKey: "test-secret", allowedDirectories: [root])
}

extension Config {
    var root: URL { allowedDirectories[0] }
    func path(_ name: String) -> String { root.path + "/" + name }
}

final class ScriptedModel: ModelClient, @unchecked Sendable {  // fields behind `lock`
    private let lock = NSLock()
    private var turns: [Result<ModelTurn, any Error>]
    init(_ turns: [ModelTurn]) { self.turns = turns.map { .success($0) } }
    init(failing errors: [any Error]) { turns = errors.map { .failure($0) } }

    func generate(
        _ messages: [JSON], _ tools: [ToolDefinition], allowTools: Bool, allowClarification: Bool
    ) async throws -> ModelTurn {
        let next = lock.withLock { turns.isEmpty ? nil : turns.removeFirst() }
        guard let next else { throw ScriptExhausted() }
        return try next.get()
    }
}

func tool(_ name: String) -> ToolDefinition { ToolDefinition(name: name, description: name, schema: ["type": "object"]) }

/// A gateway that is also a RuntimeManager (Python `FakeManager`): records calls and closing.
final class FakeManager: RuntimeManager, @unchecked Sendable {  // fields behind `lock`
    private let lock = NSLock()
    let tools: [ToolDefinition]
    private var recorded: [String] = []
    private var connects = 0, lists = 0, closes = 0

    init(tools: [ToolDefinition] = []) { self.tools = tools }

    var names: [String] { lock.withLock { recorded } }
    var connectCount: Int { lock.withLock { connects } }
    var listCount: Int { lock.withLock { lists } }
    var closed: Bool { lock.withLock { closes > 0 } }

    func connect() async throws { lock.withLock { connects += 1 } }
    func close() async { lock.withLock { closes += 1 } }
    func listTools() async throws -> [ToolDefinition] { lock.withLock { lists += 1; return tools } }
    func callTool(_ name: String, _ arguments: JSON) async throws -> ToolResult {
        lock.withLock { recorded.append(name) }
        return ToolResult(ok: true, text: "ok")
    }
    func validateResultPath(_ rawPath: String) async throws -> URL { URL(fileURLWithPath: rawPath) }
    func normalizeToolArguments(_ name: String, _ arguments: JSON) async throws -> JSON { arguments }
}

func writeTurn(_ path: String, _ content: String) -> ModelTurn {
    ModelTurn(nil, [ToolCall("write", "filesystem__write_file",
                             (["path": .string(path), "content": .string(content)] as JSON).dumps())])
}

/// A real ApprovalRequired (its context can only be built by Planner).
func approval(_ name: String = "out.txt", content: String = "x") async throws -> ApprovalRequired {
    let config = try makeConfig()
    let undo = try UndoJournal(path: config.root.appendingPathComponent(".test-undo.sqlite3"),
                               allowedRoots: config.allowedDirectories)
    let planner = Planner(config, FakeManager(tools: [tool("filesystem__write_file")]),
                          ScriptedModel([writeTurn(config.path(name), content)]), undoJournal: undo)
    guard case let .approvalRequired(value) = try await planner.run("\(name)에 저장해줘") else {
        throw ScriptExhausted()
    }
    return value
}

/// Python `ApprovalRequired(...)` with chosen public fields around a real context.
func approval(
    _ base: ApprovalRequired, tool: String, target: String, operation: String, chars: Int = 0,
    preview: String = "", truncated: Bool = false, targets: [String] = []
) -> ApprovalRequired {
    ApprovalRequired(toolName: tool, targetPath: target, operation: operation, contentChars: chars,
                     preview: preview, previewTruncated: truncated, context: base.context, targets: targets)
}

/// Wait for a message of one kind (Python `wait_for_message`), returning it and all seen so far.
/// Polls with sleeps: a blocking wait would hold a cooperative thread the worker needs.
func waitFor(_ queue: BlockingQueue<UIMessage>, _ matches: (UIMessage) -> Bool, timeout: Double = 10) async
    -> (UIMessage?, [UIMessage])
{
    var seen: [UIMessage] = []
    let deadline = Date(timeIntervalSinceNow: timeout)
    while Date() < deadline {
        guard let message = queue.get(timeout: 0) else {
            try? await Task.sleep(for: .milliseconds(5))
            continue
        }
        seen.append(message)
        if matches(message) { return (message, seen) }
    }
    return (nil, seen)
}

func isState(_ message: UIMessage) -> Bool { if case .state = message { return true }; return false }
func isStopped(_ message: UIMessage) -> Bool { if case .stopped = message { return true }; return false }
func isOutcome(_ message: UIMessage) -> Bool { if case .outcome = message { return true }; return false }
func isAttachment(_ message: UIMessage) -> Bool { if case .attachment = message { return true }; return false }
func isFailure(_ message: UIMessage) -> Bool { if case .failure = message { return true }; return false }

/// Runs a program and returns (status, stdout, stderr).
@discardableResult
func run(_ program: String, _ arguments: [String], cwd: URL? = nil, environment: [String: String]? = nil)
    throws -> (Int32, String, String)
{
    let process = Process()
    process.executableURL = URL(fileURLWithPath: program)
    process.arguments = arguments
    if let cwd { process.currentDirectoryURL = cwd }
    if let environment { process.environment = environment }
    let out = Pipe(), err = Pipe()
    process.standardOutput = out
    process.standardError = err
    try process.run()
    let output = out.fileHandleForReading.readDataToEndOfFile()
    let error = err.fileHandleForReading.readDataToEndOfFile()
    process.waitUntilExit()
    return (process.terminationStatus, String(decoding: output, as: UTF8.self), String(decoding: error, as: UTF8.self))
}
