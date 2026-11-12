import Foundation
import SymbioCore
import SymbioMCP
import SymbioPlanner
import SymbioUndo
import Testing
@testable import SymbioRuntime

/// Test doubles: a scripted planner (tests/test_runtime.py `ScriptedPlanner`) and, copied from
/// backend/Planner/Tests/SymbioPlannerTests/Fakes.swift, the fake model/gateway a real Planner runs on.

struct ScriptExhausted: Error {}
struct Boom: Error, Equatable { let message: String }

/// A tiny async event (Python `asyncio.Event`).
actor Gate {
    private var opened = false
    private var waiters: [CheckedContinuation<Void, Never>] = []
    func wait() async {
        if opened { return }
        await withCheckedContinuation { waiters.append($0) }
    }
    func open() {
        opened = true
        waiters.forEach { $0.resume() }
        waiters = []
    }
}

/// Python `Clock`: a time the test moves by hand.
final class FakeClock: @unchecked Sendable {  // `now` behind `lock`
    private let lock = NSLock()
    private var value = 1000.0
    var now: Double {
        get { lock.withLock { value } }
        set { lock.withLock { value = newValue } }
    }
}

final class ScriptedPlanner: RuntimePlanner, @unchecked Sendable {  // fields behind `lock`
    enum Action: Sendable {
        case outcome(PlannerOutcome)
        case fail(any Error)
        case call(@Sendable (ScriptedPlanner) async throws -> PlannerOutcome)
    }

    let config: Config
    private let lock = NSLock()
    private var runActions: [Action]
    private var resumeActions: [Action]
    private var approveActions: [Action]
    private var undoActions: [Action]
    private var observer: PlannerEventObserver?
    private(set) var runInputs: [String] = []
    private(set) var runOptions: [(attachment: String?, previous: Exchange?)] = []
    private(set) var resumeInputs: [(context: ClarificationContext, selection: String)] = []
    private(set) var approveInputs: [ApprovalContext] = []
    private(set) var rejectInputs: [ApprovalContext] = []
    private(set) var undoCount = 0

    init(
        _ run: [Action], resume: [Action] = [], approve: [Action] = [], undo: [Action] = [],
        config: Config? = nil
    ) throws {
        self.config = try config ?? makeConfig()
        runActions = run
        resumeActions = resume
        approveActions = approve
        undoActions = undo
    }

    func setEventObserver(_ observer: PlannerEventObserver?) { lock.withLock { self.observer = observer } }

    private func play(_ next: Action?) async throws -> PlannerOutcome {
        switch next {
        case let .outcome(outcome)?: return outcome
        case let .fail(error)?: throw error
        case let .call(body)?: return try await body(self)
        case nil: throw ScriptExhausted()
        }
    }

    private static func pop(_ list: inout [Action]) -> Action? { list.isEmpty ? nil : list.removeFirst() }

    func run(_ userText: String, attachment: String?, previous: Exchange?) async throws -> PlannerOutcome {
        let next = lock.withLock {
            runInputs.append(userText)
            runOptions.append((attachment, previous))
            return Self.pop(&runActions)
        }
        return try await play(next)
    }

    func resume(_ context: ClarificationContext, _ selection: String) async throws -> PlannerOutcome {
        let next = lock.withLock {
            resumeInputs.append((context, selection))
            return Self.pop(&resumeActions)
        }
        return try await play(next)
    }

    func approve(_ context: ApprovalContext) async throws -> PlannerOutcome {
        let next = lock.withLock {
            approveInputs.append(context)
            return Self.pop(&approveActions)
        }
        return try await play(next)
    }

    func reject(_ context: ApprovalContext) -> Completed {
        lock.withLock { rejectInputs.append(context) }
        return Completed("파일 쓰기를 거절했습니다. 파일은 변경하지 않았습니다.")
    }

    func undoLastWrite() async throws -> PlannerOutcome {
        let next = lock.withLock {
            undoCount += 1
            return Self.pop(&undoActions)
        }
        return try await play(next)
    }

    func event(_ type: PlannerEventType, _ iteration: Int = 1, _ toolName: String? = nil) {
        lock.withLock { observer }!(PlannerEvent(type, iteration, toolName))
    }
}

/// What the feedback loop was told, in order.
final class FakeFeedback: RuntimeFeedback, @unchecked Sendable {  // fields behind `lock`
    private let lock = NSLock()
    private var items: [String] = []
    let accept: Bool
    init(accept: Bool = true) { self.accept = accept }
    var log: [String] { lock.withLock { items } }
    private func add(_ item: String) { lock.withLock { items.append(item) } }

    func userInput(_ text: String, newRun: Bool) { add("user_input:\(text):\(newRun)") }
    func decision(_ action: String, tool: String?) { add("decision:\(action)") }
    func outcome(_ outcome: PlannerOutcome) { add("outcome") }
    func exception(_ error: any Error) { add("exception") }
    func state(_ previous: String, _ current: String, _ reason: String) { add("state:\(previous)->\(current)") }
    func userReport(_ text: String) -> Bool { add("user_feedback:\(text)"); return accept }
    func cancelled(reason: String) { add("cancelled:\(reason)") }
    func runFinished() { add("run_finished") }
}

// MARK: - the real Planner's doubles (copied from the Planner package tests)

final class ScriptedModel: ModelClient, @unchecked Sendable {  // fields behind `lock`
    private let lock = NSLock()
    private var turns: [ModelTurn]
    private var recorded: [[JSON]] = []
    init(turns: [ModelTurn]) { self.turns = turns }

    var calls: [[JSON]] { lock.withLock { recorded } }
    var count: Int { calls.count }
    /// Python `model.generate.side_effect = [...]` set again mid-test.
    func append(_ more: [ModelTurn]) { lock.withLock { turns += more } }

