import Foundation
import SymbioCore
import Testing
@testable import SymbioPlanner

/// Rules that left the system prompt (deleteBlock, part of searchBlock) for code checks, 2026-11-18.

private func make(_ config: Config, _ turns: [ModelTurn], extra: [ToolDefinition] = [])
    throws -> (Planner, ScriptedModel)
{
    let gateway = FakeGateway(tools: [tool("filesystem__read_text_file")] + extra, fallback: ok("본문"))
    let model = ScriptedModel(turns: turns)
    return (try makePlanner(config, gateway, model), model)
}

private func ask(_ question: String) -> ModelTurn {
    ModelTurn(nil, [], ClarificationRequest(question, [], "free_text", "q"))
}

private func find(_ keywords: [String], _ extra: [(String, JSON)] = []) -> ModelTurn {
    let arguments = JSON.object([("keywords", .array(keywords.map(JSON.string)))] + extra)
    return turn(nil, ToolCall("find", "local__find_files", arguments.dumps(JSONDumpOptions(ensureASCII: false))))
}

private func delete(_ id: String, _ paths: [String]) -> ToolCall {
    callJSON(id, "local__delete_files", ["paths": .array(paths.map(JSON.string))])
}

@Test func deleteCallsInOneReplyBecomeOneApproval() async throws {
    let config = try makeConfig()
    for name in ["a.log", "b.log"] { try writeText("x", config.path(name)) }
    let (a, b) = (config.path("a.log"), config.path("b.log"))
    let (planner, _) = try make(config, [ModelTurn(nil, [delete("1", [a]), delete("2", [b, a])])])
    let approval = try #require(try await planner.run("a.log와 b.log 삭제해줘").approval)
    #expect(approval.context.toolCalls.count == 1)
    #expect(parse(approval.context.pendingCall.arguments)["paths"] == .array([.string(a), .string(b)]))
    // A directory+pattern call is left alone.
    let pattern = callJSON("3", "local__delete_files", ["directory": "/r", "pattern": "*.txt"])
    #expect(mergeDeleteCalls([pattern, delete("4", [a])]).count == 2)
}

@Test func askingWhichFoundFileToDeleteIsTurnedBack() async throws {
    let config = try makeConfig()
    for name in ["로그-1.log", "로그-2.log"] { try writeText("x", config.path(name)) }
    let call = delete("d", [config.path("로그-1.log"), config.path("로그-2.log")])
    let (planner, model) = try make(config, [find(["로그"]), ask("어떤 파일을 지울지 알려 주세요."), turn(nil, call)])
    let outcome = try await planner.run("로그 파일 지워줘")
    #expect(outcome.approval?.operation == "delete")
    #expect(model.calls[2].lastContent.contains("삭제 대상은 묻지 말고"))
}

@Test func claimingADeleteBeforeApprovalIsTurnedBackThenStops() async throws {
    let config = try makeConfig()
    try writeText("x", config.path("old.log"))
    let (planner, model) = try make(config, [turn("old.log를 삭제했습니다."), turn("삭제 완료했습니다.")])
    let outcome = try await planner.run("old.log 삭제해줘")
    #expect(model.calls[1].lastContent.contains("아직 아무 파일도 삭제되지 않았습니다"))
    #expect(outcome.errorMessage?.contains("승인된 삭제 없이") == true)
    #expect(fileExists(config.path("old.log")))
    // An honest "could not" is not a claim.
    let (honest, _) = try make(config, [turn("파일이 잠겨 있어 삭제를 완료하지 못했습니다.")])
    #expect(try await honest.run("old.log 삭제해줘").completed != nil)
}

@Test func aNamedDestinationFolderIsNotAskedFor() async throws {
    let config = try makeConfig()
    try FileManager.default.createDirectory(atPath: config.path("보관"), withIntermediateDirectories: true)
    let roots = config.allowedDirectories
    #expect(namedDestination("a.txt를 보관 폴더로 이동해줘", roots) == config.path("보관"))
    #expect(namedDestination("새 보고 폴더를 만들어줘", roots) == config.path("보고"))
    #expect(namedDestination("보관 폴더 내용 알려줘", roots) == nil)  // not a move/copy/mkdir
    #expect(namedDestination("a.txt를 옮겨줘", roots) == nil)  // no folder named: may ask
    try writeText("x", config.path("a.txt"))
    let move = turn(nil, callJSON("m", "local__move_file", [
        "source": .string(config.path("a.txt")), "destination": .string(config.path("보관/a.txt")),
    ]))
    let (planner, model) = try make(config, [ask("보관 폴더의 절대 경로를 알려 주세요."), move])
    _ = try await planner.run("a.txt를 보관 폴더로 이동해줘")
    #expect(model.calls[1].lastContent.contains(config.path("보관")))
}

