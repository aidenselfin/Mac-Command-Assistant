import Foundation
import SymbioCore
import SymbioPlanner
import SymbioUndo
import Testing
@testable import SymbioFeedback

/// tests/test_lesson_candidates.py (the `main.py --lessons` CLI cases belong to the App package).

func systemPrompt(for loop: FeedbackLoop, _ text: String = "명령") async throws -> String {
    let model = FakeModel([modelTurn("ok")])
    loop.userInput(text, newRun: true)
    _ = try await loop.wrap(model).generate([["role": "system", "content": "base"]], [])
    return model.seen[0][0]["content"]!.stringValue!
}

@Test func analysisLessonIsOnlyACandidate() async throws {
    let h = try makeLoop(analyzer: FakeAnalyzer(lessonResult))
    h.loop.userInput("x", newRun: true)
    try h.loop.queue("R4", "reject:tool")
    await h.loop.drain()
    #expect(try h.loop.activeLessons().isEmpty)
    #expect(try h.loop.candidateLessons().map(\.text) == [lessonText])
    #expect(!(try await systemPrompt(for: h.loop)).contains(lessonText))
}

@Test func adoptedLessonIsInjectedFromTheNextCommand() async throws {
    let h = try makeLoop()
    try h.loop.addLesson("sig", "OLD")
    let old = try h.loop.candidateLessons()[0].id
    #expect(try h.loop.activateLesson(old))
    try h.loop.addLesson("sig", "NEW")
    let new = try h.loop.candidateLessons()[0].id
    #expect(try h.loop.activateLesson(new))
    #expect(try h.loop.activeLessons().map(\.text) == ["NEW"])  // same signature replaced
    #expect(try await systemPrompt(for: h.loop).contains("NEW"))
    #expect(try !h.loop.activateLesson(old))  // an inactive one can't come back silently
    #expect(try !h.loop.activateLesson(999))
}

@Test func activationKeepsTheLessonLimit() throws {
    let h = try makeLoop()
    for index in 0...lessonLimit {
        try h.loop.addLesson("sig\(index)", "lesson \(index)")
        _ = try h.loop.activateLesson(h.loop.candidateLessons()[0].id)
    }
    let texts = try h.loop.activeLessons().map(\.text)
    #expect(texts.count == lessonLimit && !texts.contains("lesson 0"))
}

@Test func rejectedCandidateIsGone() throws {
    let h = try makeLoop()
    try h.loop.addLesson("sig", "maybe")
    #expect(try h.loop.deactivateLesson(h.loop.candidateLessons()[0].id))
    #expect(try h.loop.candidateLessons().isEmpty && h.loop.activeLessons().isEmpty)
}

@Test func existingStoreKeepsItsActiveLessons() throws {
    let path = tempDir().appendingPathComponent("fb.sqlite3")
    do {
        let db = try SQLiteDatabase(path: path.path)
        try db.execute(
            "CREATE TABLE lessons (id INTEGER PRIMARY KEY, signature TEXT NOT NULL, text TEXT NOT NULL,"
                + " created_ts REAL NOT NULL, last_seen_ts REAL NOT NULL,"
                + " recurrences INTEGER NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1)")
        try db.execute("INSERT INTO lessons (signature, text, created_ts, last_seen_ts) VALUES ('s', 'kept', 1, 9e12)")
    }
    let loop = try FeedbackLoop(path)
    #expect(try loop.activeLessons().map(\.text) == ["kept"])
    #expect(try loop.candidateLessons().isEmpty)
}
