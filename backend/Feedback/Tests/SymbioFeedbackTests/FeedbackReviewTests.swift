import Foundation
import SymbioCore
import SymbioPlanner
import SymbioUndo
import Testing
@testable import SymbioFeedback

/// tests/test_feedback_review.py (RuntimeController and lessons-CLI cases belong to Runtime / App).

let secret = "syn\"the\\tic\nsecret-key"  // quotes, backslash and newline survive JSON escaping
let body = "PRIVATE_BODY_MARKER"

func allStoredText(_ loop: FeedbackLoop) throws -> String {
    let rows = try loop.all("SELECT signature, payload FROM events")
        + loop.all("SELECT signature, rule FROM incidents") + loop.all("SELECT signature, text FROM lessons")
    return rows.flatMap { $0.map { $0.string ?? "None" } }.joined(separator: "\n")
}

// ---- F01/F02 safe recording

@Test func secretsAndFileBodiesAreNotStoredOrSent() async throws {
    let h = try makeLoop(secrets: [secret])
    h.loop.userInput("\(secret) Bearer abc.def-ghi 저장해줘", newRun: true)
    let arguments = JSON.object([("path", "/a/b.txt"), ("content", .string(body))]).dumps()
    let write = ToolCall("1", "filesystem__write_file", arguments)
    let model = h.loop.wrap(FakeModel([modelTurn(String(repeating: "x", count: 400) + body, [write])]))
    _ = try await model.generate([
        ["role": "user", "content": .string(secret)],
        ["role": "tool", "content": .string(body + String(repeating: "y", count: 600))],
        [
            "role": "assistant", "content": nil,
            "tool_calls": [[
                "id": "1", "type": "function",
                "function": ["name": "filesystem__write_file", "arguments": .string(arguments)],
            ]],
        ],
    ], [])
    h.loop.outcome(.completed(Completed(String(repeating: "z", count: 400) + body)))
    let stored = try allStoredText(h.loop) + h.loop.bundle("sig", [h.loop.runID!])
    for marker in [secret, "abc.def-ghi", body] { #expect(!stored.contains(marker), "\(marker)") }
    for row in try h.loop.all("SELECT payload FROM events") { _ = try JSON.parse(row[0].string!) }
    #expect(h.emitted.all.allSatisfy { !$0.contains(secret) })
}

@Test func bearerRedactionKeepsJSONValid() throws {
    for payload: JSON in [["text": "Bearer tok"], ["text": "a Bearer tok b"], ["messages": [["content": "Bearer tok"]]]] {
        let text = scrub(payload, []).text
        #expect(!text.contains("tok"))
        _ = try JSON.parse(text)
    }
}

@Test func corruptedRowDoesNotHideValidDetection() throws {
    let h = try makeLoop()
    var found: [String] = []
    for _ in 0..<3 {
        h.clock.now += 700
        h.loop.userInput("목록", newRun: true)
        _ = try h.loop.all(
            "INSERT INTO events (ts, session_id, run_id, kind, signature, payload) "
                + "VALUES (?, 's', ?, 'llm_response', NULL, '{\"broken')",
            [.real(h.clock.now), .text(h.loop.runID!)])
        h.loop.outcome(.error("같은 실패"))
        found = h.loop.rules()
    }
    #expect(found.contains("R1"))  // R11 also sees the same request 3x
}

@Test func analysisResultIsSanitizedOnceForEverySink() async throws {
    var result = lessonResult
    result.set("lesson", .string("키 \(secret)를 쓰세요"))
    result.set("root_cause", .string("원인 \(secret)"))
    let h = try makeLoop(analyzer: FakeAnalyzer(result), secrets: [secret])
    h.loop.userInput("x", newRun: true)
    try h.loop.queue("R4", "reject:tool")
    await h.loop.drain()
    #expect(try h.loop.activeLessons().isEmpty && h.loop.candidateLessons().isEmpty)  // carried a credential
    #expect(!(try allStoredText(h.loop)).contains(secret))
    #expect(h.emitted.all.allSatisfy { !$0.contains(secret) })
}

// ---- F03 isolation, F07 lock, F08 symlinks

@Test func recordingFailureDoesNotHideModelResultOrError() async throws {
    let h = try makeLoop()
    h.loop.closeDatabase()  // every write now fails
    let turn = modelTurn("ok")
    #expect(try await h.loop.wrap(FakeModel([turn])).generate([["role": "system", "content": "s"]], []) == turn)
    await #expect(throws: PlannerError.self) {
        try await h.loop.wrap(FakeModel(failing: PlannerError("LLM 인증 실패"))).generate([], [])
    }
    #expect(h.emitted.all.count == 1)  // warned once
}

