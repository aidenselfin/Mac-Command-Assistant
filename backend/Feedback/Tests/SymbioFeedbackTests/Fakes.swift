import Foundation
import SymbioCore
import SymbioPlanner
import SymbioUndo
@testable import SymbioFeedback

/// Python tests/test_feedback.py helpers: FakeClock, FakeAnalyzer, FakeModel, make_loop, LESSON.

final class FakeClock: @unchecked Sendable {  // `value` is behind `lock`
    private let lock = NSLock()
    private var value = 1_000_000.0
    var now: Double {
        get { lock.withLock { value } }
        set { lock.withLock { value = newValue } }
    }
}

struct Boom: Error {}

final class FakeAnalyzer: FeedbackAnalyzer, @unchecked Sendable {  // fields behind `lock`
    private let lock = NSLock()
    let result: JSON
    let error: (any Error)?
    let hang: Bool
    private var seen: [(rule: String, bundle: String)] = []
    private(set) var closed = false

    init(_ result: JSON = ["action": "none", "root_cause": "r", "confidence": 0.9], error: (any Error)? = nil,
         hang: Bool = false)
    {
        self.result = result
        self.error = error
        self.hang = hang
    }

    var calls: [(rule: String, bundle: String)] { lock.withLock { seen } }

    func analyze(rule: String, bundle: String) async throws -> JSON {
        lock.withLock { seen.append((rule, bundle)) }
        if hang { try await Task.sleep(for: .seconds(3600)) }
        if let error { throw error }
        return result
    }

    func close() async { lock.withLock { closed = true } }
}

final class FakeModel: ModelClient, @unchecked Sendable {  // fields behind `lock`
    private let lock = NSLock()
    private var turns: [Result<ModelTurn, any Error>]
    private var messages: [[JSON]] = []

    init(_ turns: [ModelTurn]) { self.turns = turns.map { .success($0) } }
    init(failing error: any Error) { turns = [.failure(error)] }

    var seen: [[JSON]] { lock.withLock { messages } }

    func generate(
        _ messages: [JSON], _ tools: [ToolDefinition], allowTools: Bool, allowClarification: Bool
    ) async throws -> ModelTurn {
        let next = lock.withLock {
            self.messages.append(messages)
            return turns.removeFirst()
        }
        return try next.get()
    }
}

final class Lines: @unchecked Sendable {  // `items` is behind `lock`
    private let lock = NSLock()
    private var items: [String] = []
    func append(_ line: String) { lock.withLock { items.append(line) } }
    var all: [String] { lock.withLock { items } }
}

let lessonResult: JSON = [
    "root_cause": "경로 alias를 쓰지 않음",
    "action": "lesson",
    "lesson": "파일 목록 요청은 먼저 list_directory를 호출하세요.",
    "confidence": 0.9,
]
let lessonText = lessonResult["lesson"]!.stringValue!

func tempDir() -> URL {
    let url = FileManager.default.temporaryDirectory.appendingPathComponent("fb-\(UUID().uuidString)")
    try! FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
    return URL(fileURLWithPath: realPath(url.path) ?? url.path)
}

struct Harness {
    let loop: FeedbackLoop
    let emitted: Lines
    let clock: FakeClock
    let dir: URL
}

func makeLoop(
    _ dir: URL = tempDir(), analyzer: (any FeedbackAnalyzer)? = nil, secrets: [String] = [], dailyLimit: Int = 20,
    launch: (@Sendable (String, Int) -> Void)? = nil
) throws -> Harness {
    let emitted = Lines(), clock = FakeClock()
    let loop = try FeedbackLoop(
        dir.appendingPathComponent("fb.sqlite3"), analyzer: analyzer, emit: { emitted.append($0) },
        secrets: secrets, dailyLimit: dailyLimit, clock: { clock.now }, launch: launch)
    return Harness(loop: loop, emitted: emitted, clock: clock, dir: dir)
}

extension FeedbackLoop {
    /// Python `loop.db.execute(sql).fetchall()` for assertions.
    func all(_ sql: String, _ args: [SQLiteValue] = []) throws -> [[SQLiteValue]] { try rows(sql, args) }

    func rules() -> [String] { runFinished().map(\.rule) }
}

func modelTurn(_ text: String?, _ calls: [ToolCall] = []) -> ModelTurn { ModelTurn(text, calls) }