    func generate(
        _ messages: [JSON], _ tools: [ToolDefinition], allowTools: Bool, allowClarification: Bool
    ) async throws -> ModelTurn {
        let next: ModelTurn? = lock.withLock {
            recorded.append(messages)
            return turns.isEmpty ? nil : turns.removeFirst()
        }
        guard let next else { throw ScriptExhausted() }
        return next
    }
}

func systemText(_ messages: [JSON]) -> String { messages.first?["content"]?.stringValue ?? "" }

let advertisedTools = [
    "filesystem__list_directory", "filesystem__search_files", "filesystem__read_text_file",
    "filesystem__write_file", "web__browser_navigate", "web__browser_snapshot", "macos__get_active_app",
].map { tool($0) }

func tool(_ name: String) -> ToolDefinition { ToolDefinition(name: name, description: name, schema: ["type": "object"]) }

/// Python `fake_mcp()`: records `call_tool`, replays `results`, then `fallback`.
final class FakeGateway: ToolGateway, @unchecked Sendable {  // fields behind `lock`
    private let lock = NSLock()
    private let tools: [ToolDefinition]
    private var results: [ToolResult]
    private let fallback: ToolResult?
    private var recorded: [String] = []

    init(tools: [ToolDefinition] = advertisedTools, results: [ToolResult] = [], fallback: ToolResult? = nil) {
        self.tools = tools
        self.results = results
        self.fallback = fallback
    }

    var names: [String] { lock.withLock { recorded } }

    func listTools() async throws -> [ToolDefinition] { tools }

    func callTool(_ name: String, _ arguments: JSON) async throws -> ToolResult {
        let next: ToolResult? = lock.withLock {
            recorded.append(name)
            return results.isEmpty ? fallback : results.removeFirst()
        }
        guard let next else { throw ScriptExhausted() }
        return next
    }

    func validateResultPath(_ rawPath: String) async throws -> URL { URL(fileURLWithPath: rawPath) }

    func normalizeToolArguments(_ name: String, _ arguments: JSON) async throws -> JSON { arguments }
}

/// Python `config` fixture: a fresh allowed root.
func makeConfig() throws -> Config {
    let root = URL(fileURLWithPath: NSTemporaryDirectory())
        .appendingPathComponent("runtime-\(UUID().uuidString)/voice-action-test")
    try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
    return Config(
        provider: "openai", model: "gpt-4o-mini", apiKey: "test-secret",
        allowedDirectories: [URL(fileURLWithPath: realPath(root.path)!, isDirectory: true)])
}

extension Config {
    var root: URL { allowedDirectories[0] }
    func path(_ name: String) -> String { root.path + "/" + name }
}

func makePlanner(_ config: Config, _ gateway: any ToolGateway, _ model: any ModelClient) throws -> Planner {
    let undo = try UndoJournal(
        path: config.root.appendingPathComponent(".test-undo.sqlite3"), allowedRoots: config.allowedDirectories)
    return Planner(config, gateway, model, undoJournal: undo)
}

func turn(_ text: String?, _ calls: ToolCall...) -> ModelTurn { ModelTurn(text, calls) }
func callJSON(_ id: String, _ name: String, _ arguments: JSON) -> ToolCall { ToolCall(id, name, arguments.dumps()) }
func ok(_ text: String) -> ToolResult { ToolResult(ok: true, text: text) }
func findCall(_ keyword: String) -> ModelTurn { turn(nil, callJSON("find-1", "local__find_files", ["keywords": [.string(keyword)]])) }
func readCall(_ path: String, id: String = "read-1") -> ModelTurn {
    turn(nil, callJSON(id, "filesystem__read_text_file", ["path": .string(path)]))
}
func writeTurn(_ path: String, _ content: String) -> ModelTurn {
    turn(nil, callJSON("write", "filesystem__write_file", ["path": .string(path), "content": .string(content)]))
}
func writeText(_ text: String, _ path: String) throws {
    try FileManager.default.createDirectory(
        at: URL(fileURLWithPath: path).deletingLastPathComponent(), withIntermediateDirectories: true)
    try Data(text.utf8).write(to: URL(fileURLWithPath: path))
}

/// A real ApprovalRequired (its context can only be built by Planner).
func approval(_ name: String = "summary.txt") async throws -> ApprovalRequired {
    let config = try makeConfig()
    let planner = try makePlanner(config, FakeGateway(fallback: ok("written")), ScriptedModel(turns: [writeTurn(config.path(name), "summary")]))
    return try #require(try await planner.run("\(name)에 저장해줘").approval)
}

// MARK: - accessors

extension PlannerOutcome {
    var completed: Completed? { if case let .completed(value) = self { return value }; return nil }
    var clarification: Clarification? { if case let .clarification(value) = self { return value }; return nil }
    var approval: ApprovalRequired? { if case let .approvalRequired(value) = self { return value }; return nil }
    var errorMessage: String? { if case let .error(message) = self { return message }; return nil }
}

extension RuntimeResult {
    var outcome: PlannerOutcome? { if case let .outcome(value) = self { return value }; return nil }
    var busy: Busy? { if case let .busy(value) = self { return value }; return nil }
    var completed: Completed? { outcome?.completed }
    var clarification: Clarification? { outcome?.clarification }
    var approval: ApprovalRequired? { outcome?.approval }
    var errorMessage: String? { outcome?.errorMessage }
}

func done(_ text: String) -> RuntimeResult { .outcome(.completed(Completed(text))) }
func result(_ outcome: PlannerOutcome) -> RuntimeResult { .outcome(outcome) }
