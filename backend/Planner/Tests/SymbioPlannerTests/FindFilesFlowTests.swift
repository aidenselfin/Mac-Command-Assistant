import Foundation
import SymbioCore
import Testing
@testable import SymbioPlanner

/// tests/test_find_files_flow.py: local__find_files inside the planner.

private let findTools = [tool("filesystem__read_text_file"), tool("filesystem__search_files")]

private func make(_ config: Config, _ turns: [ModelTurn], fallback: ToolResult? = nil, extra: [ToolDefinition] = [])
    throws -> (Planner, ScriptedModel, FakeGateway)
{
    let gateway = FakeGateway(tools: findTools + extra, fallback: fallback)
    let model = ScriptedModel(turns: turns)
    return (try makePlanner(config, gateway, model), model, gateway)
}

private func find(_ keywords: String..., extra: [(String, JSON)] = []) -> ModelTurn {
    let arguments = JSON.object([("keywords", .array(keywords.map(JSON.string)))] + extra)
    return turn(nil, ToolCall("find", "local__find_files", arguments.dumps(JSONDumpOptions(ensureASCII: false))))
}

private func read(_ path: String, _ extra: [(String, JSON)] = []) -> ModelTurn {
    turn(nil, ToolCall("read", "filesystem__read_text_file", JSON.object([("path", .string(path))] + extra).dumps()))
}

private func listTurn(_ paths: String...) -> ModelTurn {
    turn(nil, callJSON("list", "local__list_files", ["paths": .array(paths.map(JSON.string)), "sort_by": "modified"]))
}

private func lastTool(_ model: ScriptedModel, _ index: Int) -> String { toolMessages(model, index).last ?? "" }

@Test func findFilesIsOfferedAndSingleMatchContinues() async throws {
    let config = try makeConfig()
    try writeText("내용", config.path("예산안.md"))
    let (planner, model, _) = try make(config, [find("예산안"), turn("찾았습니다")])
    #expect(try await planner.run("예산안 어디 있어?") == done("찾았습니다"))
    #expect(model.calls[0].toolNames.contains("local__find_files"))
    #expect(parse(model.calls[1].lastContent)["decision"] == "single")
}

@Test func fewCandidatesAskWithLabelsAndResumeSelectsTheFile() async throws {
    let config = try makeConfig()
    for name in ["계획-초안.md", "계획-최종.md"] { try writeText("내용", config.path(name)) }
    let (planner, _, _) = try make(config, [find("계획")])
    let outcome = try #require(try await planner.run("계획 문서 요약해줘").clarification)
    #expect(outcome.context.kind == "file_candidate")
    #expect(outcome.options.count == 2 && outcome.labels.count == 2)
    #expect(outcome.options[0].hasPrefix(config.root.path) && outcome.labels[0].contains("·"))
    #expect(outcome.shownOptions == outcome.labels)
    let chosen = outcome.options[0]
    let (second, _, gateway) = try make(config, [read(chosen), turn("요약")], fallback: ok("본문"))
    #expect(try await second.resume(outcome.context, chosen) == done("요약"))
    #expect(gateway.calls.count == 1 && gateway.calls[0].arguments == ["path": .string(chosen)])
}

@Test func manyCandidatesAreNotListedButNarrowed() async throws {
    let config = try makeConfig()
    for index in 1...8 { try writeText("x", config.path("임시-\(index).txt")) }
    let (planner, _, _) = try make(config, [find("임시")])
    let outcome = try #require(try await planner.run("임시 파일 읽어줘").clarification)
    #expect(outcome.context.kind == "narrow_search")
    #expect(outcome.question.contains("8개") && outcome.question.contains("임시-1 ~ 임시-8"))
    #expect(([outcome.question] + outcome.shownOptions).allSatisfy { !$0.contains("임시-3") })
    #expect(outcome.options[0].hasSuffix(".txt"))  // the newest file is offered as a button
}

@Test func narrowFreeTextRerunsTheSearchAndOptionPathSelectsIt() async throws {
    let config = try makeConfig()
    let a = config.path("a.txt")
    let context = ClarificationContext("임시 파일 읽어줘", "narrow_search", [a], 1)
    var (planner, model, _) = try make(config, [find("임시-12"), turn("끝")])
    #expect(try await planner.resume(context, "임시-12번") == done("끝"))
    #expect(model.calls[0].system.contains("사용자가 좁힌 조건(누적): 임시-12번"))
    try writeText("x", a)
    (planner, model, _) = try make(config, [read(a), turn("읽음")], fallback: ok("x"))
    #expect(try await planner.resume(context, a) == done("읽음"))
    #expect(model.calls[0].system.contains("사용자가 선택한 파일"))
}

@Test func narrowResumeRejectsBlankAndOtherKindsRejectFreeText() async throws {
    let config = try makeConfig()
    let (planner, _, _) = try make(config, [])
    #expect(try await planner.resume(ClarificationContext("x", "narrow_search", [], 1), "  ").errorMessage != nil)
    let pick = ClarificationContext("x", "file_candidate", [config.path("a.md")], 1)
    #expect(try await planner.resume(pick, "아무거나").errorMessage != nil)
}

