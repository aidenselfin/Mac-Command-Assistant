import Foundation
import SymbioCore
import SymbioPlanner
@testable import SymbioRuntime
import Testing
@testable import SymbioApp
@testable import SymbioFeedback

/// tests/test_main.py, the `main.py --lessons` cases of tests/test_lesson_candidates.py and the
/// RuntimeController + FeedbackLoop cases of tests/test_feedback.py / test_feedback_review.py.

final class Lines: @unchecked Sendable {  // `items` behind `lock`
    private let lock = NSLock()
    private var items: [String] = []
    func append(_ line: String) { lock.withLock { items.append(line) } }
    var all: [String] { lock.withLock { items } }
    var text: String { all.joined(separator: "\n") }
}

@Test func unexpectedErrorIsRedacted() {
    #expect(!errorMessage(Boom(message: "secret")).contains("secret"))
    #expect(errorMessage(PlannerError("LLM 인증 실패")) == "LLM 인증 실패")
    #expect(errorMessage(ConfigError("설정 오류")) == "설정 오류")
}

@Test func printOutcomeRendersTheQuestionAndTheResumedNumber() {
    let context = ClarificationContext(
        "회의록 요약해줘", "file_candidate", ["/allowed/회의록-1.md", "/allowed/회의록-2.md"], 1)
    let lines = Lines()
    printOutcome(.outcome(.clarification(Clarification("요약할 회의록을 선택해 주세요.", context.options, context))),
                 lines.append)
    printOutcome(.outcome(.completed(Completed("두 번째 회의록을 요약했습니다."))), lines.append)
    #expect(lines.text.contains("[QUESTION] 요약할 회의록을 선택해 주세요."))
    #expect(lines.text.contains("  2. /allowed/회의록-2.md"))
    #expect(lines.text.contains("[DONE] 두 번째 회의록을 요약했습니다."))
}

@Test func approvalOutputAndDescriptionHideUnboundedContent() async throws {
    let content = String(repeating: "x", count: 201) + "SECRET-TAIL"
    let base = try await approval()
    let pending = approval(base, tool: "filesystem__write_file", target: "/allowed/safe.txt", operation: "create",
                           chars: content.count, preview: String(content.prefix(200)), truncated: true)
    let lines = Lines()
    printOutcome(.outcome(.approvalRequired(pending)), lines.append)
    #expect(!lines.text.contains("SECRET-TAIL"))
    #expect(lines.text.contains("content_chars: 212") && lines.text.contains("operation: create"))
    #expect(!String(describing: pending).contains(content))
}

// MARK: - lessons CLI

private func feedbackPath() throws -> URL { try temporaryFolder().appendingPathComponent("fb.sqlite3") }

@Test func cliListsCandidatesAndAdopts() throws {
    let path = try feedbackPath()
    var loop = try FeedbackLoop(path)
    try loop.addLesson("sig", "후보 문장")
    let candidate = try #require(try loop.candidateLessons().first?.id)
    loop.closeDatabase()
    let lines = Lines()
    #expect(lessonsMain(["--lessons"], lines.append, dbPath: path) == 0)
    #expect(lines.text.contains("[후보 lesson]") && lines.text.contains("#\(candidate) 후보 문장"))
    #expect(lessonsMain(["--lesson-on", String(candidate)], lines.append, dbPath: path) == 0)
    #expect(lessonsMain(["--lesson-on", "999"], lines.append, dbPath: path) == 1)
    loop = try FeedbackLoop(path)
    #expect(try loop.activeLessons().map(\.text) == ["후보 문장"])
    loop.closeDatabase()
}

@Test func newSubcommandFormMatchesTheOldFlags() throws {
    let path = try feedbackPath()
    let loop = try FeedbackLoop(path)
    try loop.addLesson("sig", "새 형식")
    let candidate = try #require(try loop.candidateLessons().first?.id)
    loop.closeDatabase()
    let lines = Lines()
    #expect(lessonsMain(["lessons"], lines.append, dbPath: path) == 0)
    #expect(lessonsMain(["lessons", "on", String(candidate)], lines.append, dbPath: path) == 0)
    #expect(lessonsMain(["lessons", "off", String(candidate)], lines.append, dbPath: path) == 0)
    #expect(lines.all.suffix(2) == ["lesson #\(candidate) 채택 (다음 명령부터 적용)", "lesson #\(candidate) 비활성화"])
}

@Test(arguments: ["--lesson-on", "--lesson-off"])
func lessonFlagsNeedAnID(_ flag: String) {
    #expect(lessonsMain([flag], { _ in }) == 2)
    #expect(lessonsMain([flag, "x"], { _ in }) == 2)
}

