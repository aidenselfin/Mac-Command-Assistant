import Foundation
import SymbioCore
import SymbioPlanner
import Testing
@testable import SymbioRuntime

/// RuntimeController cases from tests/test_followup.py, test_planner.py and test_free_text.py.

// MARK: - test_followup.py

@Test func theNextCommandGetsTheLastAnswerAndOnlyForTenMinutes() async throws {
    let answer = Completed("회의 요약입니다.", changedPaths: ["/r/요약.md"], readPaths: ["/r/a.md"])
    let planner = try ScriptedPlanner([answer, Completed("둘"), Completed("셋"), Completed("넷")].map { .outcome(.completed($0)) })
    let clock = FakeClock()
    let controller = RuntimeController(planner, clock: { clock.now })

    _ = try await controller.submit("회의록 요약해줘")
    #expect(planner.runOptions[0].previous == nil)  // nothing came before the first command

    clock.now += 30
    _ = try await controller.submit("파일로 저장해줘")
    #expect(planner.runOptions[1].previous == Exchange("회의록 요약해줘", "회의 요약입니다.", ["/r/요약.md", "/r/a.md"]))

    clock.now += 60  // still recent: the memory is always the command just before
    _ = try await controller.submit("그 파일 다시")
    #expect(planner.runOptions[2].previous?.request == "파일로 저장해줘")

    clock.now += memorySeconds + 1  // too long ago: the follow-up stands alone
    _ = try await controller.submit("또")
    #expect(planner.runOptions[3].previous == nil)
}

@Test func rememberedFilesAreDedupedAndCapped() async throws {
    let answer = Completed("답", changedPaths: ["/a", "/b"], readPaths: ["/b", "/c", "/d"])
    let planner = try ScriptedPlanner([.outcome(.completed(answer)), .outcome(.completed(Completed("둘")))])
    let controller = RuntimeController(planner)
    _ = try await controller.submit("첫")
    _ = try await controller.submit("둘")
    #expect(planner.runOptions[1].previous?.files == ["/a", "/b", "/c"])
}

@Test func errorsCancelsAndQuestionsAreNotRemembered() async throws {
    let context = ClarificationContext("요약", "file_candidate", ["1", "2"], 1)
    let question = Clarification("?", ["1", "2"], context)
    let planner = try ScriptedPlanner([
        .outcome(.error("실패")), .outcome(.clarification(question)), .outcome(.completed(Completed("마지막"))),
    ])
    let controller = RuntimeController(planner)

    _ = try await controller.submit("실패할 명령")
    _ = try await controller.submit("요약해줘")  // asks which file
    let cancelled = try #require(try await controller.submit("취소").completed)
    #expect(cancelled.text.hasPrefix("요청을 취소했습니다"))
    _ = try await controller.submit("다음 명령")

    #expect(planner.runOptions.allSatisfy { $0.previous == nil })
}

@Test func anAnswerToAQuestionIsNotANewCommandAndKeepsTheMemory() async throws {
    let context = ClarificationContext("요약", "file_candidate", ["1", "2"], 1)
    let planner = try ScriptedPlanner(
        [
            .outcome(.completed(Completed("앞선 답", readPaths: ["/r/a.md"]))),
            .outcome(.clarification(Clarification("?", ["1", "2"], context))),
            .outcome(.completed(Completed("마지막"))),
        ],
        resume: [.outcome(.completed(Completed("선택한 파일 요약")))])
    let controller = RuntimeController(planner)

    _ = try await controller.submit("a.md 읽어줘")
    _ = try await controller.submit("요약해줘")
    _ = try await controller.submit("1번")  // the answer: resumes, no new run call
    #expect(planner.runInputs.count == 2)
    _ = try await controller.submit("그 파일 번역해줘")
    // Remembered: the command that was just finished (the summary of the chosen file).
    #expect(planner.runOptions[2].previous?.request == "요약해줘")
    #expect(planner.runOptions[2].previous?.reply == "선택한 파일 요약")
}

// MARK: - test_planner.py (real Planner)

@Test func interactiveNumberResumesPendingTask() async throws {
    let config = try makeConfig()
    let first = config.path("회의록-1.md"), second = config.path("회의록-2.md")
    try writeText("첫 번째", first)
    try writeText("두 번째", second)
    let model = ScriptedModel(turns: [findCall("회의록"), readCall(second), turn("두 번째 회의록을 요약했습니다.")])
    let session = RuntimeController(try makePlanner(config, FakeGateway(fallback: ok("두 번째 회의록 본문")), model))

    let question = try #require(try await session.submit("회의록 요약해줘").clarification)
    let index = try #require(question.options.firstIndex(of: second)) + 1
    #expect(try await session.submit("\(index)번") == done("두 번째 회의록을 요약했습니다."))
}

