import Foundation
import SymbioConvert
import SymbioCore
import Testing
@testable import SymbioPlanner

/// tests/test_recovery_loop.py and tests/test_planner_tools_review.py.

private let readTool = "filesystem__read_text_file"
private let docTool = "local__read_document"

private func recoveryGateway(_ extra: [ToolDefinition] = [], results: [ToolResult] = [], fallback: ToolResult? = nil)
    -> FakeGateway
{
    FakeGateway(tools: [tool(readTool)] + extra, results: results, fallback: fallback)
}

private func read(_ path: String, _ id: String = "r", tool: String = readTool) -> ModelTurn {
    turn(nil, callJSON(id, tool, ["path": .string(path)]))
}

private func allText(_ model: ScriptedModel) -> String {
    model.calls.flatMap(\.messages).map { $0["content"]?.stringValue ?? "None" }.joined(separator: "\n")
}

private func formatError(_ message: String) -> ScriptedModel.Step { .fail(PlannerError.modelFormat(message)) }

/// Python `iter([...])` clock.
final class Ticks: @unchecked Sendable {  // used from one task at a time
    private var values: [Double]
    init(_ values: [Double]) { self.values = values }
    func next() -> Double { values.removeFirst() }
}

@Test func formatErrorGoesBackToTheModelInsteadOfEnding() async throws {
    let config = try makeConfig()
    let model = ScriptedModel([formatError("clarification과 MCP 도구를 동시에 요청할 수 없습니다."), .turn(turn("안녕하세요"))])
    #expect(try await makePlanner(config, recoveryGateway(), model).run("인사해줘") == done("안녕하세요"))
    #expect(model.calls[1].last["role"] == "system")
    #expect(model.calls[1].lastContent.contains("동시에"))
}

@Test func missingNFDKoreanFileListsRealNamesAndStopsADoomLoop() async throws {
    let config = try makeConfig()
    try writeText("x", config.path("회의록 정리.docx".decomposedStringWithCanonicalMapping))
    let broken = config.path("회의록 졍리.docx")  // one syllable mangled while copying the NFD name
    let model = ScriptedModel(turns: [
        read(broken, "a", tool: docTool), read(broken, "b", tool: docTool), read(broken, "c", tool: docTool),
        turn("회의록 정리.docx를 찾았지만 경로를 잘못 적어 읽지 못했습니다."),
    ])
    let outcome = try await makePlanner(config, recoveryGateway(), model).run("정리 문서 내용 알려줘")
    let message = try #require(outcome.errorMessage)
    #expect(message.contains("반복"))
    #expect(message.contains("경로를 잘못 적어"))  // the model's own honest report
    let seen = allText(model)
    #expect(seen.precomposedStringWithCanonicalMapping.contains("회의록 정리.docx"))  // a real name to copy
    #expect(seen.contains("그대로 복사"))
    #expect(seen.contains("같은 시도가 같은 결과"))
    #expect(model.count == 4)  // stopped at the third repeat, not at 20 rounds
    #expect(model.calls.last?.allowTools == false)
}

@Test func nfcRequestReadsTheFileUnderItsOnDiskNFDName() async throws {
    let config = try makeConfig()
    let real = "보고서.txt".decomposedStringWithCanonicalMapping
    try writeText("내용", config.path(real))
    let gateway = recoveryGateway(fallback: ok("내용"))
    let model = ScriptedModel(turns: [read(config.path("보고서.txt".precomposedStringWithCanonicalMapping)), turn("내용입니다")])
    #expect(try await makePlanner(config, gateway, model).run("보고서.txt 읽어줘") == done("내용입니다"))
    #expect(gateway.calls.last?.arguments["path"]?.stringValue?.unicodeScalars.elementsEqual(config.path(real).unicodeScalars) == true)
}

@Test func missingFileHintNeverListsNamesOutsideAllowedFolders() async throws {
    let config = try makeConfig()
    let outside = config.root.deletingLastPathComponent().appendingPathComponent("secret")
    try writeText("x", outside.appendingPathComponent("salary-2026.txt").path)
    let gateway = FakeGateway(tools: [tool(readTool)], outcomes: [.failure(ToolError.rejected("허용 경로 밖 접근입니다."))])
    let model = ScriptedModel(turns: [read(outside.appendingPathComponent("salary.txt").path), turn("못 읽었습니다")])
    _ = try await makePlanner(config, gateway, model).run("salary.txt 읽어줘")
    #expect(!allText(model).contains("salary-2026"))
}

