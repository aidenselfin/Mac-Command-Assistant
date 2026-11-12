import Foundation
import SymbioCore
import SymbioPlanner
import SymbioUndo
import Testing
@testable import SymbioApp

/// tests/test_llm_mcp_transcript.py, plus the approval case of tests/test_main.py `run_text`.

private func logPath(_ name: String = "transcript.jsonl") throws -> URL {
    try temporaryFolder().appendingPathComponent(name)
}

@Test func concurrentRunsAppendParseableRecordsWithMatchedBoundaries() async throws {
    let path = try logPath()
    let ids = await withTaskGroup(of: String.self) { group in
        for label in ["first", "second"] {
            group.addTask {
                let transcript = TranscriptLog(path)
                transcript.start(label)
                transcript.event("llm_input", [("turn", 1), ("tool_names", [.string(label)])])
                await Task.yield()
                transcript.finish("completed", outcome: .outcome(.completed(Completed(label))))
                return transcript.runID
            }
        }
        return await group.reduce(into: Set<String>()) { $0.insert($1) }
    }
    let records = try readRecords(path)
    #expect(Set(records.compactMap { $0["run_id"]?.stringValue }) == ids)
    #expect(records.allSatisfy { $0["status"] == "completed" && $0["events"]?.arrayValue?.first?["event"] == "start" })
    #expect(records.allSatisfy { $0["started_at"] != nil && $0["ended_at"] != nil })
}

@Test func defaultTranscriptExcludesRawMessagesResultsAndArguments() async throws {
    let path = try logPath()
    let transcript = TranscriptLog(path)
    let model = TranscriptModel(ScriptedModel([ModelTurn(
        "FILE_BODY_SECRET-LIKE and WEB SNAPSHOT CONTENT",
        [ToolCall("call-1", "filesystem__write_file", #"{"path":"/private/secret.md","content":"FILE_BODY_SECRET-LIKE"}"#)])]),
        transcript)
    transcript.start("SECRET-LIKE user message with WEB SNAPSHOT CONTENT")
    _ = try await model.generate([["role": "user", "content": "FILE_BODY_SECRET-LIKE"]], [tool("filesystem__write_file")])
    transcript.finish("completed", outcome: .outcome(.completed(Completed("FILE_BODY_SECRET-LIKE"))))

    let logged = try String(contentsOf: path, encoding: .utf8)
    let record = try #require(try readRecords(path).first)
    #expect(!logged.contains("FILE_BODY_SECRET-LIKE") && !logged.contains("WEB SNAPSHOT CONTENT"))
    #expect(!logged.contains(#""path":"/private/secret.md""#))
    #expect((record["events"]?.arrayValue?[2]["tool_calls"]?.arrayValue?[0]["arguments"]?["chars"]?.intValue ?? 0) > 0)
}

@Test func rawPayloadOptInIncludesDiagnosticPayloads() async throws {
    let path = try logPath()
    let transcript = TranscriptLog(path, includeRawPayloads: true)
    let model = TranscriptModel(ScriptedModel([ModelTurn("diagnostic body", [ToolCall("call", "tool", "{}")])]), transcript)
    transcript.start("diagnostic request")
    _ = try await model.generate(
        [["role": "user", "content": "diagnostic request"], ["role": "user", "content": "OPENAI_API_KEY=test-secret"]],
        [tool("tool")])
    transcript.finish("completed", outcome: .outcome(.completed(Completed("diagnostic body"))))

    let logged = try String(contentsOf: path, encoding: .utf8)
    for part in ["diagnostic request", "diagnostic body", "raw_messages"] { #expect(logged.contains(part)) }
    #expect(!logged.contains("test-secret"))
}

private func runOnce(
    _ model: any ModelClient, transcript: TranscriptLog, manager: FakeManager = FakeManager(),
    journal: UndoJournal? = nil, text: String = "command"
) async throws -> [String] {
    let config = try makeConfig()
    let lines = Lines()
    try await runTranscript(
        text, out: { lines.append($0) }, transcript: transcript, loadConfig: { _ in config },
        makeManager: { _ in manager }, makeModel: { _ in model }, undoJournal: { _ in journal })
    return lines.all
}

@Test(arguments: [
    (Result<ModelTurn, any Error>.success(ModelTurn("done", [])), "completed", false),
    (.failure(PlannerError("planner failed")), "planner_error", false),
    (.failure(CancellationError()), "cancelled", true),
    (.failure(Boom(message: "unexpected")), "exception", true),
])
func runRecordsExactlyOneTerminalStatusForEachExit(_ result: Result<ModelTurn, any Error>, _ status: String, _ raises: Bool)
    async throws
{
    let path = try logPath("\(status).jsonl")
    let model: ScriptedModel
    switch result {
    case let .success(turn): model = ScriptedModel([turn])
    case let .failure(error): model = ScriptedModel(failing: [error])
    }
    var threw = false
    do { _ = try await runOnce(model, transcript: TranscriptLog(path)) } catch { threw = true }
    #expect(threw == raises)
    let records = try readRecords(path)
    #expect(records.count == 1)
    #expect(records.first?["status"]?.stringValue == status)
}

@Test func transcriptWriteFailureDoesNotMaskTheOriginalError() async throws {
    let transcript = TranscriptLog(try logPath("unwritable.jsonl"))
    transcript.append = { _, _ in throw POSIXError(.ENOSPC) }
    await #expect(throws: Boom(message: "original failure")) {
        _ = try await runOnce(ScriptedModel(failing: [Boom(message: "original failure")]), transcript: transcript)
    }
    #expect(transcript.writeError?.isEmpty == false, "\(String(describing: transcript.writeError))")
}

@Test func runPrintsTheApprovalWithoutWriting() async throws {
    let config = try makeConfig()
    let manager = FakeManager(tools: [tool("filesystem__write_file")])
    let journal = try UndoJournal(
        path: config.root.appendingPathComponent("undo-text.sqlite3"), allowedRoots: config.allowedDirectories)
    let lines = Lines()
    try await runTranscript(
        "approved.txt에 저장해줘", out: { lines.append($0) }, transcript: TranscriptLog(try logPath()),
        loadConfig: { _ in config }, makeManager: { _ in manager },
        makeModel: { _ in ScriptedModel([writeTurn(config.path("approved.txt"), "bounded preview")]) },
        undoJournal: { _ in journal })
    let output = lines.text
    for part in ["[APPROVAL REQUIRED]", "filesystem__write_file", "operation: create", "content_chars: 15"] {
        #expect(output.contains(part))
    }
    #expect(manager.names.isEmpty && manager.closed && manager.connectCount == 1 && manager.listCount == 1)
    #expect(try journal.entryCount() == 0)
}

@Test func cliLogFileOptionIsPreserved() throws {
    let args = try #require(parseTranscriptArguments(["command", "--log-file", "/tmp/custom.jsonl"]))
    #expect(args.text == "command" && args.log.path == "/tmp/custom.jsonl" && !args.raw)
    let full = try #require(parseTranscriptArguments([
        "--provider", "groq", "명령", "--allowed-directory", "/a", "--allowed-directory", "/b", rawPayloadFlag,
    ]))
    #expect(full.overrides == ["provider": "groq", "allowed_directories": ["/a", "/b"]] && full.raw)
    #expect(parseTranscriptArguments(["--provider", "other", "x"]) == nil)
    #expect(parseTranscriptArguments([]) == nil)
}
