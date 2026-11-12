import Foundation
import SymbioCore
import Testing
@testable import SymbioPlanner

/// tests/test_free_text.py: the planner side (the model adapter cases live in ModelTests).

private let freeTools = [tool("filesystem__write_file"), tool("filesystem__list_directory")]

private func make(_ config: Config, _ turns: [ModelTurn], extraTools: [ToolDefinition] = []) throws
    -> (Planner, ScriptedModel, FakeGateway)
{
    let gateway = FakeGateway(tools: freeTools + extraTools, fallback: ok("ok"))
    let model = ScriptedModel(turns: turns)
    return (try makePlanner(config, gateway, model), model, gateway)
}

private func ask(_ question: String = "새 파일의 이름을 알려 주세요.") -> ModelTurn {
    ModelTurn(nil, [], ClarificationRequest(question, [], "free_text", "q"))
}

private func write(_ path: String) -> ModelTurn {
    turn(nil, callJSON("w", "filesystem__write_file", ["path": .string(path), "content": ""]))
}

@Test func aTypedQuestionHasNoButtonsAndKeepsTheOriginalRequest() async throws {
    let (planner, _, _) = try make(try makeConfig(), [ask()])
    let outcome = try #require(try await planner.run("빈 md 파일 하나 만들어줘").clarification)
    #expect(outcome.options.isEmpty)
    #expect(outcome.context.kind == "free_text" && outcome.context.clarificationCount == 1)
    #expect(outcome.context.originalRequest == "빈 md 파일 하나 만들어줘")
    #expect(outcome.shownOptions.isEmpty)
}

@Test func theTypedAnswerContinuesTheSameTaskToAnApproval() async throws {
    let config = try makeConfig()
    let target = config.path("메모.md")
    let (planner, model, _) = try make(config, [ask(), write(target)])
    let question = try #require(try await planner.run("빈 md 파일 하나 만들어줘").clarification)
    let pending = try #require(try await planner.resume(question.context, "메모.md").approval)
    #expect(pending.operation == "create" && pending.targetPath == target)
    let system = model.calls[1].system
    #expect(system.contains("사용자가 질문에 직접 답한 내용(누적): 메모.md"))
    #expect(system.contains("승인 규칙은 바뀌지 않습니다"))
    #expect(model.calls[1].messages[1]["content"] == "빈 md 파일 하나 만들어줘\n\n(질문에 대한 사용자의 답: 메모.md)")
    #expect(!fileExists(target))  // still nothing written before approval
}

@Test func typedAnswersAccumulateAndTheBudgetIsTwoQuestions() async throws {
    let (planner, model, _) = try make(try makeConfig(), [ask("이름?"), ask("내용?"), ask("또 뭐?")])
    let first = try #require(try await planner.run("파일 하나 만들어줘").clarification)
    let second = try #require(try await planner.resume(first.context, "메모.md").clarification)
    #expect(second.context.conditions == ["메모.md"])
    let third = try await planner.resume(second.context, "비워 둬")
    #expect(third.errorMessage?.contains("clarification 최대") == true)
    #expect(model.calls[2].system.contains("메모.md; 비워 둬"))
}

@Test func typedAnswerSurvivesALaterFileChoice() async throws {
    let config = try makeConfig()
    for name in ["회의록-초안.md", "회의록-최종.md"] { try writeText("회의 내용", config.path(name)) }
    let find = turn(nil, callJSON("f", "local__find_files", ["keywords": ["회의록"]]))
    let gateway = FakeGateway(tools: freeTools + [tool("filesystem__read_text_file", "읽기")], fallback: ok("ok"))
    let model = ScriptedModel(turns: [ask("어떤 내용을 추출할까요?"), find])
    let planner = try makePlanner(config, gateway, model)
    let question = try #require(try await planner.run("회의록에서 필요한 항목을 추출해줘").clarification)
    let choices = try #require(try await planner.resume(question.context, "담당자별 할 일만").clarification)
    #expect(choices.context.kind == "file_candidate")
    let chosen = choices.options[0]
    let second = ScriptedModel(turns: [
        turn(nil, callJSON("r", "filesystem__read_text_file", ["path": .string(chosen)])), turn("담당자별 할 일입니다."),
    ])
    let resumed = try makePlanner(config, gateway, second, journal: false)
    #expect(try await resumed.resume(choices.context, chosen).completed != nil)
    let messages = second.calls[0].messages
    let user = messages[1]["content"]?.stringValue ?? "", system = messages[0]["content"]?.stringValue ?? ""
    #expect(user.contains("담당자별 할 일만"))
    #expect(system.contains(chosen))
}

@Test func aBlankTypedAnswerIsRefusedAndOptionsAreNotRequired() async throws {
    let (planner, _, _) = try make(try makeConfig(), [])
    let context = ClarificationContext("파일 만들어줘", "free_text", [], 1)
    #expect(try await planner.resume(context, "   ").errorMessage != nil)
}

@Test func aYesNoQuestionIsTurnedBackSoTheApprovalScreenDoesTheAsking() async throws {
    let config = try makeConfig()
    try writeText("x", config.path("old.log"))
    let (planner, model, _) = try make(config, [
        ask("old.log 파일을 삭제하시겠습니까?"),
        turn(nil, callJSON("d", "local__delete_files", ["paths": [.string(config.path("old.log"))]])),
    ])
    let outcome = try await planner.run("old.log 삭제해줘")
    #expect(outcome.approval?.operation == "delete")  // went to the approval, not a question
    #expect(model.calls[1].lastContent.contains("확인 질문은 필요 없습니다"))
}