@Test func closeReleasesResources() async throws {
    let analyzer = FakeAnalyzer(hang: true)
    let h = try makeLoop(analyzer: analyzer)
    h.loop.userInput("x", newRun: true)
    try h.loop.queue("R4", "reject:tool")
    await h.loop.close(waitSeconds: 0)
    #expect(analyzer.closed && h.loop.tasks.isEmpty)
    #expect(throws: (any Error).self) { try h.loop.all("SELECT 1") }
}

@Test func lockedDatabaseDoesNotBlockRecording() throws {
    let h = try makeLoop()
    let other = try SQLiteDatabase(path: h.loop.path.path)
    try other.execute("BEGIN IMMEDIATE")
    let started = Date()
    h.loop.record("a")
    h.loop.record("b")
    #expect(Date().timeIntervalSince(started) < 0.5)
    #expect(h.emitted.all.count == 1)  // one warning that records were dropped
    try other.execute("ROLLBACK")
}

@Test func storeRefusesSymlinksAndKeepsLogsPrivate() throws {
    let dir = tempDir()
    let outside = dir.appendingPathComponent("outside.txt")
    try "original".write(to: outside, atomically: true, encoding: .utf8)
    let store = dir.appendingPathComponent("store")
    try FileManager.default.createDirectory(at: store, withIntermediateDirectories: true)
    try FileManager.default.createSymbolicLink(at: store.appendingPathComponent("feedback.sqlite3"), withDestinationURL: outside)
    #expect(throws: FeedbackStoreError.self) { try FeedbackLoop(store.appendingPathComponent("feedback.sqlite3")) }
    let real = dir.appendingPathComponent("real"), linked = dir.appendingPathComponent("linked")
    try FileManager.default.createDirectory(at: real, withIntermediateDirectories: true)
    try FileManager.default.createSymbolicLink(at: linked, withDestinationURL: real)
    #expect(throws: FeedbackStoreError.self) { try FeedbackLoop(linked.appendingPathComponent("feedback.sqlite3")) }

    let clean = dir.appendingPathComponent("clean")
    try FileManager.default.createDirectory(at: clean, withIntermediateDirectories: true)
    _ = try FeedbackLoop(clean.appendingPathComponent("feedback.sqlite3"))
    let mode = try FileManager.default.attributesOfItem(atPath: clean.appendingPathComponent("feedback.sqlite3").path)[.posixPermissions] as? Int
    #expect(mode == 0o600)
}

// ---- F04 incident context

@Test func analysisStaysWithItsOriginRun() async throws {
    let analyzer = FakeAnalyzer()
    let h = try makeLoop(analyzer: analyzer)
    h.loop.userInput("ORIGIN_RUN", newRun: true)
    let origin = h.loop.runID!
    try h.loop.queue("R4", "reject:tool", [origin])
    h.loop.userInput("UNRELATED_NEXT_RUN", newRun: true)  // starts before the task runs
    await h.loop.drain()
    let bundle = analyzer.calls[0].bundle
    #expect(bundle.contains("ORIGIN_RUN") && !bundle.contains("UNRELATED_NEXT_RUN"))
    #expect(bundle.contains("signature: reject:tool"))
    #expect(try h.loop.all("SELECT run_id FROM events WHERE kind='feedback'")[0][0].string == origin)
}

@Test func repeatedErrorAcrossSessionsKeepsAllEvidence() async throws {
    let analyzer = FakeAnalyzer()
    let path = tempDir().appendingPathComponent("fb.sqlite3")
    var found: [String] = []
    for index in 0..<3 {
        let loop = try FeedbackLoop(path, analyzer: analyzer)
        loop.userInput("SESSION_\(index)", newRun: true)
        loop.outcome(.error("repeated synthetic failure"))
        found = loop.rules()
        await loop.close()
    }
    #expect(found.contains("R1"))
    let r1 = analyzer.calls.map(\.bundle).filter { $0.hasPrefix("signature: error:") }
    #expect(r1.count == 1 && (0..<3).allSatisfy { r1[0].contains("SESSION_\($0)") })
}