@Test func editCriteriaSelectionResumesOriginalWriteTaskThroughTheRuntime() async throws {
    let config = try makeConfig()
    let readme = config.path("README.md")
    let gateway = FakeGateway(results: [ok("원문"), ok("updated")])
    let model = ScriptedModel(turns: [
        readCall(readme, id: "read"), writeTurn(readme, "정리된 문서"), turn("선택한 기준으로 정리했습니다."),
    ])
    let session = RuntimeController(try makePlanner(config, gateway, model))

    #expect(try await session.submit("README를 깔끔하게 정리해줘").clarification != nil)
    #expect(try await session.submit("1").approval != nil)
    #expect(try await session.approvePending() == done("선택한 기준으로 정리했습니다."))
    #expect(gateway.names == ["filesystem__read_text_file", "filesystem__write_file"])
    #expect(systemText(model.calls.last ?? []).contains("사용자가 선택한 편집 기준"))
}

@Test func cancellingClarificationDoesNotWrite() async throws {
    let gateway = FakeGateway(), model = ScriptedModel(turns: [])
    let session = RuntimeController(try makePlanner(try makeConfig(), gateway, model))
    #expect(try await session.submit("README를 깔끔하게 정리해줘").clarification != nil)
    let outcome = try #require(try await session.submit("취소").completed)
    #expect(outcome.text.contains("변경하지 않았습니다"))
    #expect(gateway.names.isEmpty && model.count == 0)
}

@Test func invalidChoicesStopAfterTwoClarificationsWithoutWrite() async throws {
    let gateway = FakeGateway(), model = ScriptedModel(turns: [])
    let session = RuntimeController(try makePlanner(try makeConfig(), gateway, model))

    #expect(try await session.submit("README를 깔끔하게 정리해줘").clarification != nil)
    let retry = try #require(try await session.submit("선택 아님").clarification)
    #expect(retry.question == "올바른 선택지 번호를 입력하거나 취소를 입력해 주세요.")
    #expect(retry.context.clarificationCount == 2)
    let stopped = try #require(try await session.submit("99").errorMessage)
    #expect(stopped.contains("최대 횟수"))
    #expect(session.pending == nil)
    #expect(model.count == 0 && gateway.names.isEmpty)
}

// MARK: - test_free_text.py

private let freeTools = [tool("filesystem__write_file"), tool("filesystem__list_directory")]

private func ask(_ question: String = "새 파일의 이름을 알려 주세요.") -> ModelTurn {
    ModelTurn(nil, [], ClarificationRequest(question, [], "free_text", "q"))
}

@Test func typedAnswerSurvivesALaterFileChoiceThroughTheRuntime() async throws {
    let config = try makeConfig()
    for name in ["회의록-초안.md", "회의록-최종.md"] { try writeText("회의 내용", config.path(name)) }
    let model = ScriptedModel(turns: [ask("어떤 내용을 추출할까요?"), findCall("회의록")])
    let gateway = FakeGateway(tools: freeTools + [tool("filesystem__read_text_file")], fallback: ok("ok"))
    let runtime = RuntimeController(try makePlanner(config, gateway, model))

    #expect(try await runtime.submit("회의록에서 필요한 항목을 추출해줘").clarification != nil)
    let choices = try #require(try await runtime.submit("담당자별 할 일만").clarification)
    #expect(choices.context.kind == "file_candidate")
    let chosen = choices.options[0]
    model.append([readCall(chosen, id: "r"), turn("담당자별 할 일입니다.")])
    #expect(try await runtime.submit("1번").completed != nil)
    let messages = model.calls[model.count - 2]
    #expect(messages[1]["content"]?.stringValue?.contains("담당자별 할 일만") == true)
    #expect(systemText(messages).contains(chosen))
}

@Test func runtimeSendsAnyTypedAnswerToTheTaskAndCancelStillCancels() async throws {
    let config = try makeConfig()
    let target = config.path("2.md")
    let write = turn(nil, callJSON("w", "filesystem__write_file", ["path": .string(target), "content": ""]))
    let model = ScriptedModel(turns: [ask(), write, ask()])
    let controller = RuntimeController(try makePlanner(config, FakeGateway(tools: freeTools, fallback: ok("ok")), model))

    #expect(try await controller.submit("파일 만들어줘").clarification != nil)
    // "2" is just text here: a typed question has no numbered options to pick from.
    let pending = try #require(try await controller.submit("2.md").approval)
    #expect(pending.operation == "create" && pending.targetPath == target)
    _ = try await controller.rejectPending()

    #expect(try await controller.submit("또 만들어줘").clarification != nil)
    #expect(try await controller.submit("취소") == done("요청을 취소했습니다. 파일은 변경하지 않았습니다."))
}