@Test func repeatedReadsWithNewResultsAreNotTreatedAsALoop() async throws {
    let config = try makeConfig()
    try writeText("x", config.path("log.txt"))
    let gateway = recoveryGateway(results: (0..<3).map { ok("줄 \($0)") })
    let path = config.path("log.txt")
    let model = ScriptedModel(turns: [read(path, "a"), read(path, "b"), read(path, "c"), turn("끝")])
    #expect(try await makePlanner(config, gateway, model).run("log.txt 읽어줘") == done("끝"))
    #expect(!allText(model).contains("같은 시도"))
}

@Test func twoFailedRecoveriesHandTheConversationToTheStrongerModel() async throws {
    let config = try makeConfig()
    let main = ScriptedModel([formatError("형식 오류 1"), formatError("형식 오류 2")])
    let strong = ScriptedModel(turns: [turn("상위 모델 답")])
    let events = Recorder<PlannerEvent>()
    let planner = try makePlanner(config, recoveryGateway(), main, events: events, escalation: strong)
    #expect(try await planner.run("인사해줘") == done("상위 모델 답"))
    let handed = try #require(strong.calls.last).messages
    #expect(handed.filter { ($0["content"]?.stringValue ?? "").contains("형식 오류") }.count == 2)  // failures kept
    #expect(events.all.contains { $0.type == .recovering })
}

@Test func withoutAStrongerModelTheMainModelKeepsRecovering() async throws {
    let config = try makeConfig()
    let main = ScriptedModel([formatError("오류 1"), formatError("오류 2"), .turn(turn("결국 답"))])
    #expect(try await makePlanner(config, recoveryGateway(), main).run("인사해줘") == done("결국 답"))
}

@Test func endlessFormatErrorsStillEnd() async throws {
    let config = try makeConfig()
    let main = ScriptedModel(Array(repeating: formatError("늘 같은 오류"), count: 30))
    let outcome = try await makePlanner(config, recoveryGateway(), main).run("인사해줘")
    #expect(outcome.errorMessage != nil)
    #expect(main.count < config.maxToolRounds)
}

@Test func secondEmptyReplyGoesToTheStrongerModel() async throws {
    let config = try makeConfig()
    let main = ScriptedModel(turns: [turn(""), turn(nil)])
    let strong = ScriptedModel(turns: [turn("답")])
    #expect(try await makePlanner(config, recoveryGateway(), main, escalation: strong).run("인사해줘") == done("답"))
}

@Test func emptyReplyAfterRetryRaises() async throws {
    let config = try makeConfig()
    let main = ScriptedModel(turns: [], repeating: turn(" "))
    await #expect(throws: PlannerError("LLM 최종 응답이 비어 있습니다.")) {
        try await makePlanner(config, recoveryGateway(), main).run("인사해줘")
    }
    #expect(main.count == 2)
}

@Test func runTimeLimitEndsTheCommand() async throws {
    let config = try makeConfig(maxRunSeconds: 90)
    let model = ScriptedModel(Array(repeating: formatError("오류"), count: 5))
    let planner = try makePlanner(config, recoveryGateway(), model)
    let ticks = Ticks([0, 10, 100, 200, 300])
    planner.clock = { ticks.next() }
    let outcome = try await planner.run("none.txt 읽어줘")
    #expect(outcome.errorMessage?.contains("시간 한도") == true)
    #expect(outcome.errorMessage == "시간 한도 초과 (90초); 추가 모델 호출 없이 종료했습니다.")
}

@Test func recoveryStateSurvivesAnApproval() async throws {
    let config = try makeConfig()
    let main = ScriptedModel([
        formatError("오류 1"),
        .turn(writeTurn(config.path("a.txt"), "x", id: "w")),
        formatError("오류 2"),
    ])
    let strong = ScriptedModel(turns: [turn("저장했습니다")])
    let gateway = recoveryGateway([tool("filesystem__write_file", "w")], fallback: ok("ok"))
    let planner = try makePlanner(config, gateway, main, escalation: strong)
    let pending = try #require(try await planner.run("a.txt에 x 저장해줘").approval)
    #expect(try await planner.approve(pending.context).completed?.text == "저장했습니다")
}

