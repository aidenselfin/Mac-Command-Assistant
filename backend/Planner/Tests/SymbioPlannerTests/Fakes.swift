import Foundation
import SymbioCore
import SymbioMCP
import SymbioUndo
import Testing
@testable import SymbioPlanner

/// Test doubles that replay a script, like Python's `AsyncMock` model / `fake_mcp()` gateway
/// (tests/test_planner.py) and the `config` fixture (tests/conftest.py).

struct ScriptExhausted: Error {}

/// What the planner sent on one model call.
struct ModelCall: Sendable {
    let messages: [JSON]
    let tools: [ToolDefinition]
    let allowTools: Bool
    let allowClarification: Bool
    var toolNames: [String] { tools.map(\.name) }
    var last: JSON { messages.last ?? .null }
    var lastContent: String { last["content"]?.stringValue ?? "" }
    var system: String { messages.first?["content"]?.stringValue ?? "" }
}

/// Replays turns in order; `repeating` answers once the list runs out (Python `return_value`).
final class ScriptedModel: ModelClient, @unchecked Sendable {  // fields behind `lock`
    enum Step: Sendable {
        case turn(ModelTurn)
        case fail(any Error)
        case wait(Double, ModelTurn)  // sleeps (cancellable) before answering
    }

    private let lock = NSLock()
    private var steps: [Step]
    private let repeating: ModelTurn?
    private var recorded: [ModelCall] = []

    init(_ steps: [Step], repeating: ModelTurn? = nil) {
        self.steps = steps
        self.repeating = repeating
    }

    convenience init(turns: [ModelTurn], repeating: ModelTurn? = nil) {
        self.init(turns.map(Step.turn), repeating: repeating)
    }

    var calls: [ModelCall] { lock.withLock { recorded } }
    var count: Int { calls.count }

    func generate(
        _ messages: [JSON], _ tools: [ToolDefinition], allowTools: Bool, allowClarification: Bool
    ) async throws -> ModelTurn {
        let step: Step? = lock.withLock {
            recorded.append(ModelCall(
                messages: messages, tools: tools, allowTools: allowTools, allowClarification: allowClarification))
            return steps.isEmpty ? repeating.map(Step.turn) : steps.removeFirst()
        }
        switch step {
        case let .turn(turn)?: return turn
        case let .fail(error)?: throw error
        case let .wait(seconds, turn)?:
            try await Task.sleep(nanoseconds: UInt64(seconds * 1_000_000_000))
            return turn
        case nil: throw ScriptExhausted()
        }
    }
}

/// What the real MCP servers advertise; a call is only executed if its tool was offered.
let advertisedTools = [
    "filesystem__list_directory", "filesystem__search_files", "filesystem__read_text_file",
    "filesystem__write_file", "web__browser_navigate", "web__browser_snapshot", "macos__get_active_app",
].map { ToolDefinition(name: $0, description: $0, schema: ["type": "object"]) }

func tool(_ name: String, _ description: String? = nil) -> ToolDefinition {
    ToolDefinition(name: name, description: description ?? name, schema: ["type": "object"])
}

/// Python `fake_mcp()`: records `call_tool`, replays `results`, then `fallback`.
final class FakeGateway: ToolGateway, @unchecked Sendable {  // fields behind `lock`
    private let lock = NSLock()
    private var _tools: [ToolDefinition]
    private var results: [Result<ToolResult, any Error>]
    private let fallback: ToolResult?
    private var recorded: [(name: String, arguments: JSON)] = []
    private var listCount = 0
    let validate: @Sendable (String) throws -> URL

    init(
        tools: [ToolDefinition] = advertisedTools, results: [ToolResult] = [], fallback: ToolResult? = nil,
        validate: @escaping @Sendable (String) throws -> URL = { URL(fileURLWithPath: $0) }
    ) {
        _tools = tools
        self.results = results.map { .success($0) }
        self.fallback = fallback
        self.validate = validate
    }

    init(tools: [ToolDefinition] = advertisedTools, outcomes: [Result<ToolResult, any Error>]) {
        _tools = tools
        results = outcomes
        fallback = nil
        validate = { URL(fileURLWithPath: $0) }
    }

    var calls: [(name: String, arguments: JSON)] { lock.withLock { recorded } }
    var names: [String] { calls.map(\.name) }
    var listToolsCount: Int { lock.withLock { listCount } }

    func listTools() async throws -> [ToolDefinition] {
        lock.withLock {
            listCount += 1
            return _tools
        }
    }

    func callTool(_ name: String, _ arguments: JSON) async throws -> ToolResult {
        let next: Result<ToolResult, any Error>? = lock.withLock {
            recorded.append((name, arguments))
            return results.isEmpty ? fallback.map { .success($0) } : results.removeFirst()
        }
        guard let next else { throw ScriptExhausted() }
        return try next.get()
    }