@Test func clarificationBudgetAppliesToFindFilesQuestions() async throws {
    let config = try makeConfig()
    for name in ["계획-초안.md", "계획-최종.md"] { try writeText("x", config.path(name)) }
    let (planner, _, _) = try make(config, [find("계획")])
    let outcome = try await planner.runLoop("계획 요약해줘", priorClarifications: 2)
    #expect(outcome.errorMessage?.contains("clarification 최대") == true)
}

@Test func findFilesIsRefusedAfterTheUserPickedAFile() async throws {
    let config = try makeConfig()
    let a = config.path("a.md")
    try writeText("x", a)
    let (planner, model, _) = try make(config, [find("a"), read(a), turn("읽음")], fallback: ok("x"))
    #expect(try await planner.resume(ClarificationContext("a 읽어줘", "file_candidate", [a], 1), a) == done("읽음"))
    #expect(model.calls[1].lastContent.contains("이미 대상 파일이 정해졌습니다"))
}

@Test func bigFileAnswerSaysItWasOnlyPartlyRead() async throws {
    let config = try makeConfig()
    let big = config.path("큰파일.txt")
    try writeText("x", big)
    var (planner, _, _) = try make(
        config, [read(big), turn("요약")], fallback: ToolResult(ok: true, text: String(repeating: "가", count: 1234), truncated: true))
    let outcome = try await planner.run("큰파일.txt 요약해줘")
    #expect(outcome.text.hasPrefix("요약"))
    #expect(outcome.text.contains("파일 전체가 아니라 앞부분 약 1,234자만"))
    (planner, _, _) = try make(config, [read(big), turn("요약")], fallback: ok("짧음"))
    #expect(try await planner.run("큰파일.txt 요약해줘") == done("요약"))
}

@Test(arguments: ["예산안 알려줘", "회의록 요약해줘"])
func rawSearchIsNeverOfferedToTheModel(text: String) async throws {
    let (planner, model, _) = try make(try makeConfig(), [turn("끝")])
    _ = try? await planner.run(text)
    let offered = try #require(model.calls.last).toolNames
    #expect(!offered.contains("filesystem__search_files") && offered.contains("local__find_files"))
}