@Test func askingAboutEveryMatchListsThemInsteadOfAsking() async throws {
    let config = try makeConfig()
    for name in ["memo1.txt", "memo2.txt", "memo3.txt"] { try writeText("x", config.path(name)) }
    let (planner, model) = try make(config, [find(["memo"]), turn("세 파일 요약")])
    #expect(try await planner.run("memo 파일들을 읽고 요약해줘").completed != nil)
    let result = parse(model.calls[1].lastContent)
    #expect(result["decision"] == "all" && result["candidates"]?.arrayValue?.count == 3)
    // One file of several still asks.
    let (one, _) = try make(config, [find(["memo"])])
    #expect(try await one.run("memo 읽어줘").clarification?.context.kind == "file_candidate")
}

@Test func findResultSaysWhatToDoAndEmptyArgumentsAreDropped() async throws {
    let config = try makeConfig()
    try writeText("x", config.path("예산안.md"))
    let (planner, model) = try make(config, [find(["예산안"], [("modified_after", ""), ("extensions", [])]), turn("찾음")])
    _ = try await planner.run("예산안 어디 있어?")
    let single = parse(model.calls[1].lastContent)
    #expect(single["decision"] == "single" && single["note"]?.stringValue?.contains("바로 진행") == true)
    let (none, noneModel) = try make(config, [find(["없는이름"]), turn("없음")])
    _ = try await none.run("없는이름 어디 있어?")
    #expect(parse(noneModel.calls[1].lastContent)["note"]?.stringValue?.contains("찾지 못했다고") == true)
}

@Test func aContentClueLeftOutOfTheSearchIsPointedOutOnceBeforeAsking() async throws {
    let config = try makeConfig()
    try writeText("점심", config.path("memo1.txt"))
    try writeText("분기 예산 확정", config.path("memo2.txt"))
    let again = find(["memo"], [("content_keywords", ["분기 예산"])])
    let (planner, model) = try make(config, [find(["memo"]), again, turn("읽음")])
    _ = try await planner.run("분기 예산 얘기가 적힌 메모를 읽어줘")
    let retry = parse(model.calls[1].lastContent)
    #expect(retry["decision"] == "retry_with_content" && retry["note"]?.stringValue?.contains("'분기 예산'") == true)
    #expect(parse(model.calls[2].lastContent)["decision"] == "single")
    // Only once: a second plain search goes on to the user's choice.
    let (twice, _) = try make(config, [find(["memo"]), find(["memo"])])
    #expect(try await twice.run("분기 예산 얘기가 적힌 메모를 읽어줘").clarification?.context.kind == "file_candidate")
}

@Test func readingAnotherFileThanTheChosenOneIsToldTheChosenPath() async throws {
    let config = try makeConfig()
    for name in ["회의록-1.md", "회의록-2.md"] { try writeText("x", config.path(name)) }
    let (planner, _) = try make(config, [find(["회의록"])])
    let question = try #require(try await planner.run("회의록 요약해줘").clarification)
    let chosen = question.options[0]
    let wrong = turn(nil, callJSON("r", "filesystem__read_text_file", ["path": .string(config.path("회의록.md"))]))
    let (second, model) = try make(config, [wrong, turn(nil, callJSON("r2", "filesystem__read_text_file", ["path": .string(chosen)])), turn("요약")])
    _ = try await second.resume(question.context, chosen)
    #expect(model.calls[1].lastContent.contains("이 경로를 그대로 쓰세요: \(chosen)"))
}

@Test func askingForEveryFileWithAContentClueListsThemWithoutARetry() async throws {
    let config = try makeConfig()
    for name in ["memo1.txt", "memo2.txt"] { try writeText("환불", config.path(name)) }
    let (planner, model) = try make(config, [find(["memo"]), turn("둘 다")])
    _ = try await planner.run("메모 중에 환불 얘기가 있는 파일을 모두 알려줘")
    #expect(parse(model.calls[1].lastContent)["decision"] == "all")
}

@Test func findArgumentsSentInTheWrongShapeAreFixedNotRefused() {
    let fixed = normalizedFindArguments([
        "keywords": "메모", "content_keywords": ["환불", ""], "extensions": "", "folder_hint": "", "modified_after": .null,
    ])
    #expect(fixed == ["keywords": ["메모"], "content_keywords": ["환불"], "modified_after": .null])
}