@Test func slowModelCallIsCutAtTheTimeLimit() async throws {
    let config = try makeConfig(maxRunSeconds: 0.05)
    let model = ScriptedModel([.wait(5, turn("늦은 답"))])
    let started = Date()
    let outcome = try await makePlanner(config, recoveryGateway(), model).run("인사해줘")
    #expect(outcome.errorMessage?.contains("시간 한도") == true)
    #expect(Date().timeIntervalSince(started) < 2)
}

@Test func cancellingTheCommandStopsTheModelCall() async throws {
    let config = try makeConfig()
    let model = ScriptedModel([.wait(5, turn("늦은 답"))])
    let planner = try makePlanner(config, recoveryGateway(), model)
    let task = Task { try await planner.run("인사해줘") }
    try await Task.sleep(nanoseconds: 50_000_000)
    task.cancel()
    await #expect(throws: CancellationError.self) { try await task.value }
}

@Test func timeBeforeAnApprovalStillCountsAfterIt() async throws {
    let config = try makeConfig(maxRunSeconds: 90)
    let model = ScriptedModel(turns: [writeTurn(config.path("a.txt"), "x", id: "w"), turn("저장했습니다")])
    let gateway = recoveryGateway([tool("filesystem__write_file", "w")], fallback: ok("ok"))
    let planner = try makePlanner(config, gateway, model)
    // 80 s before the approval; the user then waits 1000 s; 20 s more after it is over 90.
    let ticks = Ticks([0, 0, 80, 1080, 1100])
    planner.clock = { ticks.next() }
    let pending = try #require(try await planner.run("a.txt에 x 저장해줘").approval)
    let message = try #require(try await planner.approve(pending.context).errorMessage)
    #expect(message.contains("시간 한도"))
    #expect(message.contains("승인된 파일 변경은 완료"))
}

// MARK: - tests/test_planner_tools_review.py

@Test(arguments: [
    ("https://example.com 페이지를 읽고 요약해줘", "web__browser_navigate"),
    ("폴더 파일 목록 보여줘", "filesystem__list_directory"),
])
func hiddenConversionCallIsRefusedWithoutApproval(request: String, offered: String) async throws {
    let config = try makeConfig()
    try writeText("a\n1\n", config.path("표.csv"))
    let gateway = FakeGateway(tools: [tool(offered, "t")])
    let convert: JSON = ["source": .string(config.path("표.csv")), "target_format": "json"]
    let model = ScriptedModel(turns: [turn(nil, callJSON("c0", convertTool, convert))], repeating: turn("끝"))
    let outcome = try await makePlanner(config, gateway, model).run(request)
    #expect(outcome.approval == nil)
    #expect(!model.calls[0].toolNames.contains(convertTool))
    #expect(toolMessages(model, 1).first?.hasPrefix("tool_error:") == true)  // policy or not-offered refusal
    #expect(!fileExists(config.path("표.json")))
    #expect(gateway.calls.isEmpty)
}

@Test func hiddenCallBesideAnExposedOneOnlyRunsTheExposedOne() async throws {
    let config = try makeConfig()
    try writeText("a\n1\n", config.path("표.csv"))
    let gateway = FakeGateway(tools: [tool("filesystem__list_directory", "t")], fallback: ok("[FILE] 표.csv"))
    let convert: JSON = ["source": .string(config.path("표.csv")), "target_format": "json"]
    let model = ScriptedModel(
        turns: [
            turn(
                nil, callJSON("c0", "filesystem__list_directory", ["path": .string(config.root.path)]),
                callJSON("c1", convertTool, convert)),
            turn("목록입니다"),
        ], repeating: turn("끝"))
    let outcome = try await makePlanner(config, gateway, model).run("폴더 파일 목록 보여줘")
    #expect(outcome.approval == nil)
    #expect(gateway.calls.count == 1)
    let contents = toolMessages(model, model.count - 1)
    #expect(contents[0] == "[FILE] 표.csv" && contents[1].contains("제공되지 않은 도구"))
}