@Test func unreadableStoreIsReportedNotThrown() throws {
    let folder = try temporaryFolder()  // a folder where the DB file should be
    let lines = Lines()
    #expect(lessonsMain(["--lessons"], lines.append, dbPath: folder) == 1)
    #expect(lines.all == ["[ERROR] 피드백 기록을 읽을 수 없습니다. .voice-action/feedback.sqlite3를 확인하세요."])
}

// MARK: - RuntimeController + FeedbackLoop

final class ManualClock: @unchecked Sendable {  // `value` behind `lock`
    private let lock = NSLock()
    private var value = 1_000_000.0
    var now: Double {
        get { lock.withLock { value } }
        set { lock.withLock { value = newValue } }
    }
}

/// Python `OutcomePlanner`: `run` replays outcomes; nothing else is used.
final class OutcomePlanner: RuntimePlanner, @unchecked Sendable {  // `outcomes` behind `lock`
    let config: Config
    private let lock = NSLock()
    private var outcomes: [PlannerOutcome]
    init(_ outcomes: [PlannerOutcome]) throws {
        config = try makeConfig()
        self.outcomes = outcomes
    }
    func setEventObserver(_ observer: PlannerEventObserver?) {}
    func run(_ userText: String, attachment: String?, previous: Exchange?) async throws -> PlannerOutcome {
        try lock.withLock {
            if outcomes.isEmpty { throw ScriptExhausted() }
            return outcomes.removeFirst()
        }
    }
    func resume(_ context: ClarificationContext, _ selection: String) async throws -> PlannerOutcome { throw ScriptExhausted() }
    func approve(_ context: ApprovalContext) async throws -> PlannerOutcome { throw ScriptExhausted() }
    func reject(_ context: ApprovalContext) -> Completed { Completed("") }
    func undoLastWrite() async throws -> PlannerOutcome { throw ScriptExhausted() }
}

private func makeLoop(emit: @escaping @Sendable (String) -> Void = { _ in }) throws -> (FeedbackLoop, ManualClock) {
    let clock = ManualClock()
    return (try FeedbackLoop(try feedbackPath(), emit: emit, clock: { clock.now }), clock)
}

private func column(_ loop: FeedbackLoop, _ sql: String) throws -> [String] {
    try loop.rows(sql).compactMap { $0.first?.stringValue }
}

@Test func controllerRecordsRunsAndDetects() async throws {
    let (loop, _) = try makeLoop()
    let controller = RuntimeController(
        try OutcomePlanner([.completed(Completed("ok")), .error("실패"), .error("실패"), .error("실패")]),
        feedback: FeedbackAdapter(loop))
    for _ in 0..<4 { _ = try await controller.submit("파일 목록") }
    let kinds = Set(try column(loop, "SELECT kind FROM events"))
    #expect(Set(["user_input", "outcome", "error_outcome", "state"]).isSubset(of: kinds))
    #expect(try column(loop, "SELECT status FROM incidents").first == "no_analyzer")
}

@Test func feedbackCommandGoesToTheLoopNotThePlanner() async throws {
    let (loop, _) = try makeLoop()
    let controller = RuntimeController(try OutcomePlanner([.completed(Completed("답"))]), feedback: FeedbackAdapter(loop))
    _ = try await controller.submit("질문")
    let run = loop.runID, previous = controller.previous
    let reply = try await controller.submit("/feedback 결과가 너무 많아")
    guard case let .outcome(.completed(done)) = reply else { Issue.record("not completed: \(reply)"); return }
    #expect(done.text.contains("피드백"))
    #expect(loop.runID == run && controller.previous == previous)  // context untouched
    #expect(controller.state == .idle)
    guard case .outcome(.error) = try await controller.submit("/feedback") else { Issue.record("accepted empty"); return }
    let bare = RuntimeController(try OutcomePlanner([]), feedback: nil)
    guard case .outcome(.error) = try await bare.submit("/feedback 느려") else { Issue.record("no loop"); return }
}

@Test func feedbackFailureNeverBreaksCommands() async throws {
    let (loop, _) = try makeLoop(emit: { _ in })
    loop.closeDatabase()  // every write now fails
    let controller = RuntimeController(
        try OutcomePlanner([.completed(Completed("ok")), .completed(Completed("ok"))]), feedback: FeedbackAdapter(loop))
    #expect(try await controller.submit("first") == .outcome(.completed(Completed("ok"))))
    #expect(try await controller.submit("second") == .outcome(.completed(Completed("ok"))))
}

@Test func cancellingAClarificationCountsTowardR10() throws {
    let (loop, clock) = try makeLoop()
    let adapter = FeedbackAdapter(loop)
    var seen: [[String]] = []
    for _ in 0..<2 {
        clock.now += 700
        loop.userInput("그 파일 열어줘", newRun: true)
        adapter.cancelled(reason: "clarification")  // what the controller records on "취소"
        seen.append(loop.runFinished().map(\.rule))
    }
    #expect(seen[1].contains("R10"))
}