@Test func askingWhereAFileIsComesAfterSearchingForIt() async throws {
    let config = try makeConfig()
    try writeText("x", config.path("예산안.md"))
    let find = turn(nil, callJSON("f", "local__find_files", ["keywords": ["예산안"]]))
    let (planner, model, _) = try make(config, [ask("읽을 파일의 이름을 알려 주세요."), find, turn("찾았습니다")])
    let outcome = try await planner.run("예산안 내용 읽어줘")
    #expect(model.calls[1].lastContent.contains("먼저 찾으세요"))
    #expect(outcome.errorMessage != nil || outcome.completed != nil)  // the single match was never read: no claim
}

@Test(arguments: ["빈 md 파일 하나 만들어줘", "내말은 파일로 저장해 달라는거였어"])
func creatingOrSavingMayAskForANameStraightAway(text: String) async throws {
    let (planner, _, _) = try make(try makeConfig(), [ask("새 파일의 이름을 알려 주세요.")])
    #expect(try await planner.run(text).clarification?.context.kind == "free_text")
}

@Test func aKnownTargetFromTheLastCommandLetsAFollowUpAskFreely() async throws {
    let (planner, _, _) = try make(try makeConfig(), [ask("어떤 이름으로 저장할까요?")])  // open: asks for a name
    let outcome = try await planner.run("요약해줘", previous: Exchange("a.md 읽어줘", "내용", ["/r/a.md"]))
    #expect(outcome.clarification != nil)
}

@Test(arguments: [
    "a.csv를 JSON으로 변환할까요?", "변환을 진행해도 괜찮으시면 알려 주세요.",
    "a.csv를 변환하시겠습니까? 진행 여부를 알려주세요.",
])
func aStalledConfirmationGetsOneNudgeThenTheApprovalScreen(stall: String) async throws {
    let config = try makeConfig()
    try writeText("x,y\n1,2\n", config.path("a.csv"))
    let convert = turn(nil, callJSON("c", "local__convert_file", ["source": .string(config.path("a.csv")), "target_format": "json"]))
    let (planner, model, _) = try make(config, [turn(stall), convert])
    let outcome = try await planner.run("a.csv를 json으로 바꿔줘")
    #expect(outcome.approval?.operation == "convert")  // the real approval, not a question
    #expect(model.calls[1].lastContent.contains("승인 화면에서 사용자 승인을 따로 받습니다"))
}

@Test func theNudgeIsGivenOnceSoAStubbornReplyStillEndsTheCommand() async throws {
    let (planner, model, _) = try make(try makeConfig(), [turn("변환할까요?"), turn("그래도 변환할까요?")])
    #expect(try await planner.run("a.csv를 json으로 바꿔줘") == done("그래도 변환할까요?"))
    #expect(model.count == 2)
}

@Test(arguments: [
    ("a.csv 내용 요약해줘", "요약할까요?"),  // nothing would be changed: not a mutation
    ("a.csv를 json으로 바꿔줘", "어떤 이름으로 저장할까요? 알려 주세요."),  // asks for a name
    ("a.csv를 json으로 바꿔줘", "csv에 열이 두 개 있습니다."),  // an ordinary answer
])
func otherRepliesAreLeftAlone(request: String, reply: String) async throws {
    let (planner, model, _) = try make(try makeConfig(), [turn(reply)])
    #expect(try await planner.run(request) == done(reply))
    #expect(model.count == 1)
}

@Test func anEmptyFinalReplyIsSentBackOnceBeforeTheCommandFails() async throws {
    let config = try makeConfig()
    var (planner, model, _) = try make(config, [turn(""), turn("최종 답입니다.")])
    #expect(try await planner.run("안녕") == done("최종 답입니다."))
    #expect(model.calls[1].lastContent.contains("최종 응답을 작성하세요"))
    let gateway = FakeGateway(tools: freeTools)
    model = ScriptedModel(turns: [turn(""), turn(nil)])
    planner = try makePlanner(config, gateway, model, journal: false)
    await #expect(throws: PlannerError("LLM 최종 응답이 비어 있습니다.")) { try await planner.run("안녕") }
    #expect(model.count == 2)  // one retry, not a loop
}

@Test func nullArgumentsAreDroppedBeforeTheCallIsChecked() async throws {
    let config = try makeConfig()
    try writeText("x", config.path("a.txt"))
    // The provider let `head: null` and `tail: null` through; the app must not forward them.
    let arguments: JSON = ["path": .string(config.path("a.txt")), "head": nil, "tail": nil]
    let (planner, _, gateway) = try make(config, [turn(nil, callJSON("r", "filesystem__list_directory", arguments)), turn("끝")])
    _ = try await planner.run("a.txt 목록")
    #expect(gateway.calls[0].arguments == ["path": .string(config.path("a.txt"))])
}

@Test func askingAgainForANamedFileThatWasJustNotFoundIsTurnedBack() async throws {
    let find = turn(nil, callJSON("f", "local__find_files", ["keywords": ["없는파일.txt"]]))
    let (planner, model, _) = try make(
        try makeConfig(), [find, ask("파일의 정확한 경로를 알려 주세요."), turn("없는파일.txt는 없습니다.")])
    #expect(try await planner.run("없는파일.txt 읽어줘") == done("없는파일.txt는 없습니다."))
    #expect(model.calls[2].lastContent.contains("그 파일이 없다고"))
}