@Test func bundleIsCutOnWholeRecords() throws {
    let h = try makeLoop()
    h.loop.bundleLimit = 300
    h.loop.userInput("x", newRun: true)
    for index in 0..<20 { h.loop.record("outcome", ["text": .string("line \(index)")]) }
    let bundle = try h.loop.bundle("sig", [h.loop.runID!])
    let lines = bundle.components(separatedBy: "\n\n")[0].components(separatedBy: "\n").dropFirst(2)
    #expect(!lines.isEmpty)
    for line in lines { _ = try JSON.parse(line) }
    #expect(bundle.contains("line 19") && !bundle.contains("line 0\""))
    #expect(bundle.contains("뺀 오래된 기록"))
}

// ---- F05 lesson snapshot, F06 applied recurrence

@Test func lessonsAreFixedForTheWholeRun() async throws {
    let h = try makeLoop()
    let model = FakeModel([modelTurn("a"), modelTurn("b"), modelTurn("c")])
    let wrapped = h.loop.wrap(model)
    let messages: [JSON] = [["role": "system", "content": "base"]]
    h.loop.userInput("first", newRun: true)
    _ = try await wrapped.generate(messages, [])
    try h.loop.addLesson("sig", "MID_RUN_LESSON")  // an analysis finishing mid-command...
    _ = try h.loop.activateLesson(h.loop.candidateLessons()[0].id)  // ...and adopted at once
    h.loop.userInput("1", newRun: false)  // clarification answer / approval stays in the run
    _ = try await wrapped.generate(messages, [])
    #expect(model.seen.allSatisfy { !($0[0]["content"]!.stringValue!.contains("MID_RUN_LESSON")) })
    h.loop.userInput("next command", newRun: true)
    _ = try await wrapped.generate(messages, [])
    #expect(model.seen[2][0]["content"]!.stringValue!.contains("MID_RUN_LESSON"))
}

@Test func recurrenceCountsOnlyDistinctRunsThatReceivedTheLesson() async throws {
    let h = try makeLoop()
    try h.loop.addLesson("error:x", "지침")
    _ = try h.loop.activateLesson(h.loop.candidateLessons()[0].id)
    let lessonID = try h.loop.activeLessons()[0].id
    for _ in 0..<3 {  // failures in runs that never sent the lesson to the model
        h.loop.userInput("no model call", newRun: true)
        try h.loop.queue("R1", "error:x")
    }
    #expect(try h.loop.activeLessons().map(\.id).contains(lessonID))
    for index in 0..<3 {
        h.loop.userInput("run \(index)", newRun: true)
        _ = try await h.loop.wrap(FakeModel([modelTurn("x")])).generate([["role": "system", "content": "base"]], [])
        try h.loop.queue("R1", "error:x")
        try h.loop.queue("R1", "error:x")  // the same run detected twice counts once
        #expect(try h.loop.activeLessons().map(\.id).contains(lessonID) == (index < 2))
    }
}

// ---- F10 daily budget

@Test func cancelledAnalysisStillCountsAgainstTheDailyLimit() async throws {
    let analyzer = FakeAnalyzer(hang: true)
    let path = tempDir().appendingPathComponent("fb.sqlite3")
    var loop = try FeedbackLoop(path, analyzer: analyzer, dailyLimit: 1)
    loop.userInput("first", newRun: true)
    try loop.queue("R1", "error:first")
    while analyzer.calls.isEmpty { await Task.yield() }  // the request has started
    await loop.close(waitSeconds: 0)  // ...and is cancelled on exit

    loop = try FeedbackLoop(path, analyzer: analyzer, dailyLimit: 1)
    loop.userInput("second", newRun: true)
    try loop.queue("R1", "error:second")
    #expect(analyzer.calls.count == 1)
    #expect(try loop.all("SELECT status FROM incidents ORDER BY id").map { $0[0].string } == ["cancelled", "limited"])
    await loop.close(waitSeconds: 0)
}

// ---- section 4 follow-ups: schema per action

@Test(arguments: [
    ("lesson", JSON.null), ("fix_request", .null), ("root_cause", .string(String(repeating: "x", count: 301))),
])
func analysisSchemaRequiresTheValueItsActionNeeds(field: String, value: JSON) {
    var result = lessonResult
    result.set(field, value)
    if field == "fix_request" { result.set("action", "code_fix") }
    #expect(!isValidAnalysis(result))
    #expect(isValidAnalysis(lessonResult))
}