@Test(arguments: [
    ("head", "1\n2\n3", "파일 전체가 아니라 앞 3줄만"),
    ("tail", "7\n8\n9", "파일 전체가 아니라 뒤 3줄만"),
    ("head", "1\n2", ""),  // the file ended before the limit: it was all read
])
func headAndTailReadsSayWhichPartWasRead(key: String, text: String, expected: String) async throws {
    let config = try makeConfig()
    let big = config.path("로그.txt")
    try writeText("x", big)
    let (planner, _, _) = try make(config, [read(big, [(key, 3)]), turn("요약")], fallback: ok(text))
    let outcome = try await planner.run("로그.txt 요약해줘")
    if expected.isEmpty { #expect(outcome == done("요약")) } else { #expect(outcome.text.contains(expected)) }
}

@Test func anIncompleteSearchForMeetingNotesNamesWhatItCouldNotCheck() async throws {
    let config = try makeConfig()
    try writeText("%PDF", config.path("보고서.pdf"))  // a document whose text is never searched
    var (planner, _, _) = try make(config, [find("회의록")])
    let outcome = try #require(try await planner.run("회의록 요약해줘").completed)
    #expect(outcome.text.hasPrefix("검사한 범위에서는 일치하는 파일을 찾지 못했습니다."))
    #expect(outcome.text.contains("pdf·docx 등 문서 1개"))
    try FileManager.default.removeItem(atPath: config.path("보고서.pdf"))
    (planner, _, _) = try make(config, [find("회의록")])
    #expect(try await planner.run("회의록 요약해줘").text == "요청과 일치하는 파일을 찾지 못했습니다. 파일명을 더 구체적으로 알려주세요.")
}

@Test func partialReadCountsOnlyWhatWasReadNotTheCutOffMarker() async throws {
    let config = try makeConfig()
    let big = config.path("큰.txt")
    try writeText("x", big)
    let (planner, _, _) = try make(
        config, [read(big), turn("요약")],
        fallback: ToolResult(ok: true, text: String(repeating: "가", count: 50_000) + truncationMarker, truncated: true))
    let outcome = try await planner.run("큰.txt 요약해줘")
    #expect(outcome.text.contains("앞부분 약 50,000자만") && !outcome.text.contains("50,012"))
}

@Test func filesTheUserNamedInFullAreAllTargetsNotAChoice() async throws {
    let config = try makeConfig()
    for name in ["a.csv", "b.csv", "c.csv", "d.csv"] { try writeText("x,y\n1,2\n", config.path(name)) }
    let (planner, model, _) = try make(config, [find("a.csv", "b.csv", "c.csv"), turn("세 파일을 처리합니다")])
    #expect(try await planner.run("a.csv, b.csv, c.csv를 모두 json으로 바꿔줘") == done("세 파일을 처리합니다"))
    let told = parse(lastTool(model, 1))
    #expect(told["decision"] == "named" && told["paths"]?.arrayValue?.count == 3)
}

@Test func twoFilesOfOneNameInTwoFoldersAreStillAChoice() async throws {
    let config = try makeConfig()
    for folder in ["가", "나"] { try writeText("x", config.path("\(folder)/a.csv")) }
    let (planner, _, _) = try make(config, [find("a.csv")])
    #expect(try await planner.run("a.csv를 json으로 바꿔줘").clarification?.options.count == 2)
}

@Test func candidatesTheUserDidNotNameAreStillAskedAbout() async throws {
    let config = try makeConfig()
    for name in ["계획-초안.md", "계획-최종.md"] { try writeText("x", config.path(name)) }
    let (planner, _, _) = try make(config, [find("계획")])
    #expect(try await planner.run("계획 문서 읽어줘").clarification != nil)
}

@Test func aDeleteNamedByAPatternDoesNotAskWhichFile() async throws {
    let config = try makeConfig()
    for name in ["임시-1.txt", "임시-2.txt"] { try writeText("x", config.path("삭제연습/\(name)")) }
    let (planner, model, _) = try make(config, [find("삭제연습", "txt"), turn("삭제를 준비합니다")])
    #expect(try await planner.run("삭제연습 폴더의 *.txt 파일을 모두 삭제해줘") == done("삭제를 준비합니다"))
    let told = parse(lastTool(model, 1))
    #expect(told["decision"] == "pattern" && (told["note"]?.stringValue ?? "").contains("directory와 pattern"))
}

@Test func aPlainSearchWithAStarInItIsNotTakenForAPatternDelete() async throws {
    let config = try makeConfig()
    for name in ["계획-초안.md", "계획-최종.md"] { try writeText("x", config.path(name)) }
    let (planner, _, _) = try make(config, [find("계획")])
    #expect(try await planner.run("계획* 문서 읽어줘").clarification != nil)  // not a delete
}

@Test func rankedRequestListsReadsAndAnswersWithoutAsking() async throws {
    let config = try makeConfig()
    let v1 = config.path("계약서_v1.txt"), v2 = config.path("계약서_v2.txt")
    try writeText("금액", v1)
    try writeText("금액", v2)
    let (planner, model, _) = try make(config, [find("계약서"), listTurn(v1, v2), read(v2), turn("v2입니다")], fallback: ok("금액"))
    #expect(try await planner.run("가장 최근에 수정한 계약서의 금액이 얼마야?") == done("v2입니다"))
    let sent = model.calls[1].lastContent
    #expect(sent.contains(#""decision": "ranked""#) && sent.contains("계약서_v1.txt"))
}

@Test func rankedRequestMustReadACandidateBeforeAnswering() async throws {
    let config = try makeConfig()
    for name in ["계약서_v1.txt", "계약서_v2.txt"] { try writeText("금액", config.path(name)) }
    let (planner, _, _) = try make(config, [find("계약서"), turn("1천만원입니다")])
    #expect(try await planner.run("가장 최근에 수정한 계약서의 금액이 얼마야?").errorMessage != nil)
}

@Test func rankedRequestCannotWriteBeforeReadingACandidate() async throws {
    let config = try makeConfig()
    for name in ["계약서_v1.txt", "계약서_v2.txt"] { try writeText("금액", config.path(name)) }
    let write = turn(nil, callJSON("w", "filesystem__write_file", ["path": .string(config.path("요약.txt")), "content": "x"]))
    let (planner, model, _) = try make(config, [find("계약서"), write, turn("끝")], extra: [tool("filesystem__write_file", "w")])
    _ = try? await planner.run("가장 최근 계약서 요약을 요약.txt에 저장해줘")
    #expect(model.calls[2].lastContent.contains("읽기 전에는 파일을 바꿀 수 없습니다"))
}

@Test func rankedMoveStillAsksWhichFile() async throws {
    let config = try makeConfig()
    for name in ["계약서_v1.txt", "계약서_v2.txt"] { try writeText("금액", config.path(name)) }
    let (planner, _, _) = try make(config, [find("계약서")])
    #expect(try await planner.run("가장 최근 계약서를 보관 폴더로 옮겨줘").clarification != nil)
}

@Test func singleListFilesPickBecomesADraggableFoundPath() async throws {
    let config = try makeConfig()
    try FileManager.default.removeItem(atPath: config.path("한글 메모.txt"))
    try writeText("내용", config.path("최근.txt"))
    let pick = turn(nil, callJSON("list", "local__list_files", ["limit": 1, "sort_by": "modified"]))
    let (planner, _, _) = try make(config, [pick, turn("최근 파일입니다")])
    #expect(try await planner.run("가장 최근 파일 줘").completed?.foundPaths == [config.path("최근.txt")])
}