    func validateResultPath(_ rawPath: String) async throws -> URL { try validate(rawPath) }

    func normalizeToolArguments(_ name: String, _ arguments: JSON) async throws -> JSON { arguments }
}

/// Lines and events a planner sent out, collected for assertions.
final class Recorder<T: Sendable>: @unchecked Sendable {  // fields behind `lock`
    private let lock = NSLock()
    private var items: [T] = []
    func append(_ item: T) { lock.withLock { items.append(item) } }
    var all: [T] { lock.withLock { items } }
}

/// Python `config` fixture: a fresh allowed root holding `한글 메모.txt`.
func makeConfig(maxToolRounds: Int = 20, maxRunSeconds: Double = 90, extraRoots: Int = 0) throws -> Config {
    let base = URL(fileURLWithPath: NSTemporaryDirectory()).appendingPathComponent("planner-\(UUID().uuidString)")
    var roots: [URL] = []
    for index in 0...extraRoots {
        let root = base.appendingPathComponent(index == 0 ? "voice-action-test" : "root-\(index)")
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        roots.append(URL(fileURLWithPath: realPath(root.path)!, isDirectory: true))
    }
    try Data("hello".utf8).write(to: roots[0].appendingPathComponent("한글 메모.txt"))
    return Config(
        provider: "openai", model: "gpt-4o-mini", apiKey: "test-secret", allowedDirectories: roots,
        maxToolRounds: maxToolRounds, maxRunSeconds: maxRunSeconds)
}

extension Config {
    var root: URL { allowedDirectories[0] }
    /// Plain string join: URL.appendingPathComponent may hand back a decomposed (NFD) Korean name.
    func path(_ name: String) -> String { root.path + "/" + name }
}

/// Python test-module `Planner(...)`: a planner with an undo journal in the allowed root.
func makePlanner(
    _ config: Config, _ gateway: any ToolGateway, _ model: any ModelClient,
    emit: @escaping @Sendable (String) -> Void = { _ in }, tools: [ToolDefinition]? = nil,
    timing: PlannerTiming? = nil, events: Recorder<PlannerEvent>? = nil, allowLocalWeb: Bool = false,
    journal: Bool = true, escalation: (any ModelClient)? = nil
) throws -> Planner {
    let undo = journal
        ? try UndoJournal(path: config.root.appendingPathComponent(".test-undo.sqlite3"), allowedRoots: config.allowedDirectories)
        : nil
    var observer: PlannerEventObserver?
    if let events { observer = { events.append($0) } }
    return Planner(
        config, gateway, model, emit: emit, tools: tools, timing: timing,
        eventObserver: observer, allowLocalWeb: allowLocalWeb,
        undoJournal: undo, escalationModel: escalation)
}

func turn(_ text: String?, _ calls: ToolCall...) -> ModelTurn { ModelTurn(text, calls) }

func callJSON(_ id: String, _ name: String, _ arguments: JSON) -> ToolCall {
    ToolCall(id, name, arguments.dumps())
}

func webResult(_ url: String, _ snapshot: String = "page content", ok: Bool = true) -> ToolResult {
    ToolResult(ok: ok, text: "### Page state\n- Page URL: \(url)\n- Page Snapshot:\n\(snapshot)")
}

func ok(_ text: String) -> ToolResult { ToolResult(ok: true, text: text) }

// MARK: - outcome accessors

extension PlannerOutcome {
    var completed: Completed? { if case let .completed(value) = self { return value }; return nil }
    var clarification: Clarification? { if case let .clarification(value) = self { return value }; return nil }
    var approval: ApprovalRequired? { if case let .approvalRequired(value) = self { return value }; return nil }
    var errorMessage: String? { if case let .error(message) = self { return message }; return nil }
    var text: String { completed?.text ?? errorMessage ?? "" }
}

func done(_ text: String) -> PlannerOutcome { .completed(Completed(text)) }

/// Python `json.loads(tool.arguments)` for an assertion.
func parse(_ text: String) -> JSON { (try? JSON.parse(text)) ?? .null }

func fileExists(_ path: String) -> Bool { FileManager.default.fileExists(atPath: path) }
func readText(_ path: String) -> String? { (try? Data(contentsOf: URL(fileURLWithPath: path))).map { String(decoding: $0, as: UTF8.self) } }
func writeText(_ text: String, _ path: String) throws {
    try FileManager.default.createDirectory(
        at: URL(fileURLWithPath: path).deletingLastPathComponent(), withIntermediateDirectories: true)
    try Data(text.utf8).write(to: URL(fileURLWithPath: path))
}
